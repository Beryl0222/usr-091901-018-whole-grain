"""抽检差异、企业申辩与召回决定仅作追加，不得覆盖原报告。"""
import unittest

from domain import STATUS_LABEL_OK, STATUS_RECALL, STATUS_RECHECK, DomainError
from helpers import approve_all, build_recipe, make_service, publish_default_rule


class SamplingTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        ctx = build_recipe(self.service)
        approve_all(self.service, ctx.recipe.recipe_id)
        publish_default_rule(self.service)
        self.recipe = ctx.recipe
        self.batch = self.service.register_packaging(self.recipe.recipe_id, "D1", 1000)

    def test_deviation_flags_recheck(self):
        report = self.service.file_sampling_report(
            self.batch.batch_code, "市场监管局", "GB 5009.88", 0.50
        )
        self.assertLess(report.deviation, 0)  # 实测低于配方计算区间下限
        self.assertEqual(self.service.packaging_status(self.batch.batch_code), STATUS_RECHECK)

    def test_within_range_keeps_label_status(self):
        report = self.service.file_sampling_report(
            self.batch.batch_code, "市场监管局", "GB 5009.88", 0.60
        )
        self.assertEqual(report.deviation, 0.0)
        self.assertEqual(self.service.packaging_status(self.batch.batch_code), STATUS_LABEL_OK)

    def test_appeal_and_recall_do_not_overwrite_report(self):
        report = self.service.file_sampling_report(
            self.batch.batch_code, "市场监管局", "GB 5009.88", 0.50
        )
        original = self.service.report_view(report.report_id)["report"]
        self.service.file_appeal(report.report_id, "某食品公司", "检测方法不适用于本产品")
        self.service.file_recall(report.report_id, "监管-赵", "同批次全部下架")
        view = self.service.report_view(report.report_id)
        self.assertEqual(view["report"], original)  # 原报告字段未被覆盖
        self.assertEqual(len(view["appeals"]), 1)
        self.assertEqual(view["appeals"][0]["company"], "某食品公司")
        self.assertEqual(len(view["recalls"]), 1)
        self.assertEqual(view["recalls"][0]["batch_code"], self.batch.batch_code)
        self.assertEqual(self.service.packaging_status(self.batch.batch_code), STATUS_RECALL)

    def test_store_rejects_overwrite(self):
        report = self.service.file_sampling_report(
            self.batch.batch_code, "市场监管局", "GB 5009.88", 0.50
        )
        with self.assertRaises(DomainError):
            self.service.store.put(
                self.service.store.sampling_reports, report.report_id, report
            )


if __name__ == "__main__":
    unittest.main()
