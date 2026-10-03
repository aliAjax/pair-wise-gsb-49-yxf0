"""可重算摊回台账：领域常量、类型与纯函数重算引擎。

台账（ledger）只依赖三类输入：
1. 合约年度的分层配置（起赔点、层宽、成数、恢复次数、恢复保费费率）与年度累计免赔额；
2. 巨灾事件的发生日与版本（事件只影响“未结算”赔案的排序，见 ledger_service）；
3. 赔案的发生日、核定损失与生命周期状态。

关键约定：
- 赔案按 (occurrence_date, claim_number) 确定性排序，晚到赔案按发生日插入正确年度/顺序，
  不会因为录入晚而沿用错误年度的层恢复。
- 已结算赔案是“冻结池”：其已确认的免赔额消耗与层占用先从年度池中扣除，重算时金额永不变动；
  未结算赔案在剩余池内按新顺序重算。
- 容量不足时不抛错，输出 accepted（放行）与 gap（缺口）两个金额，由调用方决定提示。
"""
from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Optional

# 赔案生命周期
PENDING_REVIEW = "pending_review"  # 待核对：缺发生日或事件版本，不进台账
OPEN = "open"                      # 已登记，尚未核定
RESERVED = "reserved"              # 已核定：预占年度累计免赔额与恢复次数
SETTLED = "settled"                # 已结算：消耗确认，金额冻结
INVALIDATED = "invalidated"        # 失效：事件撤回/版本变更后释放预占，可重新核定

# 台账分录状态
ENTRY_RESERVED = "reserved"
ENTRY_CONFIRMED = "confirmed"
ENTRY_SUPERSEDED = "superseded"

# 事件状态
EVENT_ACTIVE = "active"
EVENT_WITHDRAWN = "withdrawn"

CENT = 0.01


def _r(value: float) -> float:
    return round(float(value) + 0.0, 2)


@dataclass(frozen=True)
class LayerSpec:
    layer_no: int
    attachment: float
    width: float
    cession_pct: float
    reinstatements: int
    reinstatement_pct: float

    @property
    def capacity(self) -> float:
        """一个合约年度内该层可被占用的总毛额（基础保障 + 各次恢复）。"""
        return self.width * (1 + self.reinstatements)


@dataclass(frozen=True)
class OpenClaim:
    """参与重算的一笔未结算赔案。"""
    claim_id: int
    claim_number: str
    occurrence_date: str
    approved_loss: float


@dataclass(frozen=True)
class ConfirmedPool:
    """已结算赔案冻结下来的池占用（按层汇总）。"""
    accepted_gross: float = 0.0
    deductible_applied: float = 0.0
    reinstatement_premium: float = 0.0


@dataclass(frozen=True)
class LayerAllocation:
    layer_no: int
    gross_layer_loss: float        # 落入该层的毛损失（受层宽截断）
    deductible_applied: float      # 本笔承担的年度累计免赔额
    eligible_gross: float          # 超过免赔额后有资格摊回的毛额
    accepted_gross: float          # 层容量内放行的毛额
    gap_gross: float               # 容量不足造成的缺口毛额
    accepted_recovery: float       # 放行摊回（乘成数后）
    gap_amount: float              # 缺口摊回（乘成数后）
    reinstatement_premium: float
    reinstatements_used: int       # 截至本笔该层累计已用掉的恢复次数


@dataclass
class ClaimLedger:
    claim_id: int
    claim_number: str
    occurrence_date: str
    allocations: List[LayerAllocation] = field(default_factory=list)

    @property
    def accepted_recovery(self) -> float:
        return _r(sum(item.accepted_recovery for item in self.allocations))

    @property
    def gap_amount(self) -> float:
        return _r(sum(item.gap_amount for item in self.allocations))

    @property
    def reinstatement_premium(self) -> float:
        return _r(sum(item.reinstatement_premium for item in self.allocations))


@dataclass
class YearLedger:
    layers: List[LayerSpec]
    claim_ledgers: List[ClaimLedger]
    aggregate_deductible_total: float
    aggregate_deductible_remaining: float
    accepted_gross_by_layer: Dict[int, float]
    confirmed: Dict[int, ConfirmedPool]

    def for_claim(self, claim_id: int) -> Optional[ClaimLedger]:
        for ledger in self.claim_ledgers:
            if ledger.claim_id == claim_id:
                return ledger
        return None


