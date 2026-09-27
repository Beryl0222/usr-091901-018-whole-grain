"""消费者扫码视图与监管追溯视图。"""
import unittest

from domain import STATUS_LABEL_OK
from helpers import approve_all, build_recipe, make_service, publish_default_rule


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        ctx = build_recipe(self.service)
        approve_all(self.service, ctx.recipe.recipe_id)
        self.rule = publish_default_rule(self.service)
        self.recipe = ctx.recipe
        self.batch = self.service.register_packaging(self.recipe.recipe_id, "D1", 1000)
        self.claim = self.service.register_claim(
            "全谷物添加，助力节粮减损", self.recipe.recipe_id, "市场部-林"
        )

    def test_scan_shows_definition_range_and_status(self):
        view = self.service.scan(self.batch.batch_code)
        definition = view["applicable_definition"]
        self.assertEqual(definition["rule_id"], self.rule.rule_id)
        self.assertEqual(definition["issuer_level"], "national")
        lo, hi = view["whole_grain_ratio_range"]
        self.assertLess(lo, hi)
        self.assertGreaterEqual(lo, self.rule.min_whole_grain_ratio)
        self.assertTrue(view["meets_standard"])
        self.assertEqual(view["certification_status"], STATUS_LABEL_OK)

    def test_claim_traces_to_recipe_evidence_approver_and_packaging(self):
        trace = self.service.trace_claim(self.claim.claim_id)
        self.assertEqual(trace["claim"]["text"], "全谷物添加，助力节粮减损")
        self.assertEqual(trace["recipe"]["recipe_id"], self.recipe.recipe_id)
        self.assertEqual(trace["recipe"]["version"], 1)
        methods = [e["source"]["method"] for e in trace["composition"]["evidence"]]
        self.assertIn("GB/T 22515 全谷物含量测定", methods)
        approvers = [a["approver"] for a in trace["approvals"]]
        self.assertIn("审核员-王", approvers)
        codes = [p["batch_code"] for p in trace["circulating_packaging"]]
        self.assertIn(self.batch.batch_code, codes)

    def test_recalled_batch_leaves_circulation(self):
        report = self.service.file_sampling_report(
            self.batch.batch_code, "市场监管局", "GB 5009.88", 0.40
        )
        self.service.file_recall(report.report_id, "监管-赵", "全渠道召回")
        trace = self.service.trace_claim(self.claim.claim_id)
        self.assertEqual(trace["circulating_packaging"], [])


if __name__ == "__main__":
    unittest.main()
