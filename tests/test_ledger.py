"""可重算台账测试。

固定合约年度 2026：
- 年度累计免赔额 1,000,000
- 第1层：起赔 1,000,000、层宽 4,000,000、成数 40%、0 次恢复
- 第2层：起赔 5,000,000、层宽 5,000,000、成数 20%、2 次恢复（总容量 15,000,000），恢复保费费率 10%
"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_ledger_service
from src.domain import Actor, Conflict, ValidationError
from src.ledger import (
    LayerSpec,
    OpenClaim,
    PENDING_REVIEW,
    RESERVED,
    SETTLED,
    INVALIDATED,
    recompute_year,
)

UW = Actor("u1", "underwriter")
CAT = Actor("c1", "catastrophe_analyst")
CO = Actor("o1", "claims_officer")
FIN = Actor("f1", "finance")

YEAR_PAYLOAD = {
    "year": 2026,
    "aggregate_deductible": 1_000_000,
    "layers": [
        {"layer_no": 1, "attachment": 1_000_000, "width": 4_000_000, "cession_pct": 0.4,
         "reinstatements": 0, "reinstatement_pct": 0.0},
        {"layer_no": 2, "attachment": 5_000_000, "width": 5_000_000, "cession_pct": 0.2,
         "reinstatements": 2, "reinstatement_pct": 0.1},
    ],
}


class LedgerLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_ledger_service(str(Path(self.temp.name) / "ledger.db"))
        self.service.setup_treaty_year(UW, YEAR_PAYLOAD)

    def tearDown(self):
        self.temp.cleanup()

    def _event(self, number, date):
        return self.service.register_event(CAT, {"event_number": number, "event_date": date})

    def _claim(self, number, loss, occurrence=None, event=None):
        payload = {"claim_number": number, "approved_loss": loss}
        if occurrence:
            payload["occurrence_date"] = occurrence
        if event:
            payload["event_number"] = event
        return self.service.register_claim(CO, payload)

    def _reserve(self, number):
        return self.service.reserve_claim(CO, number)

    def test_engine_aggregate_deductible_in_order(self):
        layers = [LayerSpec(1, 1_000_000, 4_000_000, 0.4, 0, 0.0)]
        claims = [
            OpenClaim(1, "A", "2026-02-01", 1_600_000),
            OpenClaim(2, "B", "2026-03-01", 1_600_000),
        ]
        ledger = recompute_year(layers, 1_000_000, claims)
        a, b = ledger.claim_ledgers
        # 第一笔：60万毛额先吃60万免赔，摊回0；第二笔免赔只剩40万：60万-40万=20万*40%
        self.assertEqual(a.accepted_recovery, 0.0)
        self.assertEqual(a.allocations[0].deductible_applied, 600_000.0)
        self.assertEqual(b.allocations[0].deductible_applied, 400_000.0)
        self.assertEqual(b.accepted_recovery, 80_000.0)
        self.assertEqual(ledger.aggregate_deductible_remaining, 0.0)

    def test_aad_ordering_and_layer_recovery(self):
        # 300万：层1毛额200万；按顺序第一笔吃满100万免赔
        self._event("CAT-1", "2026-02-01")
        self._claim("CLM-1", 3_000_000, event="CAT-1")
        r1 = self._reserve("CLM-1")
        self.assertEqual(r1["accepted_recovery"], 400_000.0)  # (200万-100万免赔)*40%
        self.assertFalse(r1["has_gap"])

        # 第二笔300万：层1还剩300万容量，免赔已耗尽 -> 200万*40%=80万
        self._claim("CLM-2", 3_000_000, occurrence="2026-05-01")
        r2 = self._reserve("CLM-2")
        self.assertEqual(r2["accepted_recovery"], 800_000.0)

        summary = self.service.year_summary(UW, "2026")
        self.assertEqual(summary["aggregate_deductible_reserved"], 1_000_000.0)
        self.assertEqual(summary["aggregate_deductible_remaining"], 0.0)
        self.assertEqual(summary["layers"][0]["reserved_accepted_gross"], 3_000_000.0)

    def test_settlement_confirms_consumption_and_freezes_amount(self):
        self._event("CAT-1", "2026-02-01")
        self._claim("CLM-1", 3_000_000, event="CAT-1")
        self._reserve("CLM-1")
        settled = self.service.settle_claim(FIN, "CLM-1", {"payment_reference": "PAY-1"})
        self.assertEqual(settled["status"], SETTLED)
        self.assertEqual(settled["confirmed_recovery"], 400_000.0)

        # 再核定一笔：100万免赔已由已结算赔案确认消耗，新赔案直接拿摊回
        self._claim("CLM-2", 2_000_000, occurrence="2026-05-01")
        r = self._reserve("CLM-2")
        # 100万毛额，免赔剩余0，100万*40%
        self.assertEqual(r["accepted_recovery"], 400_000.0)
        summary = self.service.year_summary(UW, 2026)
        self.assertEqual(summary["aggregate_deductible_confirmed"], 1_000_000.0)

        # 事件撤回：已结算赔案金额不动（撤回的事件没有其他未结算赔案）
        self.service.withdraw_event(CAT, "CAT-1", {"reason": "误录"})
        claim2 = self.service.get_claim(CO, "CLM-2")
        self.assertEqual(claim2["status"], RESERVED)  # 不关联被撤回事件，预占保留
        # CLM-1 虽关联撤回事件，但已结算，金额冻结
        claim1 = self.service.get_claim(CO, "CLM-1")
        self.assertEqual(claim1["status"], SETTLED)
        entries1 = [e for e in claim1["ledger_entries"] if e["status"] == "confirmed"]
        self.assertEqual(round(sum(e["accepted_recovery"] for e in entries1), 2), 400_000.0)

    def test_withdraw_invalidates_only_unsettled_and_recomputes(self):
        self._event("CAT-1", "2026-02-01")
        self._claim("CLM-1", 3_000_000, event="CAT-1")
        self._reserve("CLM-1")
        # 先结算
        self.service.settle_claim(FIN, "CLM-1", {"payment_reference": "PAY-1"})
        # 同事件再来一笔未结算赔案（占层1 100万容量）
        self._claim("CLM-2", 2_000_000, event="CAT-1")
        self._reserve("CLM-2")
        before = self.service.year_summary(UW, "2026")
        self.assertEqual(before["layers"][0]["reserved_accepted_gross"], 1_000_000.0)

        self.service.withdraw_event(CAT, "CAT-1", {"reason": "事件不成立"})
        claim2 = self.service.get_claim(CO, "CLM-2")
        self.assertEqual(claim2["status"], INVALIDATED)
        after = self.service.year_summary(UW, "2026")
        # 已结算 CLM-1：层1毛额200万但被100万免赔截断，确认占用100万；CLM-2 预占清零
        self.assertEqual(after["layers"][0]["confirmed_accepted_gross"], 1_000_000.0)
        self.assertEqual(after["layers"][0]["reserved_accepted_gross"], 0.0)

    def test_event_amend_reorders_by_occurrence_and_resets_only_unsettled(self):
        # 两笔不同事件赔案，CLM-1 先结算，吃掉层2恢复的部分容量
        self._event("CAT-A", "2026-06-01")
        self._event("CAT-B", "2026-01-01")
        self._claim("CLM-1", 8_000_000, event="CAT-A")  # 层2毛额300万
        self._claim("CLM-2", 8_000_000, event="CAT-B")  # 初始排在1月
        self._reserve("CLM-1")
        self.service.settle_claim(FIN, "CLM-1", {"payment_reference": "PAY-1"})
        self._reserve("CLM-2")

        # 补录把 CAT-B 改到 12月（晚于 CLM-1）：未结算的 CLM-2 顺序后移，已结算 CLM-1 不动
        self.service.amend_event(CAT, "CAT-B", {"event_date": "2026-12-15"})
        event_b = self.service.list_events(CAT)
        b = next(e for e in event_b if e["event_number"] == "CAT-B")
        self.assertEqual(b["version"], 2)
        claim2 = self.service.get_claim(CO, "CLM-2")
        self.assertEqual(claim2["occurrence_date"], "2026-12-15")
        self.assertEqual(claim2["status"], RESERVED)
        # 分录携带新事件版本
        self.assertTrue(all(e["event_version"] == 2 for e in claim2["ledger_entries"] if e["accepted_gross"] > 0))

        # 已结算金额不随重算改变（层2：300万*20%=60万）
        claim1 = self.service.get_claim(CO, "CLM-1")
        confirmed = [e for e in claim1["ledger_entries"] if e["status"] == "confirmed" and e["layer_no"] == 2]
        self.assertEqual(round(sum(e["accepted_recovery"] for e in confirmed), 2), 600_000.0)

    def test_last_reinstatement_concurrent_submit_only_one_passes(self):
        # 两笔各 1000 万损失（层2毛额各500万）先结算，层2确认占用1000万，只剩最后一次恢复（500万容量）
        for i in (1, 2):
            number = "CLM-SET-%s" % i
            self._event("CAT-0-%s" % i, "2026-01-%02d" % (10 + i))
            self._claim(number, 10_000_000, event="CAT-0-%s" % i)
            self._reserve(number)
            self.service.settle_claim(FIN, number, {"payment_reference": "PAY-%s" % i})

        # 两笔同日晚到赔案各1000万：层1各300万缺口（无恢复），层2各争500万，合计需求1000万 > 剩余500万
        self._event("CAT-X", "2026-09-01")
        self._claim("CLM-A", 10_000_000, event="CAT-X")
        self._claim("CLM-B", 10_000_000, event="CAT-X")

        barrier = threading.Barrier(2)
        outcomes = {}

        def submit(key):
            barrier.wait()
            try:
                outcomes[key] = self.service.reserve_claim(CO, key)
            except Exception as exc:
                outcomes[key] = {"error": repr(exc)}

        t1 = threading.Thread(target=submit, args=("CLM-A",))
        t2 = threading.Thread(target=submit, args=("CLM-B",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertNotIn("error", outcomes.get("CLM-A", {}), outcomes)
        self.assertNotIn("error", outcomes.get("CLM-B", {}), outcomes)

        # 不变量是最终台账（返回值会随线程拿到锁的先后而不同，但最终分录恒按赔案号定序）
        def layer_of(number, layer_no):
            view = self.service.get_claim(CO, number)
            return next(e for e in view["ledger_entries"] if e["layer_no"] == layer_no)

        layer_a = layer_of("CLM-A", 2)
        layer_b = layer_of("CLM-B", 2)
        self.assertEqual(layer_a["accepted_gross"], 5_000_000.0)
        self.assertEqual(layer_a["gap_gross"], 0.0)
        self.assertEqual(layer_b["accepted_gross"], 0.0)
        self.assertEqual(layer_b["gap_gross"], 5_000_000.0)
        self.assertEqual(layer_b["gap_amount"], 1_000_000.0)

        # 最终台账：层2用满 2 次恢复（确认1000万+预占500万=1500万），缺口只记一笔
        summary = self.service.year_summary(UW, 2026)
        self.assertEqual(summary["layers"][1]["confirmed_accepted_gross"], 10_000_000.0)
        self.assertEqual(summary["layers"][1]["reserved_accepted_gross"], 5_000_000.0)
        self.assertEqual(summary["layers"][1]["gap_gross"], 5_000_000.0)
        self.assertEqual(summary["layers"][1]["reinstatements_used"], 2)

    def test_batch_resume_does_not_double_count(self):
        self._event("CAT-1", "2026-02-01")
        self._claim("CLM-1", 3_000_000, event="CAT-1")
        self._claim("CLM-2", 3_000_000, event="CAT-1")
        self._claim("CLM-3", 3_000_000, event="CAT-1")

        items = [
            {"op_key": "0", "claim": "CLM-1"},
            {"op_key": "1", "claim": "CLM-2"},
            {"op_key": "2", "claim": "CLM-3"},
        ]
        # 批次头=1、op0=2、op1=3：在 op1 提交完成瞬间崩溃，op0/op1 已落盘
        self.service.repository.arm_fail_after_commits(3)

        with self.assertRaises(RuntimeError):
            self.service.batch_reserve(CO, {"batch_id": "BATCH-1", "items": items})

        # 清除注入，用完整批次续作
        self.service.repository.fail_commit_ordinals = set()
        result = self.service.batch_reserve(CO, {"batch_id": "BATCH-1", "items": items})
        self.assertEqual(result["completed"], 3)
        skips = [r for r in result["results"] if r.get("skipped")]
        # 至少 op0/op1 在崩溃前已落盘 -> 重跑跳过且不重复累计
        self.assertGreaterEqual(len(skips), 2, "已完成操作应被跳过")
        # 至少 op0/op1 之一在崩溃前已落盘 -> 重跑跳过且不重复累计
        self.assertTrue(skips, "已完成操作应被跳过")

        status = self.service.batch_status(CO, "BATCH-1")
        self.assertEqual(status["completed"], 3)

        # 台账只有一套当前分录（替换式重算），层1总占用不超层宽
        summary = self.service.year_summary(UW, 2026)
        self.assertLessEqual(summary["layers"][0]["reserved_accepted_gross"], 4_000_000.0 + 0.01)
        # 同批次号、不同内容必须拒绝，防止串批
        with self.assertRaises(Conflict):
            self.service.batch_reserve(CO, {"batch_id": "BATCH-1", "items": items[:2]})

    def test_legacy_missing_date_pending_review_until_backfilled(self):
        # 旧数据：缺发生日、事件未补录发生日（version=0）
        self.service.register_event(CAT, {"event_number": "CAT-OLD"})
        claim = self.service.register_claim(CO, {"claim_number": "CLM-OLD", "approved_loss": 3_000_000, "event_number": "CAT-OLD"})
        self.assertEqual(claim["status"], PENDING_REVIEW)
        self.assertIn("发生日", claim["review_reason"])

        # 核定尝试被拒，停在待核对
        with self.assertRaises(ValidationError):
            self.service.reserve_claim(CO, "CLM-OLD")
        self.assertEqual(self.service.get_claim(CO, "CLM-OLD")["status"], PENDING_REVIEW)

        # 回填：事件补录发生日即版本变为1，赔案可核定
        self.service.amend_event(CAT, "CAT-OLD", {"event_date": "2026-07-01"})
        self.service.reserve_claim(CO, "CLM-OLD")
        view = self.service.get_claim(CO, "CLM-OLD")
        self.assertEqual(view["status"], RESERVED)
        self.assertIsNone(view["review_reason"])

        # 补不全（年份未配置）仍然停在待核对
        claim2 = self.service.register_claim(CO, {"claim_number": "CLM-NOYEAR", "approved_loss": 1_000_000, "occurrence_date": "1999-03-01"})
        self.assertEqual(claim2["status"], PENDING_REVIEW)

    def test_late_claim_uses_occurrence_year_not_intake_year(self):
        # 2025 年度无分层配置；2026 已配置。赔案2026年发生但晚到录入，仍进2026台账
        self._event("CAT-LATE", "2026-02-01")
        claim = self._claim("CLM-LATE", 2_000_000, event="CAT-LATE")
        self.assertEqual(claim["treaty_year_id"], 1)
        reserved = self._reserve("CLM-LATE")
        self.assertEqual(reserved["treaty_year_id"], 1)
        # 100万毛额被免赔吃掉
        self.assertEqual(reserved["accepted_recovery"], 0.0)


if __name__ == "__main__":
    unittest.main()
