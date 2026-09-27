"""全谷物标签合规库的领域与接口测试。"""
import json
import threading
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler
from wholegrain import ComplianceService, Store
from wholegrain.errors import NotFound, StateError, ValidationError


def fixed_clock():
    return datetime(2026, 9, 27, 10, 0, 0)


def seeded_service():
    """构造一套基础数据：原料、供应方、供应批次、产品、并行标准。"""
    app = ComplianceService(Store(), clock=fixed_clock)
    wheat = app.register_ingredient(name="小麦粉", grain_type="小麦", is_grain=True)["ingredient_id"]
    oat = app.register_ingredient(name="燕麦片", grain_type="燕麦", is_grain=True)["ingredient_id"]
    sugar = app.register_ingredient(name="白砂糖", is_grain=False)["ingredient_id"]
    sup_a = app.register_supplier(name="华北麦业", region="河北")["supplier_id"]
    sup_b = app.register_supplier(name="鲁粮集团", region="山东")["supplier_id"]
    sb_whole = app.register_supply_batch(
        supplier_id=sup_a, ingredient_id=wheat, batch_no="A20260901",
        whole_grain_content=1.0, certificate_ref="CERT-A1")["supply_batch_id"]
    sb_refined = app.register_supply_batch(
        supplier_id=sup_a, ingredient_id=wheat, batch_no="A20260902",
        whole_grain_content=0.0, certificate_ref="CERT-A2")["supply_batch_id"]
    sb_partial = app.register_supply_batch(
        supplier_id=sup_b, ingredient_id=oat, batch_no="B20260901",
        whole_grain_content=0.5, certificate_ref="CERT-B1")["supply_batch_id"]
    sb_whole_b = app.register_supply_batch(
        supplier_id=sup_b, ingredient_id=wheat, batch_no="B20260902",
        whole_grain_content=0.95, certificate_ref="CERT-B2")["supply_batch_id"]
    product = app.register_product(
        name="醇熟全麦面包", category="面包", markets=["上海", "北京"])["product_id"]
    app.publish_rule(name="全谷物食品通则", level="国家标准", product_category="面包",
                     region="全国", effective_from="2026-01-01",
                     claim_thresholds={"全麦": 0.51, "全谷物": 0.30}, issued_by="卫生主管部门")
    app.publish_rule(name="焙烤食品全谷物规范", level="行业标准", product_category="面包",
                     region="全国", effective_from="2026-01-01",
                     claim_thresholds={"全麦": 0.55}, issued_by="行业主管部门")
    app.publish_rule(name="上海烘焙团体规范", level="团体规则", product_category="面包",
                     region="上海", effective_from="2026-06-01",
                     claim_thresholds={"全麦": 0.60}, issued_by="上海烘焙协会")
    app.publish_rule(name="旧版全谷物通则", level="国家标准", product_category="面包",
                     region="全国", effective_from="2025-01-01", effective_to="2025-12-31",
                     claim_thresholds={"全麦": 0.40}, issued_by="卫生主管部门")
    ids = SimpleNamespace(
        wheat=wheat, oat=oat, sugar=sugar, sup_a=sup_a, sup_b=sup_b,
        sb_whole=sb_whole, sb_refined=sb_refined, sb_partial=sb_partial,
        sb_whole_b=sb_whole_b, product=product,
    )
    return app, ids


def v1_lines(ids):
    return [
        {"ingredient_id": ids.wheat, "refining": "全粒", "ratio": 0.6,
         "supply_batch_id": ids.sb_whole},
        {"ingredient_id": ids.wheat, "refining": "精制", "ratio": 0.3,
         "supply_batch_id": ids.sb_refined},
        {"ingredient_id": ids.sugar, "refining": "无", "ratio": 0.1},
    ]


def make_recipe(app, ids, lines=None):
    return app.create_recipe(ids.product, lines=lines or v1_lines(ids),
                             created_by="配方师小王")["recipe_id"]


