"""台账用例的角色、输入校验与赔案状态机。"""
from datetime import datetime
from typing import Any, Dict, Optional

from .domain import Conflict, ValidationError, integer, number, optional_text, text

# 台账动作角色表：admin在LedgerService中统一放行
ACTION_ROLES = {
    "register_treaty": {"underwriter"},
    "register_event": {"underwriter", "claims_officer"},
    "correct_event": {"underwriter", "claims_officer"},
    "withdraw_event": {"underwriter"},
    "submit_claim": {"claims_officer"},
    "approve_claim": {"claims_officer"},
    "settle_claim": {"finance"},
    "reject_claim": {"finance", "claims_officer"},
    "backfill_claims": {"claims_officer"},
    "recalculate": {"underwriter", "claims_officer"},
    "resume_job": {"underwriter", "claims_officer"},
}

# 赔案允许的人工动作：动作 -> 允许的前置状态
CLAIM_ACTION_STATES = {
    "approve": {"submitted", "pending_review"},
    "settle": {"approved"},
    "reject": {"submitted", "approved", "stale", "pending_review"},
}


def iso_date(data: Dict[str, Any], key: str) -> str:
    value = text(data, key)
    return check_date(value, key)


def optional_iso_date(data: Dict[str, Any], key: str) -> Optional[str]:
    value = optional_text(data, key)
    if not value:
        return None
    return check_date(value, key)


def check_date(value: str, key: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD格式的有效日期" % key) from exc
    return value


class LedgerRules:
    def known_role(self, role: str) -> bool:
        all_roles = set()
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_treaty(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["treaty_id"] = text(p, "treaty_id")
        p["year"] = integer(p, "year", 2000, 2100)
        p["aggregate_deductible"] = number(p, "aggregate_deductible", 0)
        p["aggregate_limit"] = number(p, "aggregate_limit", 0)
        if p["aggregate_limit"] <= 0:
            raise ValidationError("aggregate_limit必须大于0")
        p["layer_attachment"] = number(p, "layer_attachment", 0)
        p["layer_limit"] = number(p, "layer_limit", 0)
        if p["layer_limit"] <= p["layer_attachment"]:
            raise ValidationError("layer_limit必须高于layer_attachment")
        p["cession_pct"] = number(p, "cession_pct", 0, 1)
        if p["cession_pct"] <= 0:
            raise ValidationError("cession_pct必须大于0")
        p["reinstatement_limit"] = integer(p, "reinstatement_limit", 0)
        return p

    def validate_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["event_id"] = text(p, "event_id")
        p["occurrence_date"] = iso_date(p, "occurrence_date")
        p["name"] = optional_text(p, "name")
        p["end_date"] = optional_iso_date(p, "end_date")
        if p["end_date"] and p["end_date"] < p["occurrence_date"]:
            raise ValidationError("end_date不能早于occurrence_date")
        return p

    def validate_event_correction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"occurrence_date": iso_date(payload, "occurrence_date")}

    def validate_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["claim_number"] = text(p, "claim_number")
        p["treaty_id"] = text(p, "treaty_id")
        p["event_id"] = text(p, "event_id")
        # 旧数据允许缺发生日与事件版本，后续回填或停在待核对
        p["occurrence_date"] = optional_iso_date(p, "occurrence_date")
        p["occurrence_source"] = "reported" if p["occurrence_date"] else None
        version = p.get("event_version")
        if version is not None:
            p["event_version"] = integer(p, "event_version", 1)
        else:
            p["event_version"] = None
        loss = p.get("approved_loss")
        p["approved_loss"] = number(p, "approved_loss", 0) if loss is not None else None
        return p

    def validate_approval(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"approved_loss": number(payload, "approved_loss", 0)}

    def validate_settlement(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"payment_reference": text(payload, "payment_reference")}

    def validate_rejection(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"reject_reason": text(payload, "reject_reason")}

    def require_claim_action(self, claim: Dict[str, Any], action: str) -> None:
        allowed = CLAIM_ACTION_STATES.get(action, set())
        if claim["state"] not in allowed:
            raise Conflict("赔案当前状态%s不允许执行%s" % (claim["state"], action))
