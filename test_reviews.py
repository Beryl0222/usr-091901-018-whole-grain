"""试产、量产、改配方、换供应商分别触发审核；旧包装受过渡期约束。"""
import unittest

from domain import (
    STATUS_BLOCKED,
    STATUS_IN_REVIEW,
    STATUS_LABEL_OK,
    STATUS_TRANSITION,
    DomainError,
)
from helpers import approve_all, build_recipe, make_service, publish_default_rule


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_new_recipe_triggers_trial_review(self):
        ctx = build_recipe(self.service)
        triggers = [r.trigger for r in self.service.store.reviews.values()]
        self.assertEqual(triggers, ["trial_production"])
        self.assertEqual(self.service.recipe_status(ctx.recipe.recipe_id), STATUS_IN_REVIEW)

    def test_approval_allows_label_and_packaging(self):
        ctx = build_recipe(self.service)
        approve_all(self.service, ctx.recipe.recipe_id)
        self.assertEqual(self.service.recipe_status(ctx.recipe.recipe_id), STATUS_LABEL_OK)
        rule = publish_default_rule(self.service)
        batch = self.service.register_packaging(ctx.recipe.recipe_id, "D1", 1000)
        self.assertEqual(batch.standard_rule_id, rule.rule_id)
        self.assertEqual(self.service.packaging_status(batch.batch_code), STATUS_LABEL_OK)

    def test_packaging_blocked_before_approval(self):
        ctx = build_recipe(self.service)
        publish_default_rule(self.service)
        with self.assertRaises(DomainError):
            self.service.register_packaging(ctx.recipe.recipe_id, "D1", 1000)

    def test_recipe_change_and_supplier_change_trigger_reviews(self):
        first = build_recipe(self.service, product_id="P-1")
        approve_all(self.service, first.recipe.recipe_id)
        new_whole = self.service.register_supply_batch("SUP-C", "whole-wheat-flour")
        self.service.add_evidence(
            "lab_result",
            new_whole.supply_batch_id,
            {"lab": "L", "method": "M"},
            {"whole_grain_fraction": [0.95, 1.0]},
        )
        second = self.service.create_recipe(
            "P-1",
            "面包",
            "华东",
            [
                {
                    "ingredient_id": "whole-wheat-flour",
                    "refining": "whole",
                    "ratio": 0.62,
                    "ratio_tolerance": 0.02,
                    "supply_batch_id": new_whole.supply_batch_id,
                },
                {
                    "ingredient_id": "refined-wheat-flour",
                    "refining": "refined",
                    "ratio": 0.38,
                    "ratio_tolerance": 0.02,
                    "supply_batch_id": first.refined.supply_batch_id,
                },
            ],
        )
        self.assertEqual(second.version, 2)
        triggers = sorted(
            r.trigger for r in self.service.store.reviews.values() if r.recipe_id == second.recipe_id
        )
        self.assertEqual(triggers, ["recipe_change", "supplier_change"])

    def test_mass_production_review_and_duplicate_guard(self):
        ctx = build_recipe(self.service)
        approve_all(self.service, ctx.recipe.recipe_id)
        review = self.service.trigger_review("mass_production", ctx.recipe.recipe_id)
        self.assertEqual(review.status, "pending")
        self.assertEqual(self.service.recipe_status(ctx.recipe.recipe_id), STATUS_IN_REVIEW)
        with self.assertRaises(DomainError):
            self.service.trigger_review("mass_production", ctx.recipe.recipe_id)

    def test_decided_review_cannot_be_decided_again(self):
        ctx = build_recipe(self.service)
        review = next(iter(self.service.store.reviews.values()))
        self.service.decide_review(review.review_id, "审核员-王", True)
        with self.assertRaises(DomainError):
            self.service.decide_review(review.review_id, "审核员-李", False)

    def test_old_stock_only_circulates_within_transition(self):
        ctx = build_recipe(self.service)
        approve_all(self.service, ctx.recipe.recipe_id)
        publish_default_rule(self.service)
        batch = self.service.register_packaging(
            ctx.recipe.recipe_id, "D0", 500, is_old_stock=True
        )
        # 未获准过渡期：禁止流通
        self.assertEqual(self.service.packaging_status(batch.batch_code), STATUS_BLOCKED)
        self.service.approve_transition(
            ctx.recipe.recipe_id, "D0", "2026-09-01", "2026-12-31", "监管-赵", "标准换版过渡"
        )
        self.assertEqual(self.service.packaging_status(batch.batch_code), STATUS_TRANSITION)
        # 过渡期满后再次禁止流通
        self.assertEqual(
            self.service.packaging_status(batch.batch_code, today="2027-01-15"),
            STATUS_BLOCKED,
        )


if __name__ == "__main__":
    unittest.main()