def approve_recipe(app, recipe_id, approver="审核员甲"):
    review = app.request_review(recipe_id, "试产", "企业品控")
    app.decide_review(review["review_id"], "通过", approver, "试产合格")
    review = app.request_review(recipe_id, "量产", "企业品控")
    app.decide_review(review["review_id"], "通过", approver, "准予量产")


def approved_recipe(app, ids, lines=None):
    recipe_id = make_recipe(app, ids, lines)
    approve_recipe(app, recipe_id)
    return recipe_id


def make_packaging(app, ids, claims=("全麦",)):
    return app.create_packaging(ids.product, claims=list(claims),
                                design_ref="design/pkg-v1.ai",
                                created_by="市场部")["packaging_id"]


def make_batch(app, ids, recipe_id, packaging_id, market="北京", quantity=1000,
               production_date="2026-09-01"):
    return app.create_batch(recipe_id, packaging_id, market=market, quantity=quantity,
                            production_date=production_date)


class CompositionTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ids = seeded_service()

    def test_ratio_computed_from_lines(self):
        recipe_id = make_recipe(self.app, self.ids)
        composition = self.app.composition_of(recipe_id)
        self.assertAlmostEqual(composition["computed_ratio"], 0.6)
        self.assertAlmostEqual(composition["range_low"], 0.6)
        self.assertAlmostEqual(composition["range_high"], 0.6)
        grain_lines = [l for l in composition["lines"] if l["is_grain"]]
        self.assertEqual(len(grain_lines), 2)
        self.assertEqual(grain_lines[0]["supply_batch_no"], "A20260901")

    def test_evidence_extends_range_with_provenance(self):
        recipe_id = make_recipe(self.app, self.ids)
        self.app.add_evidence(recipe_id, kind="production_loss", source="一号线",
                              value=0.1, method="投料称重差额法", recorded_by="车间主任")
        composition = self.app.composition_of(recipe_id)
        self.assertAlmostEqual(composition["range_low"], 0.54)
        self.app.add_evidence(recipe_id, kind="lab_result", source="市质检院",
                              value=0.58, uncertainty=0.03, method="GB 5009.88",
                              recorded_by="检验员小李")
        composition = self.app.composition_of(recipe_id)
        self.assertAlmostEqual(composition["range_low"], 0.54)
        self.assertAlmostEqual(composition["range_high"], 0.61)
        sources = {e["source"] for e in composition["evidence"]}
        self.assertEqual(sources, {"一号线", "市质检院"})

    def test_lines_must_sum_to_one(self):
        lines = v1_lines(self.ids)
        lines[2]["ratio"] = 0.2
        with self.assertRaises(ValidationError):
            self.app.create_recipe(self.ids.product, lines=lines, created_by="配方师")

    def test_grain_line_requires_batch_and_consistent_refining(self):
        lines = v1_lines(self.ids)
        del lines[0]["supply_batch_id"]
        with self.assertRaises(ValidationError):
            self.app.create_recipe(self.ids.product, lines=lines, created_by="配方师")
        lines = v1_lines(self.ids)
        lines[0]["supply_batch_id"] = self.ids.sb_partial  # 含量0.5却标全粒
        with self.assertRaises(ValidationError):
            self.app.create_recipe(self.ids.product, lines=lines, created_by="配方师")

    def test_batch_ingredient_must_match_line(self):
        lines = v1_lines(self.ids)
        lines[0]["supply_batch_id"] = self.ids.sb_partial  # 燕麦批次用于小麦行
        lines[0]["refining"] = "部分精制"
        with self.assertRaises(ValidationError):
            self.app.create_recipe(self.ids.product, lines=lines, created_by="配方师")

    def test_non_grain_line_has_no_supply_batch(self):
        lines = v1_lines(self.ids)
        lines[2]["supply_batch_id"] = self.ids.sb_whole
        with self.assertRaises(ValidationError):
            self.app.create_recipe(self.ids.product, lines=lines, created_by="配方师")


class RuleMatchingTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ids = seeded_service()

    def test_parallel_rules_all_apply_and_governing_by_level(self):
        result = self.app.match_rules("面包", "上海", "2026-09-01")
        self.assertEqual(len(result["applicable"]), 3)
        self.assertEqual(result["governing"]["level"], "国家标准")
        self.assertEqual(result["governing"]["name"], "全谷物食品通则")

    def test_region_and_date_window(self):
        result = self.app.match_rules("面包", "北京", "2026-09-01")
        self.assertEqual(len(result["applicable"]), 2)  # 上海团标不适用
        result = self.app.match_rules("面包", "北京", "2025-06-01")
        self.assertEqual(len(result["applicable"]), 1)
        self.assertEqual(result["applicable"][0]["name"], "旧版全谷物通则")

    def test_no_rule_for_other_category(self):
        result = self.app.match_rules("饼干", "北京", "2026-09-01")
        self.assertEqual(result["applicable"], [])
        self.assertIsNone(result["governing"])

    def test_rule_validation(self):
        with self.assertRaises(ValidationError):
            self.app.publish_rule(name="x", level="企业标准", product_category="面包",
                                  region="全国", effective_from="2026-01-01",
                                  claim_thresholds={"全麦": 0.5}, issued_by="某方")
        with self.assertRaises(ValidationError):
            self.app.publish_rule(name="x", level="国家标准", product_category="面包",
                                  region="全国", effective_from="2026-01-01",
                                  claim_thresholds={"全麦": 1.5}, issued_by="某方")
        with self.assertRaises(ValidationError):
            self.app.publish_rule(name="x", level="国家标准", product_category="面包",
                                  region="全国", effective_from="2026-06-01",
                                  effective_to="2026-01-01",
                                  claim_thresholds={"全麦": 0.5}, issued_by="某方")


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ids = seeded_service()

    def test_trial_then_mass_production(self):
        recipe_id = make_recipe(self.app, self.ids)
        self.assertEqual(self.app.recipe_status(recipe_id), "配方草拟")
        with self.assertRaises(StateError):
            self.app.request_review(recipe_id, "量产", "企业品控")
        review = self.app.request_review(recipe_id, "试产", "企业品控")
        self.assertEqual(review["status"], "待审核")
        self.app.decide_review(review["review_id"], "通过", "审核员甲")
        self.assertEqual(self.app.recipe_status(recipe_id), "试产核验")
        review = self.app.request_review(recipe_id, "量产", "企业品控")
        self.app.decide_review(review["review_id"], "通过", "审核员甲")
        self.assertEqual(self.app.recipe_status(recipe_id), "允许使用标签")

    def test_rejected_trial_can_be_resubmitted(self):
        recipe_id = make_recipe(self.app, self.ids)
        review = self.app.request_review(recipe_id, "试产", "企业品控")
        self.app.decide_review(review["review_id"], "驳回", "审核员甲", "试产样品不达标")
        self.assertEqual(self.app.recipe_status(recipe_id), "配方草拟")
        review = self.app.request_review(recipe_id, "试产", "企业品控")
        self.app.decide_review(review["review_id"], "通过", "审核员甲")
        self.assertEqual(self.app.recipe_status(recipe_id), "试产核验")

    def test_recipe_change_triggers_auto_review(self):
        recipe_v1 = approved_recipe(self.app, self.ids)
        lines = v1_lines(self.ids)
        lines[1]["ratio"] = 0.25
        lines[2]["ratio"] = 0.15
        recipe_v2 = self.app.create_recipe(self.ids.product, lines=lines,
                                           created_by="配方师小王")["recipe_id"]
        reviews = self.app.list_reviews(recipe_v2)
        auto = [r for r in reviews if r["trigger"] == "改配方"]
        self.assertEqual(len(auto), 1)
        self.assertTrue(auto[0]["auto"])
        review = self.app.request_review(recipe_v2, "试产", "企业品控")
        self.app.decide_review(review["review_id"], "通过", "审核员甲")
        with self.assertRaises(StateError):
            self.app.request_review(recipe_v2, "量产", "企业品控")
        self.app.decide_review(auto[0]["review_id"], "通过", "审核员乙")
        review = self.app.request_review(recipe_v2, "量产", "企业品控")
        self.app.decide_review(review["review_id"], "通过", "审核员乙")
        self.assertEqual(self.app.recipe_status(recipe_v2), "允许使用标签")
        self.assertEqual(self.app.recipe_status(recipe_v1), "允许使用标签")

    def test_supplier_change_triggers_auto_review(self):
        approved_recipe(self.app, self.ids)
        lines = v1_lines(self.ids)
        lines[0]["supply_batch_id"] = self.ids.sb_whole_b  # 换成供应商B的全麦粉
        lines[0]["ratio"] = 0.6
        recipe_v2 = self.app.create_recipe(self.ids.product, lines=lines,
                                           created_by="配方师小王")["recipe_id"]
        triggers = {r["trigger"] for r in self.app.list_reviews(recipe_v2)}
        self.assertIn("改配方", triggers)
        self.assertIn("换供应商", triggers)

    def test_manual_change_review_rejected(self):
        recipe_id = make_recipe(self.app, self.ids)
        with self.assertRaises(ValidationError):
            self.app.request_review(recipe_id, "改配方", "企业品控")

    def test_decision_is_final(self):
        recipe_id = make_recipe(self.app, self.ids)
        review = self.app.request_review(recipe_id, "试产", "企业品控")
        self.app.decide_review(review["review_id"], "通过", "审核员甲")
        with self.assertRaises(StateError):
            self.app.decide_review(review["review_id"], "驳回", "审核员乙")


class BatchGateTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ids = seeded_service()
        self.recipe_id = approved_recipe(self.app, self.ids)
        self.packaging_id = make_packaging(self.app, self.ids)

    def test_happy_path_batch(self):
        batch = make_batch(self.app, self.ids, self.recipe_id, self.packaging_id)
        self.assertEqual(batch["status"], "正常流通")
        self.assertTrue(batch["batch_code"].startswith("WG-"))

    def test_unapproved_recipe_cannot_produce(self):
        draft = make_recipe(self.app, self.ids)
        with self.assertRaises(StateError):
            make_batch(self.app, self.ids, draft, self.packaging_id)

    def test_claim_must_satisfy_strictest_applicable_rule(self):
        # 北京阈值0.55、上海阈值0.60，区间下界0.6时两地均可
        make_batch(self.app, self.ids, self.recipe_id, self.packaging_id,
                   market="上海", production_date="2026-09-01")
        # 登记5%生产损耗后下界变0.57：北京仍可，上海不满足团标0.60
        self.app.add_evidence(self.recipe_id, kind="production_loss", source="一号线",
                              value=0.05, method="投料称重差额法", recorded_by="车间主任")
        make_batch(self.app, self.ids, self.recipe_id, self.packaging_id,
                   market="北京", production_date="2026-09-02")
        with self.assertRaises(StateError):
            make_batch(self.app, self.ids, self.recipe_id, self.packaging_id,
                       market="上海", production_date="2026-09-02")

    def test_undefined_claim_rejected(self):
        packaging = self.app.create_packaging(
            self.ids.product, claims=["零添加"], design_ref="d.ai", created_by="市场部"
        )["packaging_id"]
        with self.assertRaises(StateError):
            make_batch(self.app, self.ids, self.recipe_id, packaging)

    def test_unknown_market_rejected(self):
        with self.assertRaises(ValidationError):
            make_batch(self.app, self.ids, self.recipe_id, self.packaging_id, market="广州")

    def test_old_packaging_only_within_approved_transition(self):
        make_packaging(self.app, self.ids)  # 新包装上线，旧包装成为历史版本
        with self.assertRaises(StateError):
            make_batch(self.app, self.ids, self.recipe_id, self.packaging_id)
        self.app.approve_transition(self.packaging_id, approved_until="2026-12-31",
                                    max_quantity=1500, approved_by="监管员丙",
                                    reason="消化旧包装库存")
        batch = make_batch(self.app, self.ids, self.recipe_id, self.packaging_id,
                           quantity=1000, production_date="2026-10-01")
        self.assertEqual(batch["status"], "过渡销售")
        with self.assertRaises(StateError):  # 累计1600超过批准数量1500
            make_batch(self.app, self.ids, self.recipe_id, self.packaging_id,
                       quantity=600, production_date="2026-10-02")
        with self.assertRaises(StateError):  # 超过渡期截止日
            make_batch(self.app, self.ids, self.recipe_id, self.packaging_id,
                       quantity=100, production_date="2027-01-05")


class InspectionAppealRecallTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ids = seeded_service()
        self.recipe_id = approved_recipe(self.app, self.ids)
        self.packaging_id = make_packaging(self.app, self.ids)
        self.batch = make_batch(self.app, self.ids, self.recipe_id, self.packaging_id)

    def test_failed_inspection_appeal_upheld_then_recall(self):
        report = self.app.record_inspection(
            self.batch["batch_code"], measured_ratio=0.5, uncertainty=0.02,
            method="GB 5009.88", agency="市质检院", inspector="检验员小赵")
        self.assertFalse(report["within_range"])
        self.assertAlmostEqual(report["computed_ratio"], 0.6)
        self.assertEqual(self.app.batch_status(self.batch["batch_code"]), "抽检复核")
        appeal = self.app.file_appeal(report["inspection_id"],
                                      reason="取样不均导致偏差", appellant="企业法务")
        self.assertEqual(appeal["status"], "待处理")
        decided = self.app.decide_appeal(appeal["appeal_id"], outcome="维持原报告",
                                         decided_by="监管员丁")
        self.assertEqual(decided["status"], "维持原报告")
        self.assertEqual(self.app.batch_status(self.batch["batch_code"]), "抽检复核")
        # 原报告不被申辩改动
        detail = self.app.inspection_detail(report["inspection_id"])
        self.assertAlmostEqual(detail["measured_ratio"], 0.5)
        self.assertFalse(detail["within_range"])
        # 召回决定同样是追加记录，不覆盖原报告
        self.app.decide_recall(self.batch["batch_code"], reason="全谷物含量不足",
                               decided_by="监管员丁")
        self.assertEqual(self.app.batch_status(self.batch["batch_code"]), "召回中")
        detail = self.app.inspection_detail(report["inspection_id"])
        self.assertAlmostEqual(detail["measured_ratio"], 0.5)
        scan = self.app.scan(self.batch["batch_code"])
        self.assertEqual(scan["certification_status"], "已召回")
        self.assertEqual(scan["recall"]["decided_by"], "监管员丁")
        with self.assertRaises(StateError):
            self.app.decide_recall(self.batch["batch_code"], reason="重复", decided_by="监管员丁")

    def test_appeal_overturned_restores_circulation(self):
        report = self.app.record_inspection(
            self.batch["batch_code"], measured_ratio=0.59, uncertainty=0.02,
            method="GB 5009.88", agency="市质检院", inspector="检验员小赵")
        self.assertTrue(report["within_range"])
        self.assertEqual(self.app.batch_status(self.batch["batch_code"]), "抽检复核")
        appeal = self.app.file_appeal(report["inspection_id"],
                                      reason="检测方法适用错误", appellant="企业法务")
        self.app.decide_appeal(appeal["appeal_id"], outcome="申辩成立",
                               decided_by="监管员丁", comment="复测结果合格")
        self.assertEqual(self.app.batch_status(self.batch["batch_code"]), "正常流通")

    def test_one_appeal_per_inspection(self):
        report = self.app.record_inspection(
            self.batch["batch_code"], measured_ratio=0.5, uncertainty=0.0,
            method="GB 5009.88", agency="市质检院", inspector="检验员小赵")
        self.app.file_appeal(report["inspection_id"], reason="r", appellant="企业")
        with self.assertRaises(StateError):
            self.app.file_appeal(report["inspection_id"], reason="r2", appellant="企业")

    def test_records_are_frozen(self):
        report_id = self.app.record_inspection(
            self.batch["batch_code"], measured_ratio=0.5, uncertainty=0.0,
            method="GB 5009.88", agency="市质检院", inspector="检验员小赵")["inspection_id"]
        record = self.app._get("inspections", "inspection_id", report_id, "抽检报告")
        with self.assertRaises(FrozenInstanceError):
            record.measured_ratio = 0.9


