"""全谷物标签合规领域模型：配方明细、证据与可计算组成。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class DomainError(Exception):
    """业务规则被违反。"""


class NotFoundError(DomainError):
    """查询对象不存在。"""


# 产品状态（与领域契约 states 对齐）
STATUS_DRAFT = "配方草拟"
STATUS_IN_REVIEW = "试产核验"
STATUS_LABEL_OK = "允许使用标签"
STATUS_TRANSITION = "过渡销售"
STATUS_RECHECK = "抽检复核"
STATUS_RECALL = "召回中"
STATUS_BLOCKED = "禁止流通"

# 企业不得直接填报最终比例，只能提交配方明细由系统计算
FORBIDDEN_DIRECT_RATIO_FIELDS = (
    "whole_grain_ratio",
    "whole_grain_percentage",
    "final_percentage",
    "declared_ratio",
)


def reject_direct_ratio(payload):
    """拒绝任何试图绕过明细、直接填报最终比例的请求。"""
    hits = sorted(key for key in FORBIDDEN_DIRECT_RATIO_FIELDS if key in payload)
    if hits:
        raise DomainError("全谷物比例必须由配方明细与证据计算，不允许直接填报: " + ", ".join(hits))


class Refining(str, Enum):
    """谷物原料的精制方式。"""

    WHOLE = "whole"  # 全谷物原料，仅轻度加工
    REFINED = "refined"  # 精制原料，麸皮与胚芽已去除
    RECONSTITUTED = "reconstituted"  # 精制后按比例回填麸皮胚芽


class EvidenceKind(str, Enum):
    """证据类型：实验室结果与生产损耗。"""

    LAB_RESULT = "lab_result"
    PRODUCTION_LOSS = "production_loss"


class ReviewTrigger(str, Enum):
    """分别触发审核的四类事件。"""

    TRIAL_PRODUCTION = "trial_production"  # 试产
    MASS_PRODUCTION = "mass_production"  # 量产
    RECIPE_CHANGE = "recipe_change"  # 改配方
    SUPPLIER_CHANGE = "supplier_change"  # 换供应商


@dataclass(frozen=True)
class SupplyBatch:
    """原料供应批次。"""

    supply_batch_id: str
    supplier_id: str
    ingredient_id: str
    received_at: str


@dataclass(frozen=True)
class Evidence:
    """带来源的检测或损耗证据，录入后不可更改。"""

    evidence_id: str
    kind: EvidenceKind
    subject_id: str  # 供应批次号或配方版本号
    source: dict  # 来源信息：实验室、检测方法、产线等
    ranges: dict  # 数值区间，如 {"whole_grain_fraction": [0.96, 1.0]}
    recorded_at: str


@dataclass(frozen=True)
class IngredientLine:
    """配方中一行谷物原料投料。"""

    ingredient_id: str
    name: str
    grain_type: str
    refining: Refining
    ratio: float  # 投料比例（占配方总质量）
    ratio_tolerance: float  # 投料允差
    supply_batch_id: str


@dataclass(frozen=True)
class RecipeVersion:
    """配方版本，全谷物组成由明细行计算得出。"""

    recipe_id: str
    product_id: str
    product_category: str
    region: str
    version: int
    lines: tuple
    created_at: str


@dataclass(frozen=True)
class LineContribution:
    """单行原料对全谷物比例的贡献。"""

    ingredient_id: str
    supply_batch_id: str
    share: list  # 投料占比区间 [min, max]
    whole_fraction: list  # 全谷物含量区间 [min, max]
    evidence_id: Optional[str]


@dataclass(frozen=True)
class CompositionResult:
    """由配方明细与证据计算出的全谷物组成。"""

    recipe_id: str
    ratio_range: list  # 全谷物占比区间 [min, max]
    contributions: tuple
    evidence_ids: tuple
    loss_range: list
    computed_at: str


def compute_composition(recipe, lab_index, loss_evidence, now):
    """按投料比例区间与供应批次检测区间计算全谷物占比区间。

    lab_index: 供应批次号 → 最新实验室证据；loss_evidence: 该配方的生产损耗证据。
    精制原料不计入全谷物；其余原料必须持有实验室检测证据，否则拒绝计算。
    """
    contributions = []
    evidence_ids = []
    whole_lo = whole_hi = total_lo = total_hi = 0.0
    for line in recipe.lines:
        share_lo = max(0.0, line.ratio - line.ratio_tolerance)
        share_hi = line.ratio + line.ratio_tolerance
        if line.refining is Refining.REFINED:
            fraction = [0.0, 0.0]
            evidence_id = None
        else:
            evidence = lab_index.get(line.supply_batch_id)
            if evidence is None:
                raise DomainError(f"供应批次 {line.supply_batch_id} 缺少全谷物含量检测证据")
            fraction = evidence.ranges["whole_grain_fraction"]
            evidence_id = evidence.evidence_id
            evidence_ids.append(evidence_id)
        whole_lo += share_lo * fraction[0]
        whole_hi += share_hi * fraction[1]
        total_lo += share_lo
        total_hi += share_hi
        contributions.append(
            LineContribution(
                ingredient_id=line.ingredient_id,
                supply_batch_id=line.supply_batch_id,
                share=[share_lo, share_hi],
                whole_fraction=list(fraction),
                evidence_id=evidence_id,
            )
        )
    if total_hi <= 0.0:
        raise DomainError("配方投料比例之和必须为正")
    if loss_evidence is not None:
        loss_range = loss_evidence.ranges["loss_rate"]
        evidence_ids.append(loss_evidence.evidence_id)
    else:
        loss_range = [0.0, 0.0]
    ratio_lo = whole_lo / total_hi * (1.0 - loss_range[1])
    ratio_hi = whole_hi / total_lo * (1.0 - loss_range[0])
    return CompositionResult(
        recipe_id=recipe.recipe_id,
        ratio_range=[ratio_lo, ratio_hi],
        contributions=tuple(contributions),
        evidence_ids=tuple(evidence_ids),
        loss_range=list(loss_range),
        computed_at=now,
    )


@dataclass(frozen=True)
class Review:
    """一次审核，定论后不可再改。"""

    review_id: str
    trigger: ReviewTrigger
    recipe_id: str
    status: str  # pending / approved / rejected
    created_at: str
    approver: Optional[str] = None
    decided_at: Optional[str] = None
    note: str = ""


@dataclass(frozen=True)
class PackagingBatch:
    """一个包装批次，登记时锁定当时适用的标准。"""

    batch_code: str
    recipe_id: str
    design_version: str
    produced_at: str
    quantity: int
    standard_rule_id: str
    is_old_stock: bool


@dataclass(frozen=True)
class TransitionWindow:
    """获准的旧包装过渡期。"""

    window_id: str
    recipe_id: str
    design_version: str
    approved_by: str
    valid_from: str
    valid_until: str
    reason: str


@dataclass(frozen=True)
class SamplingReport:
    """抽检报告，原始记录不可覆盖。"""

    report_id: str
    batch_code: str
    agency: str
    method: str
    measured_ratio: float
    deviation: float  # 与配方计算区间的偏离，区间内为 0
    created_at: str


@dataclass(frozen=True)
class Appeal:
    """企业针对抽检报告的申辩，仅作关联追加。"""

    appeal_id: str
    report_id: str
    company: str
    argument: str
    created_at: str


@dataclass(frozen=True)
class RecallDecision:
    """召回决定，仅作关联追加。"""

    recall_id: str
    report_id: str
    batch_code: str
    decided_by: str
    scope: str
    created_at: str


@dataclass(frozen=True)
class MarketingClaim:
    """一句营养或节粮宣传，绑定配方版本。"""

    claim_id: str
    text: str
    recipe_id: str
    registered_by: str
    created_at: str