def gross_in_layer(loss: float, layer: LayerSpec) -> float:
    """单根赔案在某一层上的毛损失：超过起赔点、且不超过层宽。"""
    return min(max(loss - layer.attachment, 0.0), layer.width)


def _reinstatement_premium(used_before: float, accepted_gross: float, layer: LayerSpec) -> float:
    """恢复保费只对落入“恢复片”[k*width, (k+1)*width)（k>=1）的占用计提，基础保障片不计提。"""
    if accepted_gross <= CENT or layer.reinstatement_pct <= 0:
        return 0.0
    used_after = used_before + accepted_gross
    premium_gross = 0.0
    slice_no = 1
    while slice_no * layer.width < used_after - 1e-9 and slice_no <= layer.reinstatements:
        low = slice_no * layer.width
        high = low + layer.width
        premium_gross += max(0.0, min(used_after, high) - max(used_before, low))
        slice_no += 1
    return _r(premium_gross * layer.cession_pct * layer.reinstatement_pct)


def recompute_year(
    layers: List[LayerSpec],
    aggregate_deductible: float,
    open_claims: List[OpenClaim],
    confirmed: Optional[Dict[int, ConfirmedPool]] = None,
) -> YearLedger:
    """对一个合约年度做确定性重算。

    步骤：
    1. 已结算赔案先按冻结金额扣除免赔额与各层容量（已结算金额不动）；
    2. 未结算赔案按 (发生日, 赔案号) 排序，逐笔在剩余池内分配；
    3. 年度累计免赔额沿分层梯子自下而上消耗；容量不足部分记为缺口。
    """
    layers = sorted(layers, key=lambda item: (item.attachment, item.layer_no))
    confirmed = confirmed or {}

    deductible_remaining = max(0.0, float(aggregate_deductible))
    accepted_by_layer: Dict[int, float] = {}
    for layer in layers:
        frozen = confirmed.get(layer.layer_no, ConfirmedPool())
        deductible_remaining = max(0.0, deductible_remaining - frozen.deductible_applied)
        accepted_by_layer[layer.layer_no] = frozen.accepted_gross

    ordered = sorted(open_claims, key=lambda item: (item.occurrence_date, item.claim_number))
    claim_ledgers: List[ClaimLedger] = []
    for claim in ordered:
        claim_ledger = ClaimLedger(claim.claim_id, claim.claim_number, claim.occurrence_date)
        for layer in layers:
            gross = _r(gross_in_layer(claim.approved_loss, layer))
            deductible = _r(min(gross, deductible_remaining))
            deductible_remaining = _r(max(0.0, deductible_remaining - deductible))
            eligible = _r(gross - deductible)

            used_before = accepted_by_layer[layer.layer_no]
            capacity_remaining = max(0.0, layer.capacity - used_before)
            accepted = _r(min(eligible, capacity_remaining))
            gap = _r(eligible - accepted)
            accepted_by_layer[layer.layer_no] = _r(used_before + accepted)

            used_after = accepted_by_layer[layer.layer_no]
            reinstatements_used = min(layer.reinstatements, max(0, ceil(used_after / layer.width) - 1)) if used_after > CENT else 0
            premium = _reinstatement_premium(used_before, accepted, layer)

            claim_ledger.allocations.append(
                LayerAllocation(
                    layer_no=layer.layer_no,
                    gross_layer_loss=gross,
                    deductible_applied=deductible,
                    eligible_gross=eligible,
                    accepted_gross=accepted,
                    gap_gross=gap,
                    accepted_recovery=_r(accepted * layer.cession_pct),
                    gap_amount=_r(gap * layer.cession_pct),
                    reinstatement_premium=premium,
                    reinstatements_used=reinstatements_used,
                )
            )
        claim_ledgers.append(claim_ledger)

    return YearLedger(
        layers=layers,
        claim_ledgers=claim_ledgers,
        aggregate_deductible_total=_r(aggregate_deductible),
        aggregate_deductible_remaining=_r(deductible_remaining),
        accepted_gross_by_layer={no: _r(value) for no, value in accepted_by_layer.items()},
        confirmed=confirmed,
    )
