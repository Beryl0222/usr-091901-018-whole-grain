"""测试公共夹具：构造走完审核与包装流程的合规服务。"""
from types import SimpleNamespace

from compliance import ComplianceService


def make_service(today="2026-09-27"):
    return ComplianceService(clock=lambda: today)


def build_recipe(service, product_id="P-100", category="面包", region="华东"):
    """登记供应批次与检测证据并创建配方（不批准审核）。"""
    whole = service.register_supply_batch("SUP-A", "whole-wheat-flour")
    refined = service.register_supply_batch("SUP-B", "refined-wheat-flour")
    service.add_evidence(
        "lab_result",
        whole.supply_batch_id,
        {"lab": "国家谷物检测中心", "method": "GB/T 22515 全谷物含量测定"},
        {"whole_grain_fraction": [0.96, 1.0]},
    )
    recipe = service.create_recipe(
        product_id,
        category,
        region,
        [
            {
                "ingredient_id": "whole-wheat-flour",
                "name": "全麦粉",
                "grain_type": "小麦",
                "refining": "whole",
                "ratio": 0.62,
                "ratio_tolerance": 0.02,
                "supply_batch_id": whole.supply_batch_id,
            },
            {
                "ingredient_id": "refined-wheat-flour",
                "name": "精制小麦粉",
                "grain_type": "小麦",
                "refining": "refined",
                "ratio": 0.38,
                "ratio_tolerance": 0.02,
                "supply_batch_id": refined.supply_batch_id,
            },
        ],
    )
    return SimpleNamespace(recipe=recipe, whole=whole, refined=refined)


def approve_all(service, recipe_id, approver="审核员-王"):
    """批准该配方全部待审审核。"""
    for review in list(service.store.reviews.values()):
        if review.recipe_id == recipe_id and review.status == "pending":
            service.decide_review(review.review_id, approver, True)


def publish_default_rule(
    service,
    category="面包",
    region="全国",
    minimum=0.5,
    effective_from="2026-01-01",
    effective_to=None,
    level="national",
):
    return service.publish_rule(
        level,
        category,
        region,
        effective_from,
        minimum,
        "全谷物含量不低于标准下限方可标示全麦",
        effective_to,
    )
