"""HTTP 接口端到端：从投料到扫码。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from compliance import ComplianceService
from service import Handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.service = ComplianceService(clock=lambda: "2026-09-27")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, path, payload):
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)

    def get(self, path):
        with urlopen(self.base + path, timeout=2) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)

    def test_full_flow_from_recipe_to_scan(self):
        whole = self.post(
            "/supply-batches", {"supplier_id": "SUP-A", "ingredient_id": "whole-wheat-flour"}
        )
        refined = self.post(
            "/supply-batches", {"supplier_id": "SUP-B", "ingredient_id": "refined-wheat-flour"}
        )
        self.post(
            "/evidence",
            {
                "kind": "lab_result",
                "subject_id": whole["supply_batch_id"],
                "source": {"lab": "国家谷物检测中心", "method": "GB/T 22515"},
                "ranges": {"whole_grain_fraction": [0.97, 1.0]},
            },
        )
        recipe = self.post(
            "/recipes",
            {
                "product_id": "P-200",
                "product_category": "面包",
                "region": "华东",
                "lines": [
                    {
                        "ingredient_id": "whole-wheat-flour",
                        "refining": "whole",
                        "ratio": 0.62,
                        "ratio_tolerance": 0.02,
                        "supply_batch_id": whole["supply_batch_id"],
                    },
                    {
                        "ingredient_id": "refined-wheat-flour",
                        "refining": "refined",
                        "ratio": 0.38,
                        "ratio_tolerance": 0.02,
                        "supply_batch_id": refined["supply_batch_id"],
                    },
                ],
            },
        )
        view = self.get(f"/recipes/{recipe['recipe_id']}")
        self.assertEqual(view["status"], "试产核验")
        review_id = view["reviews"][0]["review_id"]
        self.post(
            f"/reviews/{review_id}/decision", {"approver": "审核员-王", "approved": True}
        )
        self.post(
            "/standards",
            {
                "issuer_level": "national",
                "product_category": "面包",
                "region": "全国",
                "effective_from": "2026-01-01",
                "min_whole_grain_ratio": 0.5,
                "definition_text": "全谷物含量不低于50%方可标示全麦",
            },
        )
        batch = self.post(
            "/packaging-batches",
            {"recipe_id": recipe["recipe_id"], "design_version": "D1", "quantity": 500},
        )
        scan = self.get(f"/scan/{batch['batch_code']}")
        self.assertEqual(scan["certification_status"], "允许使用标签")
        self.assertEqual(scan["applicable_definition"]["min_whole_grain_ratio"], 0.5)
        lo, hi = scan["whole_grain_ratio_range"]
        self.assertLess(lo, hi)
        claim = self.post(
            "/claims",
            {
                "text": "全谷物添加，助力节粮减损",
                "recipe_id": recipe["recipe_id"],
                "registered_by": "市场部-林",
            },
        )
        trace = self.get(f"/trace/claims/{claim['claim_id']}")
        self.assertEqual(trace["recipe"]["recipe_id"], recipe["recipe_id"])
        self.assertEqual(trace["approvals"][0]["approver"], "审核员-王")

    def test_direct_percentage_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            self.post(
                "/recipes",
                {
                    "product_id": "P-201",
                    "product_category": "面包",
                    "region": "华东",
                    "lines": [],
                    "final_percentage": 0.6,
                },
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

    def test_unknown_scan_code_is_404(self):
        with self.assertRaises(HTTPError) as ctx:
            self.get("/scan/PKG-9999")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
