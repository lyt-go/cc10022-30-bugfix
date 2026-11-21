"""同步配置领域服务：黄金记录分发、增量同步与冲突策略管理。

增量检测以目标系统「实际收到并确认」的数据为基准：
- 检测（detect_changes_for_sync）只读不写，执行检测不推进基准，不代表已同步；
- 分发（sync_golden_to_target）与检测共用同一份同步变化数据，发送成功后
  保存接收凭证并以目标实收结果推进基准；
- 新增、更新、删除按黄金ID互斥，变化详情保留实际变更字段与删除边界。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MdmSyncService(BaseModuleService):
    MODULE = "mdm_sync"
    ENTITY = "同步配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["配置编号", "黄金实体", "目标系统", "同步方式", "字段范围", "变化检测", "冲突策略", "同步状态"]
    REQUIRED_FIELDS = ["配置编号", "黄金实体", "目标系统"]
    STATUS_ORDER = ["草稿", "待校验", "同步中", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用同步": "同步中", "停用同步": "已停用"}
    NEGATIVE_ACTIONS = ["停用同步"]

    # ── 目标实收基准与接收凭证 ──────────────────────────────────────
    def _field_scope(self, sync_cfg: dict[str, Any]) -> list[str]:
        fields = [f.strip() for f in str(sync_cfg.get("字段范围", "")).split(",") if f.strip()]
        if "黄金ID" not in fields:
            fields.insert(0, "黄金ID")
        return fields

    def _baseline(self, sync_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """读取目标系统实际收过的数据基准（黄金ID -> 已确认数据快照）。"""
        baseline = sync_cfg.get("received_baseline")
        if not isinstance(baseline, dict):
            return {}
        return dict(baseline)

    def list_receipts(self, sync_id: int) -> list[dict[str, Any]]:
        """读取该配置全部接收凭证，凭证以目标实际接收结果为准。"""
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg:
            return []
        receipts = sync_cfg.get("receipts")
        return list(receipts) if isinstance(receipts, list) else []

    def _compute_changes(self, sync_cfg: dict[str, Any]) -> dict[str, Any]:
        """编排同步变化数据：黄金服务给变更字段与删除边界，本服务按目标实收基准比较。

        检测接口与分发接口都只能通过这一个入口拿到变化数据，保证两处同源。
        """
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        entity = sync_cfg.get("黄金实体", "")
        field_scope = self._field_scope(sync_cfg)
        diff = golden_svc.diff_golden_snapshot(
            self._baseline(sync_cfg), entity, field_scope=field_scope)
        new_records = diff["new_records"]
        updated_records = diff["updated_records"]
        deleted_records = diff["deleted_records"]
        total_records = len(golden_svc.list_syncable_records(entity))
        return {
            "sync_id": sync_cfg.get("配置编号"),
            "target": sync_cfg.get("目标系统"),
            "detection_method": sync_cfg.get("变化检测", "增量"),
            "baseline_count": len(self._baseline(sync_cfg)),
            "total_records": total_records,
            "new_records": new_records,
            "updated_records": updated_records,
            "deleted_records": deleted_records,
            "new_count": len(new_records),
            "updated_count": len(updated_records),
            "deleted_count": len(deleted_records),
            "change_count": len(new_records) + len(updated_records) + len(deleted_records),
        }

    def detect_changes_for_sync(self, sync_id: int) -> dict[str, Any]:
        """检测变化数据：对比目标实收基准，识别新增、更新和删除的黄金记录。

        纯只读操作：不更新基准、不写凭证。重复执行且期间无变化时，
        结果必须是三个集合全空——执行过检测不代表数据已同步。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg:
            return {"ok": False, "reason": "同步配置不存在"}
        changes = self._compute_changes(sync_cfg)
        changes["ok"] = True
        return changes

    def sync_golden_to_target(self, sync_id: int, *, deliver: bool = True) -> dict[str, Any]:
        """将增量变化分发同步到目标系统。

        与检测共用同一份同步变化数据（_compute_changes）。发送成功后才：
        1. 按目标实际收到的结果推进 received_baseline（新增/更新覆盖，删除移除）；
        2. 保存带批次号、变化明细和接收数量的接收凭证。
        deliver=False 模拟目标接收失败：基准与凭证均不变，允许客服台随后重试。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg or sync_cfg.get("status") != "同步中":
            return {"ok": False, "reason": "同步配置不可用"}

        changes = self._compute_changes(sync_cfg)
        if not deliver:
            return {
                "ok": False,
                "reason": "目标系统接收失败，基准未推进，可重试",
                "sync_id": changes["sync_id"],
                "target": changes["target"],
                "changes": changes,
            }

        baseline = self._baseline(sync_cfg)
        # 新增与更新：以本次实际送达的数据覆盖目标实收基准
        for item in changes["new_records"] + changes["updated_records"]:
            baseline[item["黄金ID"]] = dict(item["data"])
        # 删除：黄金ID离开目标实收基准（删除边界由黄金服务的停用/移除给出）
        for item in changes["deleted_records"]:
            baseline.pop(item["黄金ID"], None)

        receipt = {
            "receipt_id": f"RCPT-{sync_cfg.get('配置编号')}-"
                          f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            "sync_id": changes["sync_id"],
            "target": changes["target"],
            "received_at": datetime.datetime.utcnow().isoformat(),
            "new_count": changes["new_count"],
            "updated_count": changes["updated_count"],
            "deleted_count": changes["deleted_count"],
            "received_count": changes["new_count"] + changes["updated_count"],
            "new_ids": [r["黄金ID"] for r in changes["new_records"]],
            "updated_ids": [r["黄金ID"] for r in changes["updated_records"]],
            "deleted_ids": [r["黄金ID"] for r in changes["deleted_records"]],
            "updated_detail": [
                {"黄金ID": r["黄金ID"], "changed_fields": r["changed_fields"]}
                for r in changes["updated_records"]
            ],
            "baseline_count_after": len(baseline),
        }
        # 只有发送成功才落基准与凭证：检测本身永远不会推进同步状态
        sync_cfg["received_baseline"] = baseline
        receipts = sync_cfg.setdefault("receipts", [])
        receipts.append(receipt)
        sync_cfg["last_sync_at"] = receipt["received_at"]

        return {
            "ok": True,
            "sync_id": changes["sync_id"],
            "target": changes["target"],
            "synced_count": receipt["received_count"],
            "new_count": receipt["new_count"],
            "updated_count": receipt["updated_count"],
            "deleted_count": receipt["deleted_count"],
            "changes": changes,
            "receipt": receipt,
        }

    def resolve_sync_conflicts(self, sync_id: int,
                                 local_records: list[dict[str, Any]],
                                 remote_records: list[dict[str, Any]]) -> dict[str, Any]:
        """解决同步冲突：按冲突策略处理本地与远程数据不一致。

        读取同步配置的冲突策略，对比本地和远程黄金记录数据，
        按策略优先级解决字段冲突并输出同步结果。
        """
        sync_cfg = self.get_entry(sync_id)
        strategy = (sync_cfg.get("冲突策略", "本地优先") if sync_cfg else "本地优先")
        resolved = 0
        conflicts = 0
        for local in local_records:
            remote = next(
                (r for r in remote_records if r.get("黄金ID") == local.get("黄金ID")),
                None
            )
            if remote:
                conflicts += 1
                if strategy == "本地优先":
                    resolved += 1
                elif strategy == "远程优先":
                    for k in remote:
                        local[k] = remote[k]
        # 解决同步冲突：按冲突策略处理本地与远程数据不一致
        return {"strategy": strategy, "conflicts_detected": conflicts, "resolved": resolved}

    def query_sync_configs_for_entity(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定黄金实体的所有同步配置。"""
        rows = store.rows(self.MODULE)
        return [
            row for row in rows
            if row.get("黄金实体") == entity_model and row.get("status") in ("待校验", "同步中")
        ]