class ScanAndTraceTest(unittest.TestCase):
    def setUp(self):
        self.app, self.ids = seeded_service()
        self.recipe_id = approved_recipe(self.app, self.ids)
        self.app.add_evidence(self.recipe_id, kind="lab_result", source="市质检院",
                              value=0.6, uncertainty=0.02, method="GB 5009.88",
                              recorded_by="检验员小李")
        self.packaging_id = make_packaging(self.app, self.ids)
        self.batch = make_batch(self.app, self.ids, self.recipe_id, self.packaging_id)

    def test_scan_shows_definition_range_and_status(self):
        view = self.app.scan(self.batch["batch_code"])
        self.assertEqual(view["certification_status"], "认证有效")
        self.assertEqual(view["governing_rule"]["name"], "全谷物食品通则")
        self.assertEqual(len(view["applicable_definitions"]), 2)  # 北京: 国标+行标
        self.assertAlmostEqual(view["whole_grain_ratio"]["computed"], 0.6)
        self.assertAlmostEqual(view["whole_grain_ratio"]["range_low"], 0.58)
        self.assertTrue(view["claim_verdicts"]["全麦"]["allowed"])
        self.assertAlmostEqual(view["claim_verdicts"]["全麦"]["threshold"], 0.55)

    def test_scan_unknown_batch_404(self):
        with self.assertRaises(NotFound):
            self.app.scan("WG-999999")

    def test_trace_from_claim_to_approver_and_circulation(self):
        trace = self.app.trace_claim("全麦")
        self.assertEqual(len(trace["chains"]), 1)
        chain = trace["chains"][0]
        self.assertEqual(chain["packaging"]["packaging_id"], self.packaging_id)
        recipe = chain["recipes"][0]
        self.assertEqual(recipe["recipe_id"], self.recipe_id)
        self.assertEqual(recipe["evidence"][0]["method"], "GB 5009.88")
        approvers = {r["approver"] for r in recipe["reviews"]}
        self.assertIn("审核员甲", approvers)
        scope = chain["circulating_scope"]
        self.assertEqual(scope["total_quantity"], 1000)
        self.assertEqual(scope["batches"][0]["batch_code"], self.batch["batch_code"])
        # 召回后不再计入仍在流通的包装范围
        self.app.decide_recall(self.batch["batch_code"], reason="全谷物含量不足",
                               decided_by="监管员丁")
        trace = self.app.trace_claim("全麦")
        scope = trace["chains"][0]["circulating_scope"]
        self.assertEqual(scope["total_quantity"], 0)
        self.assertEqual(scope["batches"], [])

    def test_trace_matches_slogan_containing_claim(self):
        trace = self.app.trace_claim("全麦面包 节粮新风尚")
        self.assertEqual(len(trace["chains"]), 1)
        self.assertEqual(self.app.trace_claim("无糖")["chains"], [])


