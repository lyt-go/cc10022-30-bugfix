"""增量分发同步链路测试：以目标实收基准驱动新增/更新/删除的互斥检测。

覆盖问题单场景：
1. 再次查询时已同步黄金数据不能重复显示成新增，数量对齐目标实收；
2. 检测只读，执行检测不推进基准、不代表已同步；
3. 同一黄金ID两次地址更正、首次未送达后客服台重试，每批变化都能从
   目标此前实际收过的数据推出；
4. 发送成功后重复做未改动检测应为空；再变更一个客户、停用另一个，
   差异分别归入更新与删除；
5. 新增/更新/删除按黄金ID互斥，接收凭证落库。
"""
from __future__ import annotations

import itertools
import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.golden_record import GoldenRecordService
from app.services.mdm_sync import MdmSyncService
from app.store import store

_ENTITY_SEQ = itertools.count(1)


def _make_sync_config(entity: str) -> int:
    """登记一个同步中配置并返回其 id（字段范围含业务字段与黄金ID）。"""
    sync_svc = MdmSyncService()
    entry, missing = sync_svc.create_entry({
        "配置编号": f"MDM-INC-{entity}",
        "黄金实体": entity,
        "目标系统": f"客服台-{entity}",
        "同步方式": "增量",
        "字段范围": "黄金ID,name,address,phone",
        "变化检测": "增量",
        "冲突策略": "本地优先",
        "同步状态": "同步中",
    })
    assert not missing, f"同步配置缺少必填字段: {missing}"
    entry["status"] = "同步中"
    entry["pending"] = False
    return entry["id"]


def _ids(rows: list[dict]) -> list[str]:
    return [r["黄金ID"] for r in rows]


def _assert_mutex(changes: dict) -> None:
    buckets = (
        _ids(changes["new_records"]),
        _ids(changes["updated_records"]),
        _ids(changes["deleted_records"]),
    )
    joined = buckets[0] + buckets[1] + buckets[2]
    assert len(joined) == len(set(joined)), "新增/更新/删除必须按黄金ID互斥"


