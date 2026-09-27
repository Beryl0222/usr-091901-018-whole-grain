"""全谷物标签合规领域的核心模型与常量。

所有实体均为冻结数据类：记录一旦创建不可修改。抽检报告、企业申辩、
召回决定等事实以仅追加的方式沉淀，状态由记录流推导，从结构上保证
"不得覆盖原报告"的原则。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---- 标准级别（并行更新的三类规则） ----
LEVEL_NATIONAL = "国家标准"
LEVEL_INDUSTRY = "行业标准"
LEVEL_GROUP = "团体规则"
RULE_LEVELS = (LEVEL_NATIONAL, LEVEL_INDUSTRY, LEVEL_GROUP)
LEVEL_RANK = {LEVEL_NATIONAL: 3, LEVEL_INDUSTRY: 2, LEVEL_GROUP: 1}

# ---- 精制方式 ----
REFINE_WHOLE = "全粒"
REFINE_PARTIAL = "部分精制"
REFINE_REFINED = "精制"
REFINE_NONE = "无"
GRAIN_REFINE_METHODS = (REFINE_WHOLE, REFINE_PARTIAL, REFINE_REFINED)

# ---- 证据类型 ----
EVIDENCE_LAB = "lab_result"       # 实验室检测结果
EVIDENCE_LOSS = "production_loss"  # 生产损耗
EVIDENCE_KINDS = (EVIDENCE_LAB, EVIDENCE_LOSS)

# ---- 审核触发类型 ----
TRIGGER_TRIAL = "试产"
TRIGGER_MASS = "量产"
TRIGGER_RECIPE_CHANGE = "改配方"
TRIGGER_SUPPLIER_CHANGE = "换供应商"
REVIEW_TRIGGERS = (TRIGGER_TRIAL, TRIGGER_MASS, TRIGGER_RECIPE_CHANGE, TRIGGER_SUPPLIER_CHANGE)
AUTO_TRIGGERS = (TRIGGER_RECIPE_CHANGE, TRIGGER_SUPPLIER_CHANGE)

DECISION_APPROVE = "通过"
DECISION_REJECT = "驳回"
REVIEW_PENDING = "待审核"
REVIEW_APPROVED = "已通过"
REVIEW_REJECTED = "已驳回"

# ---- 配方版本状态（由审核记录推导） ----
RECIPE_DRAFT = "配方草拟"
RECIPE_TRIAL_VERIFIED = "试产核验"
RECIPE_LABEL_APPROVED = "允许使用标签"

# ---- 生产批次状态（由记录流推导） ----
BATCH_NORMAL = "正常流通"
BATCH_TRANSITION = "过渡销售"
BATCH_INSPECTION = "抽检复核"
BATCH_RECALLED = "召回中"

# ---- 申辩 ----
APPEAL_PENDING = "待处理"
APPEAL_UPHELD = "维持原报告"
APPEAL_OVERTURNED = "申辩成立"
APPEAL_OUTCOMES = (APPEAL_UPHELD, APPEAL_OVERTURNED)


def refine_content_ok(refining: str, content: float) -> bool:
    """精制方式与供应批次全谷物含量的一致性校验（区间互不重叠）。"""
    if refining == REFINE_WHOLE:
        return 0.9 <= content <= 1.0
    if refining == REFINE_PARTIAL:
        return 0.05 < content < 0.9
    if refining == REFINE_REFINED:
        return 0.0 <= content <= 0.05
    return False


@dataclass(frozen=True)
class StandardRule:
    """按产品类别、地区和生效时间发布的标准规则。"""

    rule_id: str
    name: str
    level: str
    product_category: str
    region: str
    effective_from: str
    effective_to: Optional[str]
    claim_thresholds: dict  # 宣称词 -> 全谷物比例下限
    issued_by: str
    published_at: str


@dataclass(frozen=True)
class Ingredient:
    ingredient_id: str
    name: str
    grain_type: str
    is_grain: bool


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    name: str
    region: str


@dataclass(frozen=True)
class SupplyBatch:
    """原料供应批次，附全谷物含量证明（组成计算的来源之一）。"""

    supply_batch_id: str
    supplier_id: str
    ingredient_id: str
    batch_no: str
    whole_grain_content: float
    certificate_ref: str
    received_at: str


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    category: str
    markets: tuple

    @classmethod
    def from_dict(cls, d):
        return cls(d["product_id"], d["name"], d["category"], tuple(d["markets"]))


@dataclass(frozen=True)
class RecipeLine:
    """配方明细行：谷物原料 + 精制方式 + 投料比例 + 供应批次。"""

    ingredient_id: str
    refining: str
    ratio: float
    supply_batch_id: Optional[str]


@dataclass(frozen=True)
class RecipeVersion:
    recipe_id: str
    product_id: str
    version: int
    lines: tuple
    created_by: str
    created_at: str
    note: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(
            recipe_id=d["recipe_id"],
            product_id=d["product_id"],
            version=d["version"],
            lines=tuple(RecipeLine(**line) for line in d["lines"]),
            created_by=d["created_by"],
            created_at=d["created_at"],
            note=d.get("note", ""),
        )


@dataclass(frozen=True)
class Evidence:
    """带来源的证据：实验室检测结果或生产损耗。"""

    evidence_id: str
    recipe_id: str
    kind: str
    source: str        # 实验室名称或产线
    value: float       # 检测比例 或 损耗率
    uncertainty: float  # 检测不确定度；损耗证据恒为 0
    method: str        # 检测方法或损耗统计口径
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True)
class Review:
    review_id: str
    recipe_id: str
    trigger: str
    requested_by: str
    requested_at: str
    auto: bool


@dataclass(frozen=True)
class ReviewDecision:
    """审核决定是独立记录，不改动审核申请本身。"""

    review_id: str
    decision: str
    approver: str
    decided_at: str
    comment: str


@dataclass(frozen=True)
class PackagingVersion:
    packaging_id: str
    product_id: str
    claims: tuple
    design_ref: str
    created_by: str
    created_at: str

    @classmethod
    def from_dict(cls, d):
        return cls(
            d["packaging_id"], d["product_id"], tuple(d["claims"]),
            d["design_ref"], d["created_by"], d["created_at"],
        )


@dataclass(frozen=True)
class TransitionApproval:
    """旧包装库存的获准过渡期。"""

    approval_id: str
    packaging_id: str
    approved_until: str
    max_quantity: int
    approved_by: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class ProductionBatch:
    batch_code: str
    recipe_id: str
    packaging_id: str
    product_id: str
    market: str
    quantity: int
    production_date: str
    transition_approval_id: Optional[str]
    created_at: str


@dataclass(frozen=True)
class InspectionReport:
    """抽检报告：记录当时的计算快照，之后任何申辩/召回都不改动它。"""

    inspection_id: str
    batch_code: str
    measured_ratio: float
    uncertainty: float
    method: str
    agency: str
    inspector: str
    computed_ratio: float
    range_low: float
    range_high: float
    within_range: bool
    created_at: str


@dataclass(frozen=True)
class Appeal:
    appeal_id: str
    inspection_id: str
    reason: str
    appellant: str
    created_at: str


@dataclass(frozen=True)
class AppealDecision:
    appeal_id: str
    outcome: str
    decided_by: str
    decided_at: str
    comment: str


@dataclass(frozen=True)
class RecallDecision:
    recall_id: str
    batch_code: str
    reason: str
    decided_by: str
    created_at: str
