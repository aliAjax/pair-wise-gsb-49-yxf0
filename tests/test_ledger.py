"""台账引擎与主流程测试：发生日排序、核定预占、结算确认。"""
import tempfile
import unittest
from pathlib import Path

from app import build_ledger_service
from src.domain import Actor, PermissionDenied, ValidationError
from src.ledger import layer_gross, recalculate, reinstatements_needed

UW = Actor("uw1", "underwriter")
CO = Actor("co1", "claims_officer")
FIN = Actor("fin1", "finance")

TREATY = {
    "treaty_id": "TRT-2026", "year": 2026,
    "aggregate_deductible": 1000000, "aggregate_limit": 4000000,
    "layer_attachment": 500000, "layer_limit": 2500000,
    "cession_pct": 0.5, "reinstatement_limit": 2,
}
EVENT = {"event_id": "CAT-01", "occurrence_date": "2026-07-15", "name": "台风海葵"}


class EngineTest(unittest.TestCase):
    def test_layer_gross(self):
        self.assertEqual(layer_gross(3500000, 500000, 2000000, 0.5), 1000000.0)
        self.assertEqual(layer_gross(400000, 500000, 2000000, 0.5), 0.0)
        self.assertEqual(layer_gross(10000000, 500000, 2000000, 0.5), 1000000.0)

    def test_reinstatements_needed(self):
        self.assertEqual(reinstatements_needed(0, 100, 2), 0)
        self.assertEqual(reinstatements_needed(100, 100, 2), 0)
        self.assertEqual(reinstatements_needed(100.01, 100, 2), 1)
        self.assertEqual(reinstatements_needed(200, 100, 2), 1)
        self.assertEqual(reinstatements_needed(250, 100, 2), 2)
        self.assertEqual(reinstatements_needed(999, 100, 2), 2)  # 不超过恢复上限

    def test_recalculate_orders_by_occurrence_date(self):
        treaty = {"layer_attachment": 500000, "layer_limit": 2500000, "cession_pct": 0.5,
                  "reinstatement_limit": 2, "aggregate_deductible": 1000000, "aggregate_limit": 4000000}
        claims = [
            {"id": 1, "claim_number": "LATE", "occurrence_date": "2026-07-20", "event_id": "E", "event_version": 1,
             "approved_loss": 3500000, "state": "approved", "confirmed_amount": 0, "reinstatements_confirmed": 0},
            {"id": 2, "claim_number": "EARLY", "occurrence_date": "2026-07-15", "event_id": "E", "event_version": 1,
             "approved_loss": 3500000, "state": "approved", "confirmed_amount": 0, "reinstatements_confirmed": 0},
        ]
        result = recalculate(treaty, claims)
        self.assertEqual([line.claim_number for line in result.lines], ["EARLY", "LATE"])
        # 早发生赔案先耗年度累计免赔额，晚发生赔案进入摊回窗口
        self.assertEqual(result.lines[0].amount, 0.0)
        self.assertEqual(result.lines[1].amount, 1000000.0)
        self.assertEqual(result.reserved_aggregate, 1000000.0)

    def test_recalculate_shortfall_when_layer_exhausted(self):
        treaty = {"layer_attachment": 500000, "layer_limit": 2500000, "cession_pct": 0.5,
                  "reinstatement_limit": 1, "aggregate_deductible": 0, "aggregate_limit": 10000000}
        claims = [
            {"id": i, "claim_number": "C%d" % i, "occurrence_date": "2026-07-1%d" % i, "event_id": "E", "event_version": 1,
             "approved_loss": 3500000, "state": "approved", "confirmed_amount": 0, "reinstatements_confirmed": 0}
            for i in range(1, 4)
        ]
        result = recalculate(treaty, claims)
        # 层容量 = 1M * (1+1) = 2M，第三笔只能落在窗口内但层已耗尽
        self.assertEqual(result.lines[0].amount, 1000000.0)
        self.assertEqual(result.lines[1].amount, 1000000.0)
        self.assertEqual(result.lines[2].amount, 0.0)
        self.assertEqual(result.lines[2].shortfall, 1000000.0)
        self.assertEqual(result.reserved_reinstatements, 1)


class LedgerWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_ledger_service(str(Path(self.temp.name) / "test.db"))
        self.service.register_treaty(UW, dict(TREATY))
        self.service.register_event(UW, dict(EVENT))

    def tearDown(self):
        self.temp.cleanup()

    def _submit_approve(self, number, occurrence_date, loss):
        claim = self.service.submit_claim(CO, {"claim_number": number, "treaty_id": "TRT-2026",
                                               "event_id": "CAT-01", "occurrence_date": occurrence_date})
        return self.service.approve_claim(CO, claim["id"], claim["version"], {"approved_loss": loss})

    def test_reserve_by_occurrence_date_and_confirm_on_settle(self):
        # 提交顺序与发生日相反：晚到的早发生赔案应挤占年度累计免赔额
        late = self._submit_approve("CLM-LATE", "2026-07-20", 3500000)
        self.assertEqual(late["reserved_amount"], 0.0)  # 当时唯一赔案，落在免赔额内
        early = self._submit_approve("CLM-EARLY", "2026-07-15", 3500000)
        self.assertEqual(early["reserved_amount"], 0.0)
        late_after = self.service.get_claim(CO, late["id"])
        self.assertEqual(late_after["reserved_amount"], 1000000.0)  # 重算后进入窗口

        treaty = self.service.get_treaty(CO, "TRT-2026")
        self.assertEqual(treaty["reserved_aggregate"], 1000000.0)
        self.assertEqual(treaty["confirmed_aggregate"], 0.0)

        # 结算后才确认消耗
        settled = self.service.settle_claim(FIN, late_after["id"], late_after["version"], {"payment_reference": "PAY-1"})
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["confirmed_amount"], 1000000.0)
        treaty = self.service.get_treaty(CO, "TRT-2026")
        self.assertEqual(treaty["reserved_aggregate"], 0.0)
        self.assertEqual(treaty["confirmed_aggregate"], 1000000.0)

    def test_ledger_entries_recorded_in_order(self):
        self._submit_approve("CLM-2", "2026-07-16", 3500000)
        self._submit_approve("CLM-1", "2026-07-15", 3500000)
        ledger = self.service.get_ledger(CO, "TRT-2026")
        numbers = [entry["claim_number"] for entry in ledger["entries"]]
        self.assertEqual(numbers, ["CLM-1", "CLM-2"])

    def test_approve_requires_occurrence_in_treaty_year(self):
        claim = self.service.submit_claim(CO, {"claim_number": "CLM-X", "treaty_id": "TRT-2026",
                                               "event_id": "CAT-01", "occurrence_date": "2025-12-31"})
        claim = self.service.approve_claim(CO, claim["id"], claim["version"], {"approved_loss": 1000000})
        self.assertEqual(claim["state"], "pending_review")  # 跨年度停在待核对
        self.assertEqual(claim["reserved_amount"], 0.0)

    def test_permission_denied_for_unknown_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_treaty(Actor("x", "outsider"), dict(TREATY))

    def test_settle_without_reservation_rejected(self):
        approved = self._submit_approve("CLM-Z", "2026-07-15", 100000)  # 损失低于起赔点
        self.assertEqual(approved["reserved_amount"], 0.0)
        with self.assertRaises(ValidationError):
            self.service.settle_claim(FIN, approved["id"], approved["version"], {"payment_reference": "PAY-9"})


if __name__ == "__main__":
    unittest.main()
