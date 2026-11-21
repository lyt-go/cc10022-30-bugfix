"""黄金记录领域服务：黄金记录创建、关联来源、置信度计算与版本管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GoldenRecordService(BaseModuleService):
    MODULE = "golden_record"
    ENTITY = "黄金记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["黄金ID", "实体模型", "关联来源", "关键字段", "代表字段", "置信度", "最后合并", "记录版本"]
    REQUIRED_FIELDS = ["黄金ID", "实体模型", "关联来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def create_golden_record(self, entity_model: str, merged_data: dict[str, Any],
                               source_ids: list[str]) -> dict[str, Any]:
        """创建黄金记录：将合并后的代表记录写入黄金记录存储。

        写入黄金记录到golden_records存储，关联来源批次与合并追溯信息，
        设置置信度、关联来源和最后合并时间，生成唯一黄金ID。
        """
        rows = store.rows(self.MODULE)
        golden_id = self._next_golden_id(entity_model, rows)
        confidence = self._compute_confidence(len(source_ids))
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "黄金ID": golden_id,
            "实体模型": entity_model,
            "关联来源": "|".join(source_ids),
            "关键字段": ",".join(k for k in merged_data if not k.startswith("_")),
            "代表字段": str(merged_data.get("representative", {})),
            "置信度": f"{confidence:.2f}",
            "最后合并": datetime.datetime.utcnow().isoformat(),
            "记录版本": "1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_ids),
        }
        for key, val in merged_data.items():
            if not key.startswith("_"):
                entry[key] = val
        rows.append(entry)
        # 写入黄金记录到golden_records存储，关联来源批次与合并追溯信息
        return {"ok": True, "golden_record": entry}

    def _next_golden_id(self, entity_model: str,
                          rows: list[dict[str, Any]]) -> str:
        """生成全局唯一黄金ID：时间戳前缀 + 同秒序号，避免同秒创建互相覆盖。"""
        prefix = f"GR-{entity_model}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        existing = {r.get("黄金ID") for r in rows}
        if prefix not in existing:
            return prefix
        seq = 1
        while f"{prefix}-{seq}" in existing:
            seq += 1
        return f"{prefix}-{seq}"

    def _compute_confidence(self, source_count: int) -> float:
        """计算黄金记录置信度：基于来源数量和质量指标综合评估。"""
        base = 0.5
        source_bonus = min(source_count * 0.1, 0.3)
        return min(round(base + source_bonus, 2), 0.99)

    def query_golden_record(self, golden_id: str) -> dict[str, Any] | None:
        """查询黄金记录：按黄金ID检索golden_records存储。

        查询黄金记录的完整字段信息、关联来源列表和置信度评分，
        返回黄金记录的完整快照数据。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id:
                # 查询黄金记录完整信息：包含关联来源和置信度
                return dict(row)
        return None

    def link_source_to_golden(self, golden_id: str, new_source_id: str) -> dict[str, Any]:
        """关联新来源记录到已有黄金记录。

        将新增来源记录关联到已有黄金记录，更新关联来源列表，
        重新计算置信度评分并更新最后合并时间。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        sources = record.get("关联来源", "").split("|")
        if "" in sources:
            sources.remove("")
        if new_source_id in sources:
            return {"ok": False, "reason": "来源已关联"}
        sources.append(new_source_id)
        record["关联来源"] = "|".join(sources)
        record["source_count"] = len(sources)
        record["置信度"] = f"{self._compute_confidence(len(sources)):.2f}"
        # 关联新来源记录：更新黄金记录的来源列表和置信度
        return {"ok": True, "golden_id": golden_id, "source_count": len(sources)}

    def validate_golden_completeness(self, golden_id: str,
                                      required_fields: list[str]) -> dict[str, Any]:
        """校验黄金记录的字段完整性：检查所有必填字段是否已填充。

        查询黄金记录数据，读取golden_records存储中的字段值，
        校验必填字段完整性并输出完备率统计。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
        complete_count = len(required_fields) - len(missing)
        return {
            "golden_id": golden_id,
            "total_required": len(required_fields),
            "filled": complete_count,
            "missing": missing,
            "completeness": round(complete_count / max(len(required_fields), 1) * 100, 2),
        }

    def update_golden_record(self, golden_id: str,
                               changed_data: dict[str, Any]) -> dict[str, Any]:
        """更新黄金记录：登记实际变更字段，未变化的字段不产生变更。

        仅写入与现值不同的业务字段，返回变化字段及新旧值；
        记录版本在发生真实变化后递增，最后合并时间同步刷新。
        执行检测本身不会调用本方法，因此检测不代表数据已变化或已同步。
        """
        rows = store.rows(self.MODULE)
        record = next((r for r in rows if r.get("黄金ID") == golden_id), None)
        if record is None:
            return {"ok": False, "reason": "黄金记录不存在"}
        changed_fields: dict[str, dict[str, Any]] = {}
        for key, new_val in changed_data.items():
            if key.startswith("_") or key in ("id", "黄金ID", "实体模型",
                                              "status", "pending", "abnormal",
                                              "记录版本", "最后合并"):
                continue
            if record.get(key) != new_val:
                changed_fields[key] = {"old": record.get(key), "new": new_val}
        if not changed_fields:
            return {"ok": True, "golden_id": golden_id, "changed_fields": {}, "changed": False}
        for key, diff in changed_fields.items():
            record[key] = diff["new"]
        record["记录版本"] = self._bump_version(record.get("记录版本", "1.0"))
        record["最后合并"] = datetime.datetime.utcnow().isoformat()
        # 登记实际变更字段：仅真实变化的字段才进入变更清单并推高记录版本
        return {
            "ok": True,
            "golden_id": golden_id,
            "changed": True,
            "changed_fields": changed_fields,
            "record_version": record["记录版本"],
        }

    def deactivate_golden_record(self, golden_id: str) -> dict[str, Any]:
        """停用黄金记录：给出删除边界，停用记录对下游即应删除。"""
        rows = store.rows(self.MODULE)
        record = next((r for r in rows if r.get("黄金ID") == golden_id), None)
        if record is None:
            return {"ok": False, "reason": "黄金记录不存在"}
        record["status"] = "已停用"
        record["pending"] = False
        record["abnormal"] = True
        record["停用时间"] = datetime.datetime.utcnow().isoformat()
        # 停用即删除边界：停用后的黄金记录不再出现在可分发集合中
        return {"ok": True, "golden_id": golden_id, "status": "已停用"}

    def _bump_version(self, version: str) -> str:
        """记录版本按小版本递增。"""
        try:
            return f"{float(version) + 1.0:.1f}"
        except (TypeError, ValueError):
            return "2.0"

    def list_syncable_records(self, entity_model: str) -> list[dict[str, Any]]:
        """列出可分发黄金记录：删除边界之外的记录（即全部已生效记录）。

        停用/删除的记录不在结果内，分发侧据此与目标实收基准比较出删除集。
        """
        rows = store.rows(self.MODULE)
        return [
            dict(row) for row in rows
            if row.get("status") == "已生效"
            and (not entity_model or row.get("实体模型") == entity_model)
        ]

    def diff_golden_snapshot(
        self,
        baseline: dict[str, dict[str, Any]],
        entity_model: str,
        field_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        """对比黄金记录与目标实收基准，按黄金ID输出互斥的新增/更新/删除。

        - 基准里没有、当前存在的黄金ID => 新增
        - 基准里有、当前仍存在但字段值变化 => 更新（附实际变化字段与新旧值）
        - 基准里有、当前已不存在（停用或移除）=> 删除
        三类集合以黄金ID互斥，同一条黄金记录在一次检测中只属于其中一类。
        """
        scope = [f for f in (field_scope or []) if f]
        current_rows = self.list_syncable_records(entity_model)
        current = {row["黄金ID"]: row for row in current_rows}

        def projected(row: dict[str, Any]) -> dict[str, Any]:
            if not scope:
                return {k: v for k, v in row.items() if not k.startswith("_")}
            return {f: row.get(f) for f in scope}

        new_records: list[dict[str, Any]] = []
        updated_records: list[dict[str, Any]] = []
        deleted_records: list[dict[str, Any]] = []
        for golden_id, row in current.items():
            if golden_id not in baseline:
                new_records.append({"黄金ID": golden_id, "data": projected(row)})
                continue
            old_snapshot = projected(baseline[golden_id])
            new_snapshot = projected(row)
            changed = {
                field: {"old": old_snapshot.get(field), "new": new_snapshot.get(field)}
                for field in new_snapshot
                if old_snapshot.get(field) != new_snapshot.get(field)
            }
            # 基准中存在但当前投影缺失的字段同样属于更新
            for field in old_snapshot:
                if field not in new_snapshot and old_snapshot.get(field) is not None:
                    changed[field] = {"old": old_snapshot.get(field), "new": None}
            if changed:
                updated_records.append({
                    "黄金ID": golden_id,
                    "changed_fields": changed,
                    "data": new_snapshot,
                })
        for golden_id, old_row in baseline.items():
            if golden_id not in current:
                deleted_records.append({
                    "黄金ID": golden_id,
                    "last_data": projected(old_row),
                })
        # 互斥校验：同一黄金ID不可能同时出现在两个集合里
        all_ids = (
            [r["黄金ID"] for r in new_records]
            + [r["黄金ID"] for r in updated_records]
            + [r["黄金ID"] for r in deleted_records]
        )
        assert len(all_ids) == len(set(all_ids)), "新增/更新/删除集合存在黄金ID重叠"
        return {
            "new_records": new_records,
            "updated_records": updated_records,
            "deleted_records": deleted_records,
            "new_count": len(new_records),
            "updated_count": len(updated_records),
            "deleted_count": len(deleted_records),
        }

    def search_golden_records(self, entity_model: str | None = None,
                                min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """搜索黄金记录：按实体模型和最低置信度筛选。

        查询golden_records存储，按实体模型筛选并过滤置信度
        低于阈值的记录，返回符合条件的黄金记录列表。
        """
        rows = store.rows(self.MODULE)
        results = [
            row for row in rows
            if row.get("status") == "已生效"
        ]
        if entity_model:
            results = [r for r in results if r.get("实体模型") == entity_model]
        results = [r for r in results if float(r.get("置信度", "0")) >= min_confidence]
        # 查询黄金记录：筛选已生效、匹配实体模型且置信度达标的记录
        results.sort(key=lambda r: float(r.get("置信度", "0")), reverse=True)
        return results

    def export_golden_records(self, entity_model: str) -> list[dict[str, Any]]:
        """导出黄金记录：将指定实体模型的黄金记录导出供下游系统消费。

        查询golden_records存储中已生效的黄金记录，导出完整的
        记录数据，包含关键字段、代表字段和置信度信息。
        """
        records = self.search_golden_records(entity_model=entity_model, min_confidence=0.5)
        # 导出黄金记录：将已生效记录数据导出供下游系统分发同步
        exported = []
        for r in records:
            exported.append({
                "golden_id": r.get("黄金ID"),
                "entity_model": r.get("实体模型"),
                "confidence": float(r.get("置信度", "0")),
                "sources": (r.get("关联来源", "")).split("|"),
                "key_fields": (r.get("关键字段", "")).split(","),
            })
        return exported