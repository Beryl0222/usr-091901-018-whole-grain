"""标准按产品类别、地区与生效时间匹配。"""
import unittest

from domain import DomainError
from standards import StandardRule, match_standard


def rule(rule_id, level, region, start, end=None, minimum=0.5):
    return StandardRule(
        rule_id=rule_id,
        issuer_level=level,
        product_category="面包",
        region=region,
        effective_from=start,
        effective_to=end,
        min_whole_grain_ratio=minimum,
        definition_text=f"定义{rule_id}",
        published_at=start,
    )


class StandardMatchTest(unittest.TestCase):
    def setUp(self):
        self.rules = [
            rule("STD-GB-1", "national", "全国", "2025-01-01", "2026-06-30", 0.4),
            rule("STD-GB-2", "national", "全国", "2026-07-01", None, 0.5),
            rule("STD-IND-1", "industry", "全国", "2026-01-01", None, 0.45),
            rule("STD-HD-1", "group", "华东", "2026-03-01", None, 0.55),
        ]

    def test_effective_window_selects_version(self):
        self.assertEqual(
            match_standard(self.rules, "面包", "华南", "2026-02-01").rule_id, "STD-GB-1"
        )
        self.assertEqual(
            match_standard(self.rules, "面包", "华南", "2026-09-01").rule_id, "STD-GB-2"
        )

    def test_regional_rule_beats_nationwide(self):
        self.assertEqual(
            match_standard(self.rules, "面包", "华东", "2026-09-01").rule_id, "STD-HD-1"
        )

    def test_national_beats_industry_in_same_window(self):
        rules = [
            rule("A", "industry", "全国", "2026-01-01"),
            rule("B", "national", "全国", "2026-01-01"),
        ]
        self.assertEqual(match_standard(rules, "面包", "华东", "2026-09-01").rule_id, "B")

    def test_no_applicable_rule_raises(self):
        with self.assertRaises(DomainError):
            match_standard(self.rules, "饼干", "华东", "2026-09-01")


if __name__ == "__main__":
    unittest.main()