class TestIncrementalSync:

    def setup_method(self) -> None:
        # 每个用例使用独立实体，避免与种子数据和其它用例串扰
        self.entity = f"inc_customer_{next(_ENTITY_SEQ)}"
        self.golden_svc = GoldenRecordService()
        self.sync_svc = MdmSyncService()
        self.sync_id = _make_sync_config(self.entity)

    def _create_customer(self, name: str, address: str, phone: str = "13800000000") -> str:
        result = self.golden_svc.create_golden_record(
            self.entity,
            {"name": name, "address": address, "phone": phone},
            ["SRC-1"],
        )
        assert result["ok"], f"黄金记录创建失败: {result}"
        return result["golden_record"]["黄金ID"]

    # ── 场景1：首同步后再次检测，已同步数据不得重复算作新增 ──
    def test_synced_data_not_reported_as_new_again(self):
        gid_a = self._create_customer("客户甲", "地址A")
        gid_b = self._create_customer("客户乙", "地址B")

        first_detect = self.sync_svc.detect_changes_for_sync(self.sync_id)
        assert first_detect["ok"]
        assert first_detect["new_count"] == 2, "首检测两条都应是新增"
        assert first_detect["updated_count"] == 0
        assert first_detect["deleted_count"] == 0
        _assert_mutex(first_detect)

        # 检测只读：检测过后基准仍为空，再查结果应保持一致而非被“消费”
        again_detect = self.sync_svc.detect_changes_for_sync(self.sync_id)
        assert again_detect["new_count"] == 2, "检测本身不能推进基准"

        # 成功分发：数量与目标实际收到的记录一致
        sent = self.sync_svc.sync_golden_to_target(self.sync_id)
        assert sent["ok"]
        assert sent["synced_count"] == 2, f"目标实收2条，实际: {sent['synced_count']}"
        assert sent["new_count"] == 2 and sent["deleted_count"] == 0

        # 再次检测：已同步的黄金数据不能再显示成新增，更新行也不应丢失
        re_detect = self.sync_svc.detect_changes_for_sync(self.sync_id)
        assert re_detect["new_count"] == 0, "已同步数据重复显示成新增"
        assert re_detect["updated_count"] == 0
        assert re_detect["deleted_count"] == 0
        assert re_detect["change_count"] == 0
        assert {gid_a, gid_b} == set(self.sync_svc._baseline(
            self.sync_svc.get_entry(self.sync_id))), "基准应等于目标实收"

    # ── 场景2：检测与分发共用同一份变化数据 ──
    def test_detect_and_dispatch_share_same_change_dataset(self):
        gid = self._create_customer("客户丙", "旧地址")
        self.sync_svc.sync_golden_to_target(self.sync_id)

        upd = self.golden_svc.update_golden_record(gid, {"address": "新地址-1"})
        assert upd["changed"] and list(upd["changed_fields"]) == ["address"]

        detected = self.sync_svc.detect_changes_for_sync(self.sync_id)
        dispatched = self.sync_svc.sync_golden_to_target(self.sync_id)
        # 两个入口同源：变化明细（含实际变更字段与新旧值）必须完全一致
        d_upd = detected["updated_records"][0]
        s_upd = dispatched["changes"]["updated_records"][0]
        assert d_upd["黄金ID"] == s_upd["黄金ID"] == gid
        assert d_upd["changed_fields"] == s_upd["changed_fields"]
        assert d_upd["changed_fields"]["address"] == {"old": "旧地址", "new": "新地址-1"}
        assert detected["new_count"] == dispatched["new_count"]
        assert detected["updated_count"] == dispatched["updated_count"]
        assert detected["deleted_count"] == dispatched["deleted_count"]

        # 接收凭证保存了实际变更字段
        receipt = self.sync_svc.list_receipts(self.sync_id)[-1]
        assert receipt["updated_ids"] == [gid]
        assert receipt["updated_detail"][0]["changed_fields"]["address"]["new"] == "新地址-1"
        assert receipt["received_count"] == 1

    # ── 场景3：两次地址更正，首批未送达，客服台重试 ──
    def test_two_corrections_with_failed_first_delivery_then_retry(self):
        gid = self._create_customer("客户丁", "地址初版")
        # 首批（新增）送达成功，目标实收基准建立
        first = self.sync_svc.sync_golden_to_target(self.sync_id)
        assert first["ok"] and first["synced_count"] == 1

        # 第一次地址更正提交，但目标接收失败
        self.golden_svc.update_golden_record(gid, {"address": "更正一"})
        failed = self.sync_svc.sync_golden_to_target(self.sync_id, deliver=False)
        assert failed["ok"] is False
        assert failed["changes"]["updated_records"][0]["changed_fields"]["address"] == {
            "old": "地址初版", "new": "更正一"}
        entry = self.sync_svc.get_entry(self.sync_id)
        baseline_after_fail = self.sync_svc._baseline(entry)
        assert baseline_after_fail[gid]["address"] == "地址初版", "接收失败不得推进基准"
        receipts_before = len(self.sync_svc.list_receipts(self.sync_id))

        # 第二次地址更正在首个变更尚未成功送到时已提交
        self.golden_svc.update_golden_record(gid, {"address": "更正二"})

        # 客服台随后重试接收：这一批必须能从它此前实际收过的数据推出
        retry = self.sync_svc.detect_changes_for_sync(self.sync_id)
        assert retry["new_count"] == 0 and retry["deleted_count"] == 0
        assert retry["updated_count"] == 1
        upd = retry["updated_records"][0]
        assert upd["黄金ID"] == gid
        # 旧值正是目标此前实际收到的“地址初版”，而不是从未送达的“更正一”
        assert upd["changed_fields"]["address"] == {"old": "地址初版", "new": "更正二"}

        retry_send = self.sync_svc.sync_golden_to_target(self.sync_id)
        assert retry_send["ok"]
        assert retry_send["updated_count"] == 1
        assert self.sync_svc._baseline(
            self.sync_svc.get_entry(self.sync_id))[gid]["address"] == "更正二"
        # 失败批次不产生凭证，成功重试才落一张凭证
        assert len(self.sync_svc.list_receipts(self.sync_id)) == receipts_before + 1

        # 重试成功后再检测为空：目标已与黄金侧一致
        assert self.sync_svc.detect_changes_for_sync(self.sync_id)["change_count"] == 0

    # ── 场景4：成功后重复未改动检测为空；再做一更新一停用 ──
    def test_unchanged_redetect_empty_then_update_and_delete(self):
        gid_keep = self._create_customer("客户戊", "地址戊")
        gid_stop = self._create_customer("客户己", "地址己")
        base = self.sync_svc.sync_golden_to_target(self.sync_id)
        assert base["ok"] and base["synced_count"] == 2

        # 发送成功后重复做一次未改动的检测
        quiet = self.sync_svc.detect_changes_for_sync(self.sync_id)
        assert quiet["change_count"] == 0, "无变化检测必须三个集合全空"
        assert quiet["new_count"] == quiet["updated_count"] == quiet["deleted_count"] == 0

        # 变更一个客户（更新），停用另一个（删除边界）
        self.golden_svc.update_golden_record(gid_keep, {"address": "戊的新地址"})
        off = self.golden_svc.deactivate_golden_record(gid_stop)
        assert off["ok"] and off["status"] == "已停用"
        # 停用记录确实离开可分发集合
        assert all(r["黄金ID"] != gid_stop
                   for r in self.golden_svc.list_syncable_records(self.entity))

        diff = self.sync_svc.detect_changes_for_sync(self.sync_id)
        _assert_mutex(diff)
        assert diff["new_count"] == 0
        assert _ids(diff["updated_records"]) == [gid_keep], "字段变化应归为更新"
        assert _ids(diff["deleted_records"]) == [gid_stop], "停用越过删除边界应归为删除"
        upd = diff["updated_records"][0]
        assert upd["changed_fields"]["address"] == {"old": "地址戊", "new": "戊的新地址"}
        assert diff["deleted_records"][0]["last_data"]["address"] == "地址己"

        # 分发后目标实收：保留更新后的客户，停用客户从基准移除
        sent = self.sync_svc.sync_golden_to_target(self.sync_id)
        assert sent["ok"]
        assert sent["updated_count"] == 1 and sent["deleted_count"] == 1
        assert sent["synced_count"] == 1, "目标实际只收到1条更新记录"
        baseline = self.sync_svc._baseline(self.sync_svc.get_entry(self.sync_id))
        assert set(baseline) == {gid_keep}, "删除的黄金ID必须移出目标实收基准"
        assert baseline[gid_keep]["address"] == "戊的新地址"

        receipt = self.sync_svc.list_receipts(self.sync_id)[-1]
        assert receipt["deleted_ids"] == [gid_stop]
        assert receipt["updated_ids"] == [gid_keep]
        assert receipt["baseline_count_after"] == 1

        # 最终再检测：一切已同步，无新增/更新/删除
        final = self.sync_svc.detect_changes_for_sync(self.sync_id)
        assert final["change_count"] == 0

    # ── 场景5：新增与更新在同一批次中按黄金ID互斥且计数正确 ──
    def test_mixed_batch_counts_and_exclusivity(self):
        gid_old = self._create_customer("老客户", "老地址")
        self.sync_svc.sync_golden_to_target(self.sync_id)

        gid_new = self._create_customer("新客户", "新地址")
        self.golden_svc.update_golden_record(gid_old, {"address": "老客户新地址"})

        changes = self.sync_svc.detect_changes_for_sync(self.sync_id)
        _assert_mutex(changes)
        assert _ids(changes["new_records"]) == [gid_new]
        assert _ids(changes["updated_records"]) == [gid_old]
        assert changes["change_count"] == 2
        assert changes["total_records"] == 2, "总数应等于当前生效黄金记录数"

        sent = self.sync_svc.sync_golden_to_target(self.sync_id)
        assert sent["synced_count"] == 2, "目标实收=1新增+1更新"
        # 凭证数量与变化分类可核验
        receipt = self.sync_svc.list_receipts(self.sync_id)[-1]
        assert receipt["new_ids"] == [gid_new]
        assert receipt["updated_ids"] == [gid_old]
        assert receipt["received_count"] == 2
        # 再查无重复新增
        assert self.sync_svc.detect_changes_for_sync(self.sync_id)["new_count"] == 0


def _run() -> int:
    tester = TestIncrementalSync()
    methods = [name for name in dir(tester) if name.startswith("test_")]
    passed = failed = 0
    for name in methods:
        tester.setup_method()
        try:
            getattr(tester, name)()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {name} -> {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n结果: {passed}/{len(methods)} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
