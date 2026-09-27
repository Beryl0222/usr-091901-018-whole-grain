"""标准规则：按产品类别、地区与生效时间匹配适用定义。"""
from dataclasses import dataclass
from typing import Optional

from domain import DomainError

# 发布主体效力层级：国家标准 > 行业标准 > 团体规则
ISSUER_PRECEDENCE = {"national": 3, "industry": 2, "group": 1}
NATIONWIDE = "全国"


@dataclass(frozen=True)
class StandardRule:
    """一条按产品类别、地区与生效时间发布的标准规则。"""

    rule_id: str
    issuer_level: str  # national / industry / group
    product_category: str
    region: str  # 具体地区或"全国"
    effective_from: str  # 生效日期（ISO，含当日）
    effective_to: Optional[str]  # 失效日期（不含当日），None 表示长期有效
    min_whole_grain_ratio: float
    definition_text: str
    published_at: str

    def applies_on(self, on_date):
        return self.effective_from <= on_date and (
            self.effective_to is None or on_date < self.effective_to
        )


def match_standard(rules, product_category, region, on_date):
    """解析某批产品上市时适用的标准。

    地区专属规则优先于全国规则，同范围内按效力层级与生效时间取最新。
    """
    candidates = [
        rule
        for rule in rules
        if rule.product_category == product_category
        and rule.region in (region, NATIONWIDE)
        and rule.applies_on(on_date)
    ]
    if not candidates:
        raise DomainError(f"{on_date} 起 {region}/{product_category} 无适用标准")
    return max(
        candidates,
        key=lambda rule: (
            rule.region != NATIONWIDE,
            ISSUER_PRECEDENCE.get(rule.issuer_level, 0),
            rule.effective_from,
        ),
    )
