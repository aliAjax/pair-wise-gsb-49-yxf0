"""台账用例编排：合约年度、巨灾事件、赔案与可重算摊回台账。

两阶段消耗：
    reserve（核定）→ 预占年度累计免赔额与层恢复次数（reserved）
    settle（结算） → 预占转确认（confirmed），金额冻结，任何后续重算都不再改动

事件版本：
    补录/修正发生日或撤回事件都会让事件 version+1；已结算赔案作为冻结池保留，
    未结算赔案的旧预占整体作废，按新的 (发生日, 赔案号) 顺序重算。

可续作批次：
    每个批次按 batch_id 幂等，操作以 (batch_id, op_key) 落盘；
    写入失败后拿完整批次重试时跳过已完成项，台账由“替换式重算”产生，不会重复累计。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .domain import Actor, Conflict, PermissionDenied, ValidationError, integer, number, text
from .ledger import (
    ENTRY_CONFIRMED,
    ENTRY_RESERVED,
    EVENT_WITHDRAWN,
    INVALIDATED,
    OPEN,
    PENDING_REVIEW,
    RESERVED,
    SETTLED,
    ClaimLedger,
    ConfirmedPool,
    LayerAllocation,
    LayerSpec,
    OpenClaim,
    recompute_year,
)
from .ledger_repository import LedgerRepository

ROLES = {
    "underwriter",          # 合约年度与分层
    "catastrophe_analyst",  # 巨灾事件
    "claims_officer",       # 赔案
    "finance",              # 结算
}
ACTION_ROLES = {
    "setup_treaty_year": {"underwriter"},
    "register_event": {"catastrophe_analyst"},
    "amend_event": {"catastrophe_analyst"},
    "withdraw_event": {"catastrophe_analyst"},
    "register_claim": {"claims_officer"},
    "backfill_claim": {"claims_officer"},
    "reserve_claim": {"claims_officer"},
    "release_claim": {"claims_officer"},
    "settle_claim": {"finance"},
    "recompute": {"claims_officer", "catastrophe_analyst"},
    "batch_reserve": {"claims_officer"},
}


def parse_date(value: Any, key: str = "occurrence_date") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    value = value.strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc
    return value


class LedgerService:
    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository

    # ---- 身份与权限 ----
    def _actor(self, actor: Optional[Actor]) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _can(self, actor: Actor, action: str) -> None:
        if actor.role != "admin" and actor.role not in ACTION_ROLES.get(action, set()):
            raise PermissionDenied("角色无权执行%s" % action)

    @staticmethod
    def _year_of(date_text: Optional[str]) -> Optional[int]:
        if not date_text:
            return None
        return int(date_text[:4])

    def _event_for(self, conn, claim: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not claim.get("event_id"):
            return None
        return self.repository.get_event(conn, int(claim["event_id"]))

    def _resolved_date(self, claim: Dict[str, Any], event: Optional[Dict[str, Any]]) -> Optional[str]:
        return claim.get("occurrence_date") or (event.get("event_date") if event else None)

    def _resolve_event_key(self, conn, key: Any) -> Dict[str, Any]:
        if isinstance(key, bool):
            raise ValidationError("事件标识无效")
        if isinstance(key, int) or (isinstance(key, str) and key.strip().isdigit()):
            return self.repository.get_event(conn, int(key))
        if isinstance(key, str) and key.strip():
            return self.repository.get_event_by_number(conn, key.strip())
        raise ValidationError("事件标识无效")

    def _review_reason(self, conn, claim: Dict[str, Any], event: Optional[Dict[str, Any]], date_text: Optional[str]) -> Optional[str]:
        if not date_text:
            return "缺发生日：无法归入合约年度"
        if event is not None and int(event.get("version", 0)) < 1:
            return "事件版本缺失：发生日尚未补录"
        if event is not None and event.get("status") == EVENT_WITHDRAWN:
            return "关联事件已撤回"
        year = self.repository.get_year_by_calendar(conn, self._year_of(date_text))
        if year is None:
            return "合约年度%s未配置" % self._year_of(date_text)
        if not year["layers"]:
            return "合约年度%s未配置分层" % self._year_of(date_text)
        return None

    # ---- 台账重算（替换式，不追加累计）----
    def _materialize_year(self, conn, year_id: int, batch_id: str, actor_id: str):
        year = self.repository.get_year(conn, year_id)
        layers = [
            LayerSpec(
                layer_no=int(row["layer_no"]),
                attachment=float(row["attachment"]),
                width=float(row["width"]),
                cession_pct=float(row["cession_pct"]),
                reinstatements=int(row["reinstatements"]),
                reinstatement_pct=float(row["reinstatement_pct"]),
            )
            for row in year["layers"]
        ]
        if not layers:
            return {}, None

        confirmed: Dict[int, ConfirmedPool] = {}
        rows = conn.execute(
            """SELECT layer_no,
                      SUM(accepted_gross) AS accepted_gross,
                      SUM(deductible_applied) AS deductible_applied,
                      SUM(reinstatement_premium) AS reinstatement_premium
               FROM ledger_entries WHERE treaty_year_id=? AND status=? GROUP BY layer_no""",
            (year_id, ENTRY_CONFIRMED),
        ).fetchall()
        for row in rows:
            confirmed[int(row["layer_no"])] = ConfirmedPool(
                accepted_gross=float(row["accepted_gross"] or 0.0),
                deductible_applied=float(row["deductible_applied"] or 0.0),
                reinstatement_premium=float(row["reinstatement_premium"] or 0.0),
            )

        open_claims: List[OpenClaim] = []
        event_versions: Dict[int, Optional[int]] = {}
        for claim in self.repository.list_claims(conn, status=RESERVED, year_id=year_id):
            event = self._event_for(conn, claim)
            date_text = self._resolved_date(claim, event)
            if not date_text:
                continue
            open_claims.append(
                OpenClaim(
                    claim_id=int(claim["id"]),
                    claim_number=claim["claim_number"],
                    occurrence_date=date_text,
                    approved_loss=float(claim["approved_loss"]),
                )
            )
            event_versions[int(claim["id"])] = int(event["version"]) if event else None

        year_ledger = recompute_year(layers, float(year["aggregate_deductible"]), open_claims, confirmed)
        self.repository.supersede_reserved(conn, year_id, batch_id)
        mapping: Dict[int, ClaimLedger] = {}
        for claim_ledger in year_ledger.claim_ledgers:
            mapping[claim_ledger.claim_id] = claim_ledger
            for allocation in claim_ledger.allocations:
                self.repository.insert_entry(
                    conn,
                    year_id=year_id,
                    claim_id=claim_ledger.claim_id,
                    layer_no=allocation.layer_no,
                    event_version=event_versions.get(claim_ledger.claim_id),
                    allocation=allocation,
                    batch_id=batch_id,
                    status=ENTRY_RESERVED,
                )
        self.repository.audit(
            conn, "treaty_year", year_id, "recomputed", actor_id,
            {"batch_id": batch_id, "reserved_claims": len(open_claims)},
        )
        return mapping, year_ledger

    # ---- 合约年度 ----
    def setup_treaty_year(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "setup_treaty_year")
        year = integer(payload or {}, "year", 1900, 2999)
        aggregate_deductible = number(payload, "aggregate_deductible", 0)
        raw_layers = payload.get("layers", [])
        if not isinstance(raw_layers, list) or not raw_layers:
            raise ValidationError("至少配置一个分层")
        layer_nos = set()
        normalized = []
        for item in raw_layers:
            if not isinstance(item, dict):
                raise ValidationError("分层配置必须是对象")
            layer_no = integer(item, "layer_no", 1)
            if layer_no in layer_nos:
                raise ValidationError("层号不能重复：%s" % layer_no)
            layer_nos.add(layer_no)
            attachment = number(item, "attachment", 0)
            width = number(item, "width", 0.0001)
            cession_pct = number(item, "cession_pct", 0, 1)
            reinstatements = integer(item, "reinstatements", 0, 99)
            reinstatement_pct = number(item, "reinstatement_pct", 0, 1)
            normalized.append((layer_no, attachment, width, cession_pct, reinstatements, reinstatement_pct))
        with self.repository.tx() as conn:
            created = self.repository.create_treaty_year(conn, year, aggregate_deductible, actor.user_id)
            for args in normalized:
                self.repository.add_layer(conn, created["id"], *args, actor.user_id)
            return self.repository.get_year(conn, created["id"])

    def list_treaty_years(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        with self.repository.tx() as conn:
            return self.repository.list_years(conn)

    def year_summary(self, actor: Actor, year_key: Any) -> Dict[str, Any]:
        self._actor(actor)
        with self.repository.tx() as conn:
            year = self._get_year(conn, year_key)
            entries = self.repository.current_entries(conn, year_id=int(year["id"]))
            layers: Dict[int, Dict[str, Any]] = {}
            for row in year["layers"]:
                layers[int(row["layer_no"])] = {
                    "layer_no": int(row["layer_no"]),
                    "attachment": float(row["attachment"]),
                    "width": float(row["width"]),
                    "reinstatements": int(row["reinstatements"]),
                    "confirmed_accepted_gross": 0.0,
                    "reserved_accepted_gross": 0.0,
                    "gap_gross": 0.0,
                    "reinstatements_used": 0,
                    "reinstatement_premium_confirmed": 0.0,
                    "reinstatement_premium_reserved": 0.0,
                }
            deductible_confirmed = 0.0
            deductible_reserved = 0.0
            recovery_confirmed = 0.0
            recovery_reserved = 0.0
            for entry in entries:
                bucket = layers.setdefault(int(entry["layer_no"]), {"layer_no": int(entry["layer_no"])})
                if entry["status"] == ENTRY_CONFIRMED:
                    bucket["confirmed_accepted_gross"] += float(entry["accepted_gross"])
                    bucket["reinstatement_premium_confirmed"] += float(entry["reinstatement_premium"])
                    deductible_confirmed += float(entry["deductible_applied"])
                    recovery_confirmed += float(entry["accepted_recovery"])
                else:
                    bucket["reserved_accepted_gross"] += float(entry["accepted_gross"])
                    bucket["gap_gross"] += float(entry["gap_gross"])
                    bucket["reinstatement_premium_reserved"] += float(entry["reinstatement_premium"])
                    bucket["reinstatements_used"] = max(bucket.get("reinstatements_used", 0), int(entry["reinstatements_used"]))
                    deductible_reserved += float(entry["deductible_applied"])
                    recovery_reserved += float(entry["accepted_recovery"])
                if entry["status"] == ENTRY_CONFIRMED:
                    bucket["reinstatements_used"] = max(bucket.get("reinstatements_used", 0), int(entry["reinstatements_used"]))
            total_aad = float(year["aggregate_deductible"])
            return {
                "treaty_year": year,
                "aggregate_deductible": round(total_aad, 2),
                "aggregate_deductible_confirmed": round(deductible_confirmed, 2),
                "aggregate_deductible_reserved": round(deductible_reserved, 2),
                "aggregate_deductible_remaining": round(max(0.0, total_aad - deductible_confirmed - deductible_reserved), 2),
                "recovery_confirmed": round(recovery_confirmed, 2),
                "recovery_reserved": round(recovery_reserved, 2),
                "layers": [
                    {key: (round(value, 2) if isinstance(value, float) else value) for key, value in bucket.items()}
                    for bucket in (layers[no] for no in sorted(layers))
                ],
            }

    def _get_year(self, conn, year_key: Any) -> Dict[str, Any]:
        if isinstance(year_key, bool):
            raise ValidationError("年度标识无效")
        if isinstance(year_key, int):
            # 整数优先解释为合约日历年；查不到再按数据库主键处理
            year = self.repository.get_year_by_calendar(conn, year_key)
            if year is not None:
                return year
            return self.repository.get_year(conn, year_key)
        if isinstance(year_key, str) and year_key.strip():
            key = year_key.strip()
            if key.isdigit():
                year = self.repository.get_year_by_calendar(conn, int(key))
                if year is not None:
                    return year
                return self.repository.get_year(conn, int(key))
            raise ValidationError("合约年度不存在：%s" % year_key)
        raise ValidationError("年度标识无效")

    # ---- 巨灾事件 ----
    def register_event(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "register_event")
        event_number = text(payload or {}, "event_number")
        raw_date = payload.get("event_date")
        event_date = parse_date(raw_date, "event_date") if raw_date else None
        with self.repository.tx() as conn:
            return self.repository.create_event(conn, event_number, event_date, actor.user_id)

    def list_events(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        with self.repository.tx() as conn:
            return self.repository.list_events(conn)

    def _amend_event(self, conn, event_key: Any, event_date: Optional[str], actor_id: str) -> Dict[str, Any]:
        event = self._resolve_event_key(conn, event_key)
        event = self.repository.update_event(conn, int(event["id"]), event_date, "active", actor_id, bump_version=True)
        affected_years = set()
        for claim in self.repository.list_claims(conn):
            if not claim.get("event_id") or int(claim["event_id"]) != int(event["id"]):
                continue
            if claim["status"] == SETTLED:
                continue  # 已结算金额与归属冻结，永不动
            old_year = claim.get("treaty_year_id")
            if old_year:
                affected_years.add(int(old_year))
            new_year_id = None
            if event_date:
                year_row = self.repository.get_year_by_calendar(conn, self._year_of(event_date))
                if year_row is not None and year_row["layers"]:
                    new_year_id = int(year_row["id"])
                    affected_years.add(new_year_id)
            fields: Dict[str, Any] = {"occurrence_date": event_date, "treaty_year_id": new_year_id}
            if claim["status"] == OPEN:
                pass  # 尚未核定，保持 open
            elif claim["status"] == PENDING_REVIEW:
                # 原来因数据不全停在待核对：数据补齐后回到 open，等待人工核定
                fields["status"] = OPEN
                fields["review_reason"] = None
            elif new_year_id is not None:
                fields["status"] = RESERVED if claim["status"] in (RESERVED, INVALIDATED) else claim["status"]
                fields["review_reason"] = None
            else:
                fields["status"] = PENDING_REVIEW
                fields["review_reason"] = "事件补录后仍无法归入已配置合约年度"
            self.repository.update_claim(conn, int(claim["id"]), actor_id, **fields)
        return event, affected_years

    def amend_event(self, actor: Actor, event_key: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "amend_event")
        raw_date = (payload or {}).get("event_date")
        event_date = parse_date(raw_date, "event_date") if raw_date else None
        with self.repository.tx() as conn:
            event, affected_years = self._amend_event(conn, event_key, event_date, actor.user_id)
            for year_id in sorted(affected_years):
                self._materialize_year(conn, year_id, "event-amend:%s:v%s" % (event["id"], event["version"]), actor.user_id)
            self.repository.audit(conn, "cat_event", int(event["id"]), "amended", actor.user_id, {"version": event["version"], "event_date": event_date})
            return self.repository.get_event(conn, int(event["id"]))

    def withdraw_event(self, actor: Actor, event_key: Any, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "withdraw_event")
        reason = text(payload or {}, "reason") if (payload or {}).get("reason") else "事件撤回"
        with self.repository.tx() as conn:
            event = self._resolve_event_key(conn, event_key)
            event = self.repository.update_event(conn, int(event["id"]), event["event_date"], EVENT_WITHDRAWN, actor.user_id, bump_version=True)
            affected_years = set()
            for claim in self.repository.list_claims(conn):
                if not claim.get("event_id") or int(claim["event_id"]) != int(event["id"]):
                    continue
                if claim["status"] == SETTLED:
                    continue  # 已结算金额不动
                if claim.get("treaty_year_id"):
                    affected_years.add(int(claim["treaty_year_id"]))
                self.repository.update_claim(
                    conn, int(claim["id"]), actor.user_id,
                    status=INVALIDATED,
                    review_reason="关联事件%s已撤回（%s）" % (event["event_number"], reason),
                )
            for year_id in sorted(affected_years):
                self._materialize_year(conn, year_id, "event-withdraw:%s:v%s" % (event["id"], event["version"]), actor.user_id)
            self.repository.audit(conn, "cat_event", int(event["id"]), "withdrawn", actor.user_id, {"version": event["version"], "reason": reason})
            return self.repository.get_event(conn, int(event["id"]))

    # ---- 赔案 ----
    def register_claim(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "register_claim")
        claim_number = text(payload or {}, "claim_number")
        approved_loss = number(payload, "approved_loss", 0)
        raw_date = payload.get("occurrence_date")
        occurrence_date = parse_date(raw_date) if raw_date else None
        event_number = (payload.get("event_number") or "").strip() if isinstance(payload.get("event_number"), str) else ""
        with self.repository.tx() as conn:
            event = None
            if event_number:
                event = self.repository.get_event_by_number(conn, event_number)
            date_text = occurrence_date or (event["event_date"] if event else None)
            year_id = None
            status = OPEN
            review_reason = None
            if date_text:
                year_row = self.repository.get_year_by_calendar(conn, self._year_of(date_text))
                if year_row is not None:
                    year_id = int(year_row["id"])
            reason = self._review_reason(conn, {"occurrence_date": date_text, "event_id": event["id"] if event else None}, event, date_text)
            if reason:
                status = PENDING_REVIEW
                review_reason = reason
            claim = self.repository.create_claim(
                conn, claim_number, year_id, event["id"] if event else None,
                date_text, approved_loss, status, review_reason, actor.user_id,
            )
            return claim

    def list_claims(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        allowed = {PENDING_REVIEW, OPEN, RESERVED, SETTLED, INVALIDATED}
        if status and status not in allowed:
            raise ValidationError("未知赔案状态：%s" % status)
        with self.repository.tx() as conn:
            return self.repository.list_claims(conn, status=status)

    def get_claim(self, actor: Actor, claim_key: Any) -> Dict[str, Any]:
        self._actor(actor)
        with self.repository.tx() as conn:
            claim = self.repository.resolve_claim(conn, claim_key)
            claim["ledger_entries"] = self.repository.current_entries(conn, claim_id=int(claim["id"]))
            return claim

    def backfill_claim(self, actor: Actor, claim_key: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """旧数据回填：补发生日/事件。补不全就停在待核对；已结算赔案不允许改动。"""
        actor = self._actor(actor)
        self._can(actor, "backfill_claim")
        payload = payload or {}
        with self.repository.tx() as conn:
            claim = self.repository.resolve_claim(conn, claim_key)
            if claim["status"] == SETTLED:
                raise Conflict("已结算赔案金额不动，不能回填")
            if "occurrence_date" in payload:
                raw = payload.get("occurrence_date")
                claim["occurrence_date"] = parse_date(raw) if raw else None
            if "event_number" in payload:
                raw_event = payload.get("event_number")
                if raw_event in (None, ""):
                    claim["event_id"] = None
                else:
                    event = self.repository.get_event_by_number(conn, text(payload, "event_number"))
                    claim["event_id"] = int(event["id"])
            event = self._event_for(conn, claim)
            date_text = self._resolved_date(claim, event)
            old_year = claim.get("treaty_year_id")
            new_year_id = None
            if date_text:
                year_row = self.repository.get_year_by_calendar(conn, self._year_of(date_text))
                if year_row is not None:
                    new_year_id = int(year_row["id"])
            reason = self._review_reason(conn, claim, event, date_text)
            fields: Dict[str, Any] = {"occurrence_date": date_text, "treaty_year_id": new_year_id}
            affected = {int(y) for y in (old_year, new_year_id) if y}
            if reason:
                fields["status"] = PENDING_REVIEW
                fields["review_reason"] = reason
            elif claim["status"] == RESERVED:
                fields["status"] = RESERVED
                fields["review_reason"] = None
            else:
                fields["status"] = OPEN
                fields["review_reason"] = None
            updated = self.repository.update_claim(conn, int(claim["id"]), actor.user_id, **fields)
            for year_id in sorted(affected):
                self._materialize_year(conn, year_id, "backfill:claim:%s" % claim["id"], actor.user_id)
            self.repository.audit(conn, "claim", int(claim["id"]), "backfilled", actor.user_id, {"reason": reason})
            return self.repository.get_claim(conn, int(claim["id"]))

    @staticmethod
    def _serialize_allocation(allocation: LayerAllocation) -> Dict[str, Any]:
        return {
            "layer_no": allocation.layer_no,
            "gross_layer_loss": allocation.gross_layer_loss,
            "deductible_applied": allocation.deductible_applied,
            "eligible_gross": allocation.eligible_gross,
            "accepted_gross": allocation.accepted_gross,
            "gap_gross": allocation.gap_gross,
            "accepted_recovery": allocation.accepted_recovery,
            "gap_amount": allocation.gap_amount,
            "reinstatement_premium": allocation.reinstatement_premium,
            "reinstatements_used": allocation.reinstatements_used,
        }

    def _reserve_claim(self, conn, claim_key: Any, approved_loss: Optional[float], actor_id: str, batch_id: str) -> Dict[str, Any]:
        """核定：先预占。必须在调用方的 IMMEDIATE 事务内执行。"""
        claim = self.repository.resolve_claim(conn, claim_key)
        if claim["status"] == SETTLED:
            raise Conflict("赔案已结算，不能重复核定")
        event = self._event_for(conn, claim)
        if event is not None and event.get("status") == EVENT_WITHDRAWN:
            raise ValidationError("关联事件已撤回，无法核定；请等待事件补录或更正")
        date_text = self._resolved_date(claim, event)
        loss = float(claim["approved_loss"]) if approved_loss is None else approved_loss
        reason = self._review_reason(conn, claim, event, date_text)
        if reason:
            pending_year = None
            if date_text:
                pending_year_row = self.repository.get_year_by_calendar(conn, self._year_of(date_text))
                pending_year = int(pending_year_row["id"]) if pending_year_row else None
            self.repository.update_claim(
                conn, int(claim["id"]), actor_id,
                status=PENDING_REVIEW, review_reason=reason,
                occurrence_date=date_text, treaty_year_id=pending_year,
            )
            self.repository.audit(conn, "claim", int(claim["id"]), "reserve_blocked", actor_id, {"reason": reason, "batch_id": batch_id})
            raise ValidationError("%s，赔案停在待核对" % reason)

        year_row = self.repository.get_year_by_calendar(conn, self._year_of(date_text))
        year_id = int(year_row["id"])
        self.repository.update_claim(
            conn, int(claim["id"]), actor_id,
            status=RESERVED, review_reason=None, occurrence_date=date_text,
            treaty_year_id=year_id, approved_loss=loss,
        )
        mapping, _ = self._materialize_year(conn, year_id, batch_id, actor_id)
        claim_ledger = mapping.get(int(claim["id"]))
        allocations = [self._serialize_allocation(item) for item in (claim_ledger.allocations if claim_ledger else [])]
        gap_amount = round(sum(item["gap_amount"] for item in allocations), 2)
        accepted = round(sum(item["accepted_recovery"] for item in allocations), 2)
        premium = round(sum(item["reinstatement_premium"] for item in allocations), 2)
        result = {
            "claim_id": int(claim["id"]),
            "claim_number": claim["claim_number"],
            "status": RESERVED,
            "occurrence_date": date_text,
            "treaty_year_id": year_id,
            "approved_loss": round(loss, 2),
            "accepted_recovery": accepted,
            "gap_amount": gap_amount,
            "reinstatement_premium_reserved": premium,
            "has_gap": gap_amount > 0.01,
            "allocations": allocations,
        }
        self.repository.audit(conn, "claim", int(claim["id"]), "reserved", actor_id, {"batch_id": batch_id, "accepted_recovery": accepted, "gap_amount": gap_amount})
        return result

    def reserve_claim(self, actor: Actor, claim_key: Any, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "reserve_claim")
        approved_loss = None
        if payload and "approved_loss" in payload:
            approved_loss = number(payload, "approved_loss", 0)
        with self.repository.tx() as conn:
            return self._reserve_claim(conn, claim_key, approved_loss, actor.user_id, "reserve:%s" % claim_key)

    def release_claim(self, actor: Actor, claim_key: Any, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """主动释放预占（赔案作废/撤销）：失效并重算该年度，已结算赔案不允许。"""
        actor = self._actor(actor)
        self._can(actor, "release_claim")
        with self.repository.tx() as conn:
            claim = self.repository.resolve_claim(conn, claim_key)
            if claim["status"] == SETTLED:
                raise Conflict("已结算赔案不能释放")
            if claim["status"] != RESERVED:
                raise Conflict("只有已核定（预占）赔案可以释放")
            year_id = int(claim["treaty_year_id"])
            self.repository.update_claim(conn, int(claim["id"]), actor.user_id, status=INVALIDATED, review_reason="预占已手动释放")
            self._materialize_year(conn, year_id, "release:claim:%s" % claim["id"], actor.user_id)
            self.repository.audit(conn, "claim", int(claim["id"]), "released", actor.user_id, {})
            return self.repository.get_claim(conn, int(claim["id"]))

    def settle_claim(self, actor: Actor, claim_key: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """结算：预占转确认并冻结金额；恢复次数消耗此刻才确认。"""
        actor = self._actor(actor)
        self._can(actor, "settle_claim")
        payment_reference = text(payload or {}, "payment_reference")
        with self.repository.tx() as conn:
            claim = self.repository.resolve_claim(conn, claim_key)
            if claim["status"] != RESERVED:
                raise Conflict("只有已核定（预占中）的赔案可以结算")
            entries = [row for row in self.repository.current_entries(conn, claim_id=int(claim["id"])) if row["status"] == ENTRY_RESERVED]
            accepted = round(sum(float(row["accepted_recovery"]) for row in entries), 2)
            gap = round(sum(float(row["gap_amount"]) for row in entries), 2)
            premium = round(sum(float(row["reinstatement_premium"]) for row in entries), 2)
            if accepted <= 0.01:
                raise ValidationError("无可结算摊回（放行金额为0）")
            self.repository.confirm_claim(conn, int(claim["id"]))
            self.repository.update_claim(conn, int(claim["id"]), actor.user_id, status=SETTLED, review_reason=None)
            self.repository.audit(
                conn, "claim", int(claim["id"]), "settled", actor.user_id,
                {"payment_reference": payment_reference, "confirmed_recovery": accepted, "gap_amount": gap, "reinstatement_premium": premium},
            )
            return {
                "claim_id": int(claim["id"]),
                "claim_number": claim["claim_number"],
                "status": SETTLED,
                "payment_reference": payment_reference,
                "confirmed_recovery": accepted,
                "unrecovered_gap": gap,
                "reinstatement_premium": premium,
                "entries": self.repository.current_entries(conn, claim_id=int(claim["id"])),
            }

    def recompute_year(self, actor: Actor, year_key: Any) -> Dict[str, Any]:
        """显式重算年度（核对用）：只重建预占，已结算金额不动。"""
        actor = self._actor(actor)
        self._can(actor, "recompute")
        with self.repository.tx() as conn:
            year = self._get_year(conn, year_key)
            mapping, year_ledger = self._materialize_year(conn, int(year["id"]), "manual-recompute", actor.user_id)
            return {"year": year["year"], "reserved_claims": len(mapping)}

    # ---- 可续作批次 ----
    def batch_reserve(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._can(actor, "batch_reserve")
        batch_id = text(payload or {}, "batch_id")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("items必须是非空列表")
        items = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict) or "claim" not in item:
                raise ValidationError("第%s项缺少claim标识" % index)
            approved_loss = None
            if "approved_loss" in item:
                approved_loss = number(item, "approved_loss", 0)
            items.append((str(index), item["claim"], approved_loss))
        # 批次头先落盘；随后每项独立事务，已完成项重提时直接跳过。
        # 注意：已完成项清单必须用只读连接读取——写事务在某些隔离级别下看不到
        # “上次崩溃后、当前事务开始前”已提交的其它批次操作。
        with self.repository.tx() as conn:
            self.repository.ensure_batch(conn, batch_id, "reserve_claims", len(items))
        with self.repository._connect() as conn:
            already = self.repository.completed_ops(conn, batch_id)
        results = []
        for op_key, claim_key, approved_loss in items:
            skipped = op_key in already
            if skipped:
                stored = already[op_key]
                # 注意展开顺序：先放存储结果，再用 skipped=True 覆盖其内部的 False
                results.append({"op_key": op_key, "claim": claim_key, **stored["result"], "skipped": True})
                continue
            with self.repository.tx() as conn:
                try:
                    data = self._reserve_claim(conn, claim_key, approved_loss, actor.user_id, "batch:%s" % batch_id)
                    result = {"ok": True, "skipped": False, "data": data}
                except ValidationError as exc:
                    result = {"ok": False, "skipped": False, "error": {"code": exc.code, "message": str(exc)}}
                claim_id = self.repository.resolve_claim(conn, claim_key)["id"]
                self.repository.record_op(conn, batch_id, op_key, int(claim_id), result)
            results.append({"op_key": op_key, "claim": claim_key, **result})
        completed = sum(1 for item in results if item.get("ok") or item.get("error"))
        return {"batch_id": batch_id, "total": len(items), "completed": completed, "results": results}

    def batch_status(self, actor: Actor, batch_id: str) -> Dict[str, Any]:
        self._actor(actor)
        with self.repository.tx() as conn:
            batch = self.repository.get_batch(conn, batch_id)
            if batch is None:
                raise Conflict("批次不存在：%s" % batch_id)
            ops = self.repository.completed_ops(conn, batch_id)
            return {"batch": batch, "completed": len(ops), "ops": list(ops.values())}

    def audit_timeline(self, actor: Actor, entity: Optional[str] = None, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.audit_timeline(entity=entity, entity_id=entity_id)
