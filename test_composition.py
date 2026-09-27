"""组成计算：全谷物比例只能由配方明细与证据得出。"""
import unittest

from domain import (
    DomainError,
    Evidence,
    EvidenceKind,
    IngredientLine,
    RecipeVersion,
    Refining,
    compute_composition,
    reject_direct_ratio,
)


def make_recipe(lines):
    return RecipeVersion(
        recipe_id="RCP-1",
        product_id="P",
        product_category="面包",
        region="华东",
        version=1,
        lines=tuple(lines),
        created_at="2026-09-01",
    )


def make_line(refining, ratio, tolerance, supply):
    return IngredientLine(
        ingredient_id=supply,
        name=supply,
        grain_type="小麦",
        refining=Refining(refining),
        ratio=ratio,
        ratio_tolerance=tolerance,
        supply_batch_id=supply,
    )


def lab(subject, lo, hi):
    return Evidence(
        evidence_id=f"EV-{subject}",
        kind=EvidenceKind.LAB_RESULT,
        subject_id=subject,
        source={"lab": "L", "method": "M"},
        ranges={"whole_grain_fraction": [lo, hi]},
        recorded_at="2026-09-01",
    )


class CompositionTest(unittest.TestCase):
    def test_range_computed_from_lines_and_evidence(self):
        recipe = make_recipe(
            [make_line("whole", 0.62, 0.02, "SUP-1"), make_line("refined", 0.38, 0.02, "SUP-2")]
        )
        result = compute_composition(recipe, {"SUP-1": lab("SUP-1", 0.96, 1.0)}, None, "2026-09-27")
        self.assertAlmostEqual(result.ratio_range[0], 0.576 / 1.04)
        self.assertAlmostEqual(result.ratio_range[1], 0.64 / 0.96)
        self.assertEqual(result.evidence_ids, ("EV-SUP-1",))
        refined = result.contributions[1]
        self.assertEqual(refined.whole_fraction, [0.0, 0.0])
        self.assertIsNone(refined.evidence_id)

    def test_missing_evidence_blocks_computation(self):
        recipe = make_recipe([make_line("whole", 1.0, 0.0, "SUP-9")])
        with self.assertRaises(DomainError):
            compute_composition(recipe, {}, None, "2026-09-27")

    def test_production_loss_lowers_ratio(self):
        recipe = make_recipe([make_line("whole", 1.0, 0.0, "SUP-1")])
        loss = Evidence(
            "EV-L",
            EvidenceKind.PRODUCTION_LOSS,
            "RCP-1",
            {"line": "1号线"},
            {"loss_rate": [0.05, 0.1]},
            "2026-09-02",
        )
        result = compute_composition(recipe, {"SUP-1": lab("SUP-1", 1.0, 1.0)}, loss, "2026-09-27")
        self.assertAlmostEqual(result.ratio_range[0], 0.9)
        self.assertAlmostEqual(result.ratio_range[1], 0.95)
        self.assertIn("EV-L", result.evidence_ids)

    def test_direct_ratio_rejected(self):
        with self.assertRaises(DomainError):
            reject_direct_ratio({"product_id": "P", "final_percentage": 0.6})
        reject_direct_ratio({"product_id": "P"})  # 正常明细请求放行


if __name__ == "__main__":
    unittest.main()
