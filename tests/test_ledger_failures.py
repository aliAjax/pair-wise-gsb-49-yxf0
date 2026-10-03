"""并发、事件失效、批次续作与旧数据回填测试。"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_ledger_service
from src.domain import Actor, Conflict

UW = Actor("uw1", "underwriter")
CO = Actor("co1", "claims_officer")
FIN = Actor("fin1", "finance")

TREATY = {
    "treaty_id": "TRT-2026", "year": 2026,
    "aggregate_deductible": 1000000, "aggregate_limit": 4000000,
    "layer_attachment": 500000, "layer_limit": 2500000,
    "cession_pct": 0.5, "reinstatement_limit": 2,
}


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_ledger_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _treaty(self, **overrides):
        data = dict(TREATY)
        data.update(overrides)
        return self.service.register_treaty(UW, data)

    def _approve(self, number, treaty_id, event_id, loss, occurrence_date=None):
        payload = {"claim_number": number, "treaty_id": treaty_id, "event_id": event_id}
        if occurrence_date:
            payload["occurrence_date"] = occurrence_date
        claim = self.service.submit_claim(CO, payload)
        return self.service.approve_claim(CO, claim["id"], claim["version"], {"approved_loss": loss})


class ConcurrencyTest(LedgerCase):
    def test_last_reinstatement_allows_only_one_claim(self):
        # 层容量 = 1M * (1次恢复) = 2M；基础赔案先占 1M，只剩最后一次恢复的 1M
        self._treaty(aggregate_deductible=0, reinstatement_limit=1)
        self.service.register_event(UW, {"event_id": "CAT-01", "occurrence_date": "2026-07-15"})
        base = self._approve("CLM-BASE", "TRT-2026", "CAT-01", 3500000, "2026-07-15")
        self.assertEqual(base["reserved_amount"], 1000000.0)

        racers = []
        for number, date in (("CLM-R1", "2026-07-16"), ("CLM-R2", "2026-07-17")):
            racers.append(self.service.submit_claim(CO, {"claim_number": number, "treaty_id": "TRT-2026",
                                                         "event_id": "CAT-01", "occurrence_date": date}))
        barrier = threading.Barrier(3)
        errors = {}

        def race(claim):
            try:
                barrier.wait(timeout=10)
                self.service.approve_claim(CO, claim["id"], claim["version"], {"approved_loss": 3500000})
            except Exception as exc:  # noqa: BLE001 - 测试需要捕获线程内任何异常
                errors[claim["claim_number"]] = exc

        threads = [threading.Thread(target=race, args=(claim,)) for claim in racers]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, {})
        # 并发落账后只放行一个：最终台账里一个拿满 1M，另一个返回缺口
        final = {c["claim_number"]: c for c in self.service.list_claims(CO, treaty_id="TRT-2026")}
        reserved = sorted(final[n]["reserved_amount"] for n in ("CLM-R1", "CLM-R2"))
        self.assertEqual(reserved, [0.0, 1000000.0])
        loser = [n for n in ("CLM-R1", "CLM-R2") if final[n]["reserved_amount"] == 0.0][0]
        self.assertEqual(final[loser]["shortfall"], 1000000.0)
        # 恢复次数不被两个赔案同时消耗
        treaty = self.service.get_treaty(CO, "TRT-2026")
        self.assertEqual(treaty["reserved_aggregate"], 2000000.0)
        self.assertEqual(treaty["reserved_reinstatements"], 1)

    def test_sequential_approval_returns_shortfall(self):
        # 顺序核定：后到的赔案响应里直接返回缺口
        self._treaty(aggregate_deductible=0, reinstatement_limit=1)
        self.service.register_event(UW, {"event_id": "CAT-01", "occurrence_date": "2026-07-15"})
        self._approve("CLM-BASE", "TRT-2026", "CAT-01", 3500000, "2026-07-15")
        winner = self._approve("CLM-W", "TRT-2026", "CAT-01", 3500000, "2026-07-16")
        self.assertEqual(winner["reserved_amount"], 1000000.0)
        loser = self._approve("CLM-L", "TRT-2026", "CAT-01", 3500000, "2026-07-17")
        self.assertEqual(loser["reserved_amount"], 0.0)
        self.assertEqual(loser["shortfall"], 1000000.0)


class EventChangeTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self._treaty()
        self.service.register_event(UW, {"event_id": "CAT-01", "occurrence_date": "2026-07-15"})

    def test_withdrawal_invalidates_unsettled_but_keeps_settled(self):
        first = self._approve("CLM-1", "TRT-2026", "CAT-01", 3500000, "2026-07-15")  # 落在免赔额内
        second = self._approve("CLM-2", "TRT-2026", "CAT-01", 3500000, "2026-07-16")  # 预占1M
        third = self._approve("CLM-3", "TRT-2026", "CAT-01", 3500000, "2026-07-17")   # 预占1M
        settled = self.service.settle_claim(FIN, second["id"], second["version"], {"payment_reference": "PAY-1"})
        self.assertEqual(settled["confirmed_amount"], 1000000.0)

        event = self.service.withdraw_event(UW, "CAT-01")
        self.assertEqual(event["status"], "withdrawn")

        first = self.service.get_claim(CO, first["id"])
        third = self.service.get_claim(CO, third["id"])
        anchored = self.service.get_claim(CO, second["id"])
        self.assertEqual(first["state"], "stale")   # 未结算失效
        self.assertEqual(third["state"], "stale")
        self.assertEqual(third["reserved_amount"], 0.0)
        self.assertEqual(anchored["state"], "settled")  # 已结算金额不动
        self.assertEqual(anchored["confirmed_amount"], 1000000.0)

        treaty = self.service.get_treaty(CO, "TRT-2026")
        self.assertEqual(treaty["reserved_aggregate"], 0.0)
        self.assertEqual(treaty["confirmed_aggregate"], 1000000.0)

        ledger = self.service.get_ledger(CO, "TRT-2026")
        released = [e["claim_number"] for e in ledger["entries"] if e["entry_status"] == "released"]
        self.assertEqual(sorted(released), ["CLM-1", "CLM-3"])

    def test_correction_reorders_ledger_by_new_date(self):
        # CLM-1缺发生日（旧数据），核定时继承事件日7/15；CLM-2自报7/14
        inherited = self._approve("CLM-1", "TRT-2026", "CAT-01", 3500000)
        self.assertEqual(inherited["occurrence_date"], "2026-07-15")
        reported = self._approve("CLM-2", "TRT-2026", "CAT-01", 3500000, "2026-07-14")
        # 当前顺序 CLM-2(7/14) 先耗免赔额，CLM-1 进入窗口
        self.assertEqual(self.service.get_claim(CO, inherited["id"])["reserved_amount"], 1000000.0)
        self.assertEqual(self.service.get_claim(CO, reported["id"])["reserved_amount"], 0.0)

        # 事件日更正为7/10：继承日期的赔案跟随，自报日期不动，台账按新顺序重算
        self.service.correct_event(UW, "CAT-01", {"occurrence_date": "2026-07-10"})
        inherited = self.service.get_claim(CO, inherited["id"])
        reported = self.service.get_claim(CO, reported["id"])
        self.assertEqual(inherited["occurrence_date"], "2026-07-10")
        self.assertEqual(inherited["reserved_amount"], 0.0)   # 现在它先耗免赔额
        self.assertEqual(reported["occurrence_date"], "2026-07-14")
        self.assertEqual(reported["reserved_amount"], 1000000.0)

    def test_correction_keeps_settled_amounts(self):
        early = self._approve("CLM-1", "TRT-2026", "CAT-01", 3500000, "2026-07-15")
        late = self._approve("CLM-2", "TRT-2026", "CAT-01", 3500000, "2026-07-16")
        settled = self.service.settle_claim(FIN, late["id"], late["version"], {"payment_reference": "PAY-1"})
        self.service.correct_event(UW, "CAT-01", {"occurrence_date": "2026-07-10"})
        anchored = self.service.get_claim(CO, settled["id"])
        self.assertEqual(anchored["confirmed_amount"], 1000000.0)  # 已结算不动
        self.assertEqual(anchored["state"], "settled")

    def test_event_backfill_revalidates_pending_claim(self):
        self._treaty(treaty_id="TRT-ZERO", aggregate_deductible=0)
        # 事件尚未补录：核定只能停在待核对
        claim = self.service.submit_claim(CO, {"claim_number": "CLM-G", "treaty_id": "TRT-ZERO", "event_id": "CAT-99"})
        claim = self.service.approve_claim(CO, claim["id"], claim["version"], {"approved_loss": 3500000})
        self.assertEqual(claim["state"], "pending_review")
        self.assertEqual(claim["reserved_amount"], 0.0)
        # 事件补录后：未结算赔案自动重校验并进台账
        event = self.service.register_event(UW, {"event_id": "CAT-99", "occurrence_date": "2026-07-15"})
        self.assertIsNotNone(event["recalc_job_id"])
        claim = self.service.get_claim(CO, claim["id"])
        self.assertEqual(claim["state"], "approved")
        self.assertEqual(claim["occurrence_date"], "2026-07-15")
        self.assertEqual(claim["event_version"], 1)
        self.assertEqual(claim["reserved_amount"], 1000000.0)

    def test_event_version_follows_correction_and_settled_keeps_old(self):
        claim = self._approve("CLM-1", "TRT-2026", "CAT-01", 3500000, "2026-07-16")
        settled = self._approve("CLM-2", "TRT-2026", "CAT-01", 3500000, "2026-07-17")
        settled = self.service.settle_claim(FIN, settled["id"], settled["version"], {"payment_reference": "PAY-1"})
        self.assertEqual(settled["event_version"], 1)
        # 事件更正后版本递增：未结算赔案跟随，已结算赔案保留旧版本作历史
        self.service.correct_event(UW, "CAT-01", {"occurrence_date": "2026-07-10"})
        event = self.service.get_event(CO, "CAT-01")
        self.assertEqual(event["version"], 2)
        claim = self.service.get_claim(CO, claim["id"])
        settled = self.service.get_claim(CO, settled["id"])
        self.assertEqual(claim["event_version"], 2)
        self.assertEqual(settled["event_version"], 1)
        self.assertEqual(settled["confirmed_amount"], 1000000.0)

    def test_manual_recalculate_is_idempotent(self):
        self._approve("CLM-1", "TRT-2026", "CAT-01", 3500000, "2026-07-15")
        before = self.service.get_treaty(CO, "TRT-2026")
        job = self.service.recalculate(CO, "TRT-2026")
        self.assertEqual(job["status"], "completed")
        job = self.service.recalculate(CO, "TRT-2026")
        self.assertEqual(job["status"], "completed")
        after = self.service.get_treaty(CO, "TRT-2026")
        # 重复重算不改变累计：计数是绝对值而非累加
        self.assertEqual(before["reserved_aggregate"], after["reserved_aggregate"])
        self.assertEqual(before["reserved_reinstatements"], after["reserved_reinstatements"])


class BatchResumeTest(LedgerCase):
    def test_resume_after_write_failure_without_double_counting(self):
        self._treaty(treaty_id="TRT-A", aggregate_deductible=0)
        self._treaty(treaty_id="TRT-B", aggregate_deductible=0)
        self.service.register_event(UW, {"event_id": "CAT-02", "occurrence_date": "2026-07-15"})
        self._approve("CLM-A", "TRT-A", "CAT-02", 3500000, "2026-07-15")
        self._approve("CLM-B", "TRT-B", "CAT-02", 3500000, "2026-07-16")

        # 注入一次性写入失败：TRT-B 的台账任务第一次执行时抛错
        original = self.service._recalc_treaty_tx
        failed_once = []

        def flaky(connection, treaty_id, job_id, reason, actor_id):
            if treaty_id == "TRT-B" and not failed_once:
                failed_once.append(treaty_id)
                raise sqlite3.OperationalError("模拟写入失败")
            return original(connection, treaty_id, job_id, reason, actor_id)

        self.service._recalc_treaty_tx = flaky
        self.service.withdraw_event(UW, "CAT-02")

        job = self.service.get_job(CO, self.service.list_jobs(CO)[0]["id"])
        self.assertEqual(job["status"], "failed")
        states = {task["treaty_id"]: task["status"] for task in job["tasks"]}
        self.assertEqual(states, {"TRT-A": "completed", "TRT-B": "failed"})
        # TRT-B 尚未重算：旧预占还在
        self.assertEqual(self.service.get_treaty(CO, "TRT-B")["reserved_aggregate"], 1000000.0)

        def batch_count(treaty_id):
            with sqlite3.connect(self.db_path) as conn:
                return conn.execute("SELECT COUNT(*) FROM ledger_batches WHERE treaty_id=?", (treaty_id,)).fetchone()[0]

        batches_a = batch_count("TRT-A")
        # 从完整批次续作：已完成的 TRT-A 不重复执行
        self.service._recalc_treaty_tx = original
        job = self.service.resume_job(CO, job["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(batch_count("TRT-A"), batches_a)
        # TRT-B 重算完成，预占释放且只算一次
        self.assertEqual(self.service.get_treaty(CO, "TRT-B")["reserved_aggregate"], 0.0)
        claim_b = [c for c in self.service.list_claims(CO, treaty_id="TRT-B")][0]
        self.assertEqual(claim_b["state"], "stale")
        self.assertEqual(claim_b["reserved_amount"], 0.0)


class LegacyBackfillTest(LedgerCase):
    def test_backfill_sweep_and_pending_review(self):
        self._treaty()
        self.service.register_event(UW, {"event_id": "CAT-01", "occurrence_date": "2026-07-15"})
        # 旧数据三种形态：缺事件版本 / 缺发生日 / 事件号不存在
        self.service.submit_claim(CO, {"claim_number": "CLM-A", "treaty_id": "TRT-2026",
                                       "event_id": "CAT-01", "occurrence_date": "2026-07-16"})
        self.service.submit_claim(CO, {"claim_number": "CLM-B", "treaty_id": "TRT-2026", "event_id": "CAT-01"})
        self.service.submit_claim(CO, {"claim_number": "CLM-C", "treaty_id": "TRT-2026", "event_id": "GHOST"})

        report = self.service.backfill_claims(CO, {})
        self.assertEqual(report["fixed"], 2)
        self.assertEqual(report["pending_review"], ["CLM-C"])

        claims = {c["claim_number"]: c for c in self.service.list_claims(CO, treaty_id="TRT-2026")}
        self.assertEqual(claims["CLM-A"]["event_version"], 1)
        self.assertEqual(claims["CLM-A"]["state"], "submitted")
        self.assertEqual(claims["CLM-B"]["occurrence_date"], "2026-07-15")
        self.assertEqual(claims["CLM-B"]["occurrence_source"], "inherited")
        self.assertEqual(claims["CLM-C"]["state"], "pending_review")  # 补不全停在待核对

        # 事件补录后 CLM-C 可以回填并重新核定
        self.service.register_event(UW, {"event_id": "GHOST", "occurrence_date": "2026-07-18"})
        claims = {c["claim_number"]: c for c in self.service.list_claims(CO, treaty_id="TRT-2026")}
        self.assertEqual(claims["CLM-C"]["state"], "submitted")
        self.assertEqual(claims["CLM-C"]["occurrence_date"], "2026-07-18")

    def test_stale_version_and_double_settle_rejected(self):
        self._treaty(aggregate_deductible=0)
        self.service.register_event(UW, {"event_id": "CAT-01", "occurrence_date": "2026-07-15"})
        claim = self.service.submit_claim(CO, {"claim_number": "CLM-V", "treaty_id": "TRT-2026",
                                               "event_id": "CAT-01", "occurrence_date": "2026-07-15"})
        with self.assertRaises(Conflict):
            self.service.approve_claim(CO, claim["id"], claim["version"] + 1, {"approved_loss": 1000000})
        claim = self.service.approve_claim(CO, claim["id"], claim["version"], {"approved_loss": 3500000})
        claim = self.service.get_claim(CO, claim["id"])
        settled = self.service.settle_claim(FIN, claim["id"], claim["version"], {"payment_reference": "PAY-1"})
        self.assertEqual(settled["state"], "settled")
        with self.assertRaises(Conflict):
            self.service.settle_claim(FIN, claim["id"], settled["version"], {"payment_reference": "PAY-2"})


if __name__ == "__main__":
    unittest.main()
