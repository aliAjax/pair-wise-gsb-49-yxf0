"""台账用例编排：核定预占、结算确认、事件失效重算、批次续作与旧数据回填。"""
from typing import Any, Dict, List, Optional, Tuple

from .domain import Actor, Conflict, PermissionDenied, ValidationError
from .ledger import RecalcResult, recalculate
from .ledger_rules import LedgerRules
from .repository import Repository


def _changed(claim: Dict[str, Any], fields: Dict[str, Any]) -> bool:
    for key, value in fields.items():
        current = claim.get(key)
        if isinstance(value, float) or isinstance(current, float):
            if current is None or value is None:
                if current != value:
                    return True
            elif abs(float(current) - float(value)) > 0.005:
                return True
        elif current != value:
            return True
    return False


class LedgerService:
    def __init__(self, repository: Repository, rules: LedgerRules = None) -> None:
        self.repository = repository
        self.rules = rules or LedgerRules()

    # ------------------------------------------------------------------
    # 身份与角色
    # ------------------------------------------------------------------
    def _actor(self, actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")
        return actor

    def _require(self, actor: Actor, action: str) -> Actor:
        actor = self._actor(actor)
        if not self.rules.role_can(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        return actor

    @staticmethod
    def _check_version(entity: Dict[str, Any], expected_version: int) -> None:
        if int(entity["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")

    # ------------------------------------------------------------------
    # 合约年度
    # ------------------------------------------------------------------
    def register_treaty(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require(actor, "register_treaty")
        payload = self.rules.validate_treaty(data or {})
        return self.repository.create_treaty_year(payload, actor.user_id)

    def get_treaty(self, actor: Actor, treaty_id: str) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_treaty_year(treaty_id)

    def list_treaties(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_treaty_years()

    def get_ledger(self, actor: Actor, treaty_id: str) -> Dict[str, Any]:
        self._actor(actor)
        treaty = self.repository.get_treaty_year(treaty_id)
        return {"treaty": treaty, "entries": self.repository.latest_batch_entries(treaty_id)}

    # ------------------------------------------------------------------
    # 巨灾事件
    # ------------------------------------------------------------------
    def register_event(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require(actor, "register_event")
        payload = self.rules.validate_event(data or {})
        event = self.repository.create_event(payload, actor.user_id)
        # 事件补录：引用该事件的赔案可能正停在待核对，触发相关合约年度重算
        job = self._start_recalc("event_registered:%s" % event["event_id"], actor, self._treaty_ids_of_event(event["event_id"]))
        event["recalc_job_id"] = job["id"] if job else None
        return event

    def correct_event(self, actor: Actor, event_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require(actor, "correct_event")
        payload = self.rules.validate_event_correction(data or {})
        event = self.repository.update_event_occurrence(event_id, payload["occurrence_date"], actor.user_id)
        job = self._start_recalc("event_corrected:%s" % event_id, actor, self._treaty_ids_of_event(event_id))
        event["recalc_job_id"] = job["id"] if job else None
        return event

    def withdraw_event(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._require(actor, "withdraw_event")
        event = self.repository.withdraw_event(event_id, actor.user_id)
        job = self._start_recalc("event_withdrawn:%s" % event_id, actor, self._treaty_ids_of_event(event_id))
        event["recalc_job_id"] = job["id"] if job else None
        return event

    def get_event(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_event(event_id)

    def list_events(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_events(status=status)

    def _treaty_ids_of_event(self, event_id: str) -> List[str]:
        claims = self.repository.list_claims(event_id=event_id, limit=1000)
        return sorted({claim["treaty_id"] for claim in claims})

    # ------------------------------------------------------------------
    # 赔案
    # ------------------------------------------------------------------
    def submit_claim(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require(actor, "submit_claim")
        payload = self.rules.validate_claim(data or {})
        self.repository.get_treaty_year(payload["treaty_id"])
        return self.repository.create_claim(payload, actor.user_id)

    def get_claim(self, actor: Actor, claim_id: int) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_claim(claim_id)

    def list_claims(self, actor: Actor, treaty_id: Optional[str] = None, event_id: Optional[str] = None, state: Optional[str] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_claims(treaty_id=treaty_id, event_id=event_id, state=state)

    def claim_audit(self, actor: Actor, claim_id: int) -> List[Dict[str, Any]]:
        self._actor(actor)
        self.repository.get_claim(claim_id)
        return self.repository.ledger_audit("claim", str(claim_id))

    def claim_ledger(self, actor: Actor, claim_id: int) -> List[Dict[str, Any]]:
        self._actor(actor)
        self.repository.get_claim(claim_id)
        return self.repository.claim_ledger_entries(claim_id)

    def approve_claim(self, actor: Actor, claim_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """核定：按发生日预占年度累计与恢复次数；容量不足时返回缺口。"""
        actor = self._require(actor, "approve_claim")
        payload = self.rules.validate_approval(data or {})
        with self.repository.transaction() as connection:
            claim = self.repository.claim_tx(connection, claim_id)
            self._check_version(claim, expected_version)
            self.rules.require_claim_action(claim, "approve")
            treaty = self.repository.treaty_year_tx(connection, claim["treaty_id"])
            event = self.repository.event_tx(connection, claim["event_id"])
            fields = {"approved_loss": payload["approved_loss"]}
            disposition, updates = self._revalidate(treaty, {**claim, **fields}, event)
            fields.update(updates)
            if disposition == "participate":
                fields["state"] = "approved"
            else:
                # 事件缺失/已撤回/发生日缺或跨年度：停在待核对，不占额度
                fields.update({"state": "pending_review", "reserved_amount": 0.0, "reinstatements_reserved": 0, "shortfall": 0.0})
            self.repository.update_claim_tx(connection, claim_id, fields, actor.user_id, "approve", {"expected_version": expected_version})
            if disposition == "participate":
                self._recalc_treaty_tx(connection, treaty["treaty_id"], None, "approve_claim:%s" % claim["claim_number"], actor.user_id)
        return self.repository.get_claim(claim_id)

    def settle_claim(self, actor: Actor, claim_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """结算：确认消耗年度累计与恢复次数，金额此后锚定不动。"""
        actor = self._require(actor, "settle_claim")
        payload = self.rules.validate_settlement(data or {})
        with self.repository.transaction() as connection:
            claim = self.repository.claim_tx(connection, claim_id)
            self._check_version(claim, expected_version)
            self.rules.require_claim_action(claim, "settle")
            if float(claim["reserved_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            fields = {
                "state": "settled",
                "confirmed_amount": claim["reserved_amount"],
                "reinstatements_confirmed": claim["reinstatements_reserved"],
                "payment_reference": payload["payment_reference"],
            }
            self.repository.update_claim_tx(connection, claim_id, fields, actor.user_id, "settle", {"expected_version": expected_version})
            self._recalc_treaty_tx(connection, claim["treaty_id"], None, "settle_claim:%s" % claim["claim_number"], actor.user_id)
        return self.repository.get_claim(claim_id)

    def reject_claim(self, actor: Actor, claim_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require(actor, "reject_claim")
        payload = self.rules.validate_rejection(data or {})
        with self.repository.transaction() as connection:
            claim = self.repository.claim_tx(connection, claim_id)
            self._check_version(claim, expected_version)
            self.rules.require_claim_action(claim, "reject")
            fields = {"state": "rejected", "reject_reason": payload["reject_reason"], "reserved_amount": 0.0, "reinstatements_reserved": 0, "shortfall": 0.0}
            self.repository.update_claim_tx(connection, claim_id, fields, actor.user_id, "reject", {"expected_version": expected_version})
            if claim["state"] in ("approved", "stale"):
                self._recalc_treaty_tx(connection, claim["treaty_id"], None, "reject_claim:%s" % claim["claim_number"], actor.user_id)
        return self.repository.get_claim(claim_id)

    # ------------------------------------------------------------------
    # 旧数据回填
    # ------------------------------------------------------------------
    def backfill_claims(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        """缺发生日或事件版本的旧赔案：能补则补，补不全停在待核对。"""
        actor = self._require(actor, "backfill_claims")
        treaty_filter = (data or {}).get("treaty_id")
        fixed, pending, affected = 0, [], set()
        for claim in self.repository.claims_missing_backfill():
            if treaty_filter and claim["treaty_id"] != treaty_filter:
                continue
            event = self.repository.find_event(claim["event_id"])
            treaty = self.repository.get_treaty_year(claim["treaty_id"])
            with self.repository.transaction() as connection:
                disposition, updates = self._revalidate(treaty, claim, event)
                if disposition in ("participate", "submitted"):
                    fields = dict(updates)
                    if claim["state"] == "pending_review":
                        fields["state"] = "approved" if disposition == "participate" else "submitted"
                    if fields:
                        self.repository.update_claim_tx(connection, claim["id"], fields, actor.user_id, "backfill", {"source": "legacy_sweep"})
                    fixed += 1
                    if claim["state"] in ("approved", "stale", "pending_review") and claim.get("approved_loss") is not None:
                        affected.add(claim["treaty_id"])
                else:
                    if claim["state"] != "pending_review":
                        self.repository.update_claim_tx(connection, claim["id"], {"state": "pending_review"}, actor.user_id, "backfill", {"source": "legacy_sweep"})
                    pending.append(claim["claim_number"])
        job = self._start_recalc("backfill_claims", actor, sorted(affected))
        return {"fixed": fixed, "pending_review": pending, "recalc_job_id": job["id"] if job else None}

    # ------------------------------------------------------------------
    # 重算批次：执行与续作
    # ------------------------------------------------------------------
    def recalculate(self, actor: Actor, treaty_id: str) -> Dict[str, Any]:
        actor = self._require(actor, "recalculate")
        self.repository.get_treaty_year(treaty_id)
        job = self._start_recalc("manual_recalculate", actor, [treaty_id])
        return job

    def resume_job(self, actor: Actor, job_id: int) -> Dict[str, Any]:
        """写入失败后从完整批次续作：已完成任务跳过，未完成任务重跑。"""
        actor = self._require(actor, "resume_job")
        job = self.repository.get_recalc_job(job_id)
        if job["status"] == "completed":
            return job
        self.repository.reset_failed_tasks(job_id)
        return self._run_recalc_job(job_id, actor.user_id)

    def get_job(self, actor: Actor, job_id: int) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_recalc_job(job_id)

    def list_jobs(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_recalc_jobs()

    def _start_recalc(self, reason: str, actor: Actor, treaty_ids: List[str]) -> Optional[Dict[str, Any]]:
        if not treaty_ids:
            return None
        job = self.repository.create_recalc_job(reason, actor.user_id, treaty_ids)
        return self._run_recalc_job(job["id"], actor.user_id)

    def _run_recalc_job(self, job_id: int, actor_id: str) -> Dict[str, Any]:
        job = self.repository.get_recalc_job(job_id)
        self.repository.mark_job_running(job_id)
        for task in self.repository.pending_recalc_tasks(job_id):
            try:
                with self.repository.transaction() as connection:
                    self._recalc_treaty_tx(connection, task["treaty_id"], job_id, job["reason"], actor_id)
            except Exception as exc:
                self.repository.fail_recalc_task(job_id, task["treaty_id"], str(exc))
                return self.repository.get_recalc_job(job_id)
        self.repository.complete_recalc_job(job_id)
        return self.repository.get_recalc_job(job_id)

    # ------------------------------------------------------------------
    # 重算核心：重校验 + 引擎 + 落库（单事务）
    # ------------------------------------------------------------------
    def _revalidate(self, treaty: Dict[str, Any], claim: Dict[str, Any], event: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        """核对赔案的事件与发生日。返回(处置, 字段更新)。

        处置：participate（可进台账）/ release（失效释放）/ pending（待核对）/ submitted（退回待核定）。
        """
        updates: Dict[str, Any] = {}
        if event is None or event["status"] != "active":
            return ("release", updates) if claim["state"] in ("approved", "stale") else ("pending", updates)
        # 自报发生日不动；继承自事件的发生日跟随事件更正
        own_date = claim["occurrence_date"] if claim.get("occurrence_source") == "reported" else None
        occurrence = own_date or event["occurrence_date"]
        if claim["occurrence_date"] != occurrence:
            updates["occurrence_date"] = occurrence
        if claim.get("occurrence_source") != "reported" and occurrence:
            updates["occurrence_source"] = "inherited"
        if claim["event_version"] != event["version"]:
            updates["event_version"] = event["version"]
        if int(str(occurrence)[:4]) != int(treaty["year"]):
            # 发生日不在合约年度内：数据待核对，不进台账
            return "pending", updates
        if claim.get("approved_loss") is None:
            return "submitted", updates
        return "participate", updates

    def _recalc_treaty_tx(self, connection, treaty_id: str, job_id: Optional[int], reason: str, actor_id: str) -> RecalcResult:
        treaty = self.repository.treaty_year_tx(connection, treaty_id)
        claims = self.repository.treaty_claims_tx(connection, treaty_id)
        participants: List[Dict[str, Any]] = []
        released_lines: List[Dict[str, Any]] = []
        pending_updates: Dict[int, Dict[str, Any]] = {}

        for claim in claims:
            if claim["state"] == "settled":
                participants.append(claim)  # 已结算金额锚定不动
                continue
            event = self.repository.event_tx(connection, claim["event_id"])
            disposition, updates = self._revalidate(treaty, claim, event)
            if disposition == "participate":
                merged = {**claim, **updates, "state": "approved"}
                participants.append(merged)
                pending_updates[claim["id"]] = {**updates, "state": "approved"}
            elif disposition == "release":
                fields = {**updates, "state": "stale", "reserved_amount": 0.0, "reinstatements_reserved": 0, "shortfall": 0.0}
                pending_updates[claim["id"]] = fields
                released_lines.append(self._released_line(claim, event))
            else:
                target = "pending_review" if disposition == "pending" else "submitted"
                fields = {**updates, "reserved_amount": 0.0, "reinstatements_reserved": 0, "shortfall": 0.0}
                if claim["state"] != target:
                    fields["state"] = target
                if claim["state"] in ("approved", "stale"):
                    released_lines.append(self._released_line(claim, event))
                pending_updates[claim["id"]] = fields

        result = recalculate(treaty, participants)
        for line in result.lines:
            if line.entry_status != "reserved":
                continue
            fields = pending_updates.setdefault(line.claim_id, {})
            fields.update({
                "reserved_amount": line.amount,
                "reinstatements_reserved": line.reinstatements_used,
                "shortfall": line.shortfall,
            })

        for claim in claims:
            fields = pending_updates.get(claim["id"])
            if fields and _changed(claim, fields):
                self.repository.update_claim_tx(connection, claim["id"], fields, actor_id, "recalculate", {"reason": reason})

        entries = [self._entry_from_line(line) for line in result.lines] + released_lines
        totals = {
            "reserved_aggregate": result.reserved_aggregate,
            "confirmed_aggregate": result.confirmed_aggregate,
            "reserved_reinstatements": result.reserved_reinstatements,
            "confirmed_reinstatements": result.confirmed_reinstatements,
        }
        batch_id = self.repository.persist_recalc_tx(connection, treaty_id, job_id, reason, entries, totals, actor_id)
        if job_id is not None:
            self.repository.complete_recalc_task_tx(connection, job_id, treaty_id, batch_id)
        return result

    @staticmethod
    def _entry_from_line(line) -> Dict[str, Any]:
        return {
            "claim_id": line.claim_id,
            "claim_number": line.claim_number,
            "occurrence_date": line.occurrence_date,
            "event_id": line.event_id,
            "event_version": line.event_version,
            "gross_recovery": line.gross_recovery,
            "reserved_amount": line.amount if line.entry_status == "reserved" else 0.0,
            "confirmed_amount": line.amount if line.entry_status == "confirmed" else 0.0,
            "reinstatements_used": line.reinstatements_used,
            "shortfall": line.shortfall,
            "entry_status": line.entry_status,
        }

    @staticmethod
    def _released_line(claim: Dict[str, Any], event: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        occurrence = claim["occurrence_date"] or (event["occurrence_date"] if event else None)
        return {
            "claim_id": claim["id"],
            "claim_number": claim["claim_number"],
            "occurrence_date": occurrence,
            "event_id": claim["event_id"],
            "event_version": claim["event_version"],
            "gross_recovery": 0.0,
            "reserved_amount": 0.0,
            "confirmed_amount": 0.0,
            "reinstatements_used": 0,
            "shortfall": 0.0,
            "entry_status": "released",
        }