class PersistenceTest(unittest.TestCase):
    def test_store_roundtrip(self):
        with NamedTemporaryFile(suffix=".json", delete=True) as fh:
            path = fh.name
        app = ComplianceService(Store(path), clock=fixed_clock)
        app.register_ingredient(name="小麦粉", grain_type="小麦", is_grain=True)
        app.register_product(name="面包", category="面包", markets=["北京"])
        reloaded = ComplianceService(Store(path), clock=fixed_clock)
        self.assertEqual(len(reloaded.list_ingredients()), 1)
        self.assertEqual(list(reloaded.list_products()[0]["markets"]), ["北京"])
        # 计数器也随之恢复，编号不重复
        ingredient = reloaded.register_ingredient(name="燕麦片", grain_type="燕麦", is_grain=True)
        self.assertEqual(ingredient["ingredient_id"], "ING-0002")


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_app = Handler.app
        cls.app, cls.ids = seeded_service()
        Handler.app = cls.app
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        Handler.app = cls._original_app

    def get(self, path):
        try:
            with urlopen(f"{self.base_url}{path}", timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                body = error.read()
                try:
                    return error.code, json.loads(body)
                except json.JSONDecodeError:
                    return error.code, {}

    def post(self, path, payload):
        request = Request(f"{self.base_url}{path}",
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_full_flow_over_http(self):
        ids = self.ids
        status, recipe = self.post("/recipes", {
            "product_id": ids.product, "created_by": "配方师小王", "lines": v1_lines(ids)})
        self.assertEqual(status, 200)
        recipe_id = recipe["recipe_id"]
        self.assertAlmostEqual(recipe["composition"]["computed_ratio"], 0.6)
        for trigger in ("试产", "量产"):
            status, review = self.post(f"/recipes/{recipe_id}/reviews",
                                       {"trigger": trigger, "requested_by": "企业品控"})
            self.assertEqual(status, 200)
            status, decided = self.post(f"/reviews/{review['review_id']}/decision",
                                        {"decision": "通过", "approver": "审核员甲"})
            self.assertEqual(status, 200)
            self.assertEqual(decided["status"], "已通过")
        status, packaging = self.post("/packaging", {
            "product_id": ids.product, "claims": ["全麦"],
            "design_ref": "design/pkg-v1.ai", "created_by": "市场部"})
        self.assertEqual(status, 200)
        status, batch = self.post("/batches", {
            "recipe_id": recipe_id, "packaging_id": packaging["packaging_id"],
            "market": "北京", "quantity": 800, "production_date": "2026-09-15"})
        self.assertEqual(status, 200)
        self.assertEqual(batch["status"], "正常流通")
        status, view = self.get(f"/scan/{batch['batch_code']}")
        self.assertEqual(status, 200)
        self.assertEqual(view["certification_status"], "认证有效")
        self.assertEqual(view["governing_rule"]["level"], "国家标准")
        status, trace = self.get("/trace/claims/%E5%85%A8%E9%BA%A6")  # 全麦
        self.assertEqual(status, 200)
        self.assertEqual(len(trace["chains"]), 1)
        self.assertEqual(trace["chains"][0]["circulating_scope"]["total_quantity"], 800)

    def test_recipe_rejects_direct_percentage(self):
        status, payload = self.post("/recipes", {
            "product_id": self.ids.product, "created_by": "配方师小王",
            "lines": v1_lines(self.ids), "final_ratio": 0.6})
        self.assertEqual(status, 400)
        self.assertIn("不允许直接填报", payload["error"])

    def test_error_mapping(self):
        status, payload = self.post("/rules", {"name": "缺字段"})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, _ = self.get("/recipes/RCP-9999")
        self.assertEqual(status, 404)
        status, _ = self.get("/no-such-route")
        self.assertEqual(status, 404)

    def test_conflict_on_repeat_decision(self):
        _, recipe = self.post("/recipes", {
            "product_id": self.ids.product, "created_by": "配方师小王",
            "lines": v1_lines(self.ids)})
        recipe_id = recipe["recipe_id"]
        _, review = self.post(f"/recipes/{recipe_id}/reviews",
                              {"trigger": "试产", "requested_by": "企业品控"})
        self.post(f"/reviews/{review['review_id']}/decision",
                  {"decision": "通过", "approver": "审核员甲"})
        status, payload = self.post(f"/reviews/{review['review_id']}/decision",
                                    {"decision": "驳回", "approver": "审核员乙"})
        self.assertEqual(status, 409)
        self.assertIn("不得更改", payload["error"])


if __name__ == "__main__":
    unittest.main()
