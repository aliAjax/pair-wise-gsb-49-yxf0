"""合约年度台账计算引擎：按发生日排序重算年度累计与层恢复。

纯函数模块，不做任何I/O。输入合约年度参数与赔案列表，输出每条赔案的
台账行（预占/确认/释放）以及合约年度合计。所有金额保留两位小数。
"""
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

# 已结算赔案状态：金额锚定，不参与重算
ANCHOR_STATE = "settled"

EPSILON = 1e-6


def round2(value: float) -> float:
    """金额统一保留两位小数，并吸收浮点尾差。"""
    return round(float(value) + 1e-9, 2)


def layer_gross(approved_loss: float, attachment: float, width: float, cession_pct: float) -> float:
    """赔案毛摊回：起赔点之上、层宽之内，按分出比例计算。与排序无关。"""
    return round2(min(max(0.0, float(approved_loss) - float(attachment)), float(width)) * float(cession_pct))


def reinstatements_needed(cumulative_usage: float, instance_capacity: float, cap: int) -> int:
    """累计耗用对应的恢复次数：首次用满层宽不计恢复，之后每跨一层宽计一次。"""
    if cumulative_usage <= 0 or instance_capacity <= 0:
        return 0
    return min(int(cap), max(0, int(ceil((float(cumulative_usage) - EPSILON) / float(instance_capacity))) - 1))


@dataclass
class LedgerLine:
    """单条台账行：某赔案在本次重算中的落位结果。"""

    claim_id: int
    claim_number: str
    occurrence_date: str
    event_id: str
    event_version: Optional[int]
    approved_loss: float
    gross_recovery: float
    amount: float               # 预占（未结算）或确认（已结算）摊回
    shortfall: float            # 缺口：落入累计窗口但因层容量耗尽未获摊回的部分
    retained: float             # 年度累计免赔额内/累计限额外的自留部分
    reinstatements_used: int    # 本赔案新消耗的恢复次数
    entry_status: str           # reserved | confirmed | released


@dataclass
class RecalcResult:
    lines: List[LedgerLine] = field(default_factory=list)
    reserved_aggregate: float = 0.0
    confirmed_aggregate: float = 0.0
    reserved_reinstatements: int = 0
    confirmed_reinstatements: int = 0

    def line_for(self, claim_id: int) -> Optional[LedgerLine]:
        for line in self.lines:
            if line.claim_id == claim_id:
                return line
        return None


def ordering_key(claim: Dict[str, Any]) -> Tuple[str, int]:
    """台账顺序：先按发生日，再按赔案主键，保证可重算且稳定。"""
    return (str(claim["occurrence_date"]), int(claim["id"]))


def recalculate(treaty: Dict[str, Any], claims: List[Dict[str, Any]]) -> RecalcResult:
    """按发生日顺序重算一个合约年度的台账。

    treaty字段：layer_attachment/layer_limit/cession_pct/reinstatement_limit/
    aggregate_deductible/aggregate_limit。
    claims中settled赔案金额锚定不动，approved/stale赔案重新计算预占。
    """
    attachment = float(treaty["layer_attachment"])
    width = float(treaty["layer_limit"]) - attachment
    cession_pct = float(treaty["cession_pct"])
    reinstatement_cap = int(treaty["reinstatement_limit"])
    instance_capacity = round2(width * cession_pct)
    total_capacity = round2(instance_capacity * (1 + reinstatement_cap))
    window_start = float(treaty["aggregate_deductible"])
    window_end = window_start + float(treaty["aggregate_limit"])

    result = RecalcResult()
    aggregate_before = 0.0   # 之前赔案毛摊回累计，用于定位年度累计窗口
    layer_before = 0.0       # 之前赔案实际摊回累计，用于定位层容量与恢复次数

    for claim in sorted(claims, key=ordering_key):
        gross = layer_gross(claim["approved_loss"], attachment, width, cession_pct)
        if claim["state"] == ANCHOR_STATE:
            # 已结算：金额与恢复次数锚定不动，只推进游标
            amount = round2(claim["confirmed_amount"])
            reinstatements = int(claim["reinstatements_confirmed"])
            line = LedgerLine(
                claim_id=int(claim["id"]),
                claim_number=str(claim["claim_number"]),
                occurrence_date=str(claim["occurrence_date"]),
                event_id=str(claim["event_id"]),
                event_version=claim.get("event_version"),
                approved_loss=float(claim["approved_loss"]),
                gross_recovery=gross,
                amount=amount,
                shortfall=0.0,
                retained=round2(gross - amount),
                reinstatements_used=reinstatements,
                entry_status="confirmed",
            )
            result.confirmed_aggregate = round2(result.confirmed_aggregate + amount)
            result.confirmed_reinstatements += reinstatements
        else:
            # 未结算：毛摊回先落入年度累计窗口，再受层剩余容量约束
            window_amount = round2(
                max(0.0, min(round2(aggregate_before + gross), window_end) - max(aggregate_before, window_start))
            )
            available = round2(total_capacity - layer_before)
            amount = round2(min(window_amount, max(0.0, available)))
            shortfall = round2(window_amount - amount)
            retained = round2(gross - window_amount)
            reinstatements = reinstatements_needed(
                round2(layer_before + amount), instance_capacity, reinstatement_cap
            ) - reinstatements_needed(layer_before, instance_capacity, reinstatement_cap)
            line = LedgerLine(
                claim_id=int(claim["id"]),
                claim_number=str(claim["claim_number"]),
                occurrence_date=str(claim["occurrence_date"]),
                event_id=str(claim["event_id"]),
                event_version=claim.get("event_version"),
                approved_loss=float(claim["approved_loss"]),
                gross_recovery=gross,
                amount=amount,
                shortfall=shortfall,
                retained=retained,
                reinstatements_used=reinstatements,
                entry_status="reserved",
            )
            result.reserved_aggregate = round2(result.reserved_aggregate + amount)
            result.reserved_reinstatements += reinstatements
        aggregate_before = round2(aggregate_before + gross)
        layer_before = round2(layer_before + line.amount)
        result.lines.append(line)
    return result
