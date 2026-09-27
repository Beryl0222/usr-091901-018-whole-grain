"""全谷物标签合规库的服务入口与HTTP接口。"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from wholegrain import ComplianceService, Store
from wholegrain.errors import DomainError, ValidationError

SERVICE_ID = "whole-grain-label"
SERVICE_NAME = "全谷物标签合规库"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

# 全谷物比例只能由配方明细计算，这些字段在配方接口中一律拒绝
FORBIDDEN_RATIO_FIELDS = {
    "whole_grain_ratio", "final_ratio", "final_percentage",
    "whole_grain_percentage", "computed_ratio", "percentage",
}


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _require(body, *fields):
    missing = [name for name in fields if body.get(name) in (None, "", [])]
    if missing:
        raise ValidationError("缺少必填字段: " + ", ".join(missing))


def _require_query(query, *fields):
    missing = [name for name in fields if not query.get(name)]
    if missing:
        raise ValidationError("缺少查询参数: " + ", ".join(missing))


# ---------- 路由处理函数 ----------

def _health(app, params, query, body):
    return health_payload()


def _contract(app, params, query, body):
    return load_contract()


def _create_rule(app, params, query, body):
    _require(body, "name", "level", "product_category", "region",
             "effective_from", "claim_thresholds", "issued_by")
    return app.publish_rule(
        name=body["name"], level=body["level"],
        product_category=body["product_category"], region=body["region"],
        effective_from=body["effective_from"], effective_to=body.get("effective_to"),
        claim_thresholds=body["claim_thresholds"], issued_by=body["issued_by"],
    )


def _list_rules(app, params, query, body):
    return {"rules": app.list_rules(category=query.get("category"),
                                    region=query.get("region"),
                                    date=query.get("date"))}


def _match_rules(app, params, query, body):
    _require_query(query, "category", "region", "date")
    return app.match_rules(query["category"], query["region"], query["date"])


def _create_ingredient(app, params, query, body):
    _require(body, "name", "is_grain")
    return app.register_ingredient(name=body["name"],
                                   grain_type=body.get("grain_type", ""),
                                   is_grain=body["is_grain"])


def _list_ingredients(app, params, query, body):
    return {"ingredients": app.list_ingredients()}


def _create_supplier(app, params, query, body):
    _require(body, "name", "region")
    return app.register_supplier(name=body["name"], region=body["region"])


def _list_suppliers(app, params, query, body):
    return {"suppliers": app.list_suppliers()}


def _create_supply_batch(app, params, query, body):
    _require(body, "supplier_id", "ingredient_id", "batch_no",
             "whole_grain_content", "certificate_ref")
    return app.register_supply_batch(
        supplier_id=body["supplier_id"], ingredient_id=body["ingredient_id"],
        batch_no=body["batch_no"], whole_grain_content=body["whole_grain_content"],
        certificate_ref=body["certificate_ref"],
    )


def _list_supply_batches(app, params, query, body):
    return {"supply_batches": app.list_supply_batches(ingredient_id=query.get("ingredient_id"))}


def _create_product(app, params, query, body):
    _require(body, "name", "category", "markets")
    return app.register_product(name=body["name"], category=body["category"],
                                markets=body["markets"])


def _list_products(app, params, query, body):
    return {"products": app.list_products()}


def _create_recipe(app, params, query, body):
    forbidden = FORBIDDEN_RATIO_FIELDS & set(body)
    if forbidden:
        raise ValidationError(
            "全谷物比例由配方明细计算，不允许直接填报: " + ", ".join(sorted(forbidden))
        )
    _require(body, "product_id", "lines", "created_by")
    return app.create_recipe(body["product_id"], lines=body["lines"],
                             created_by=body["created_by"], note=body.get("note", ""))


def _get_recipe(app, params, query, body):
    return app.recipe_detail(params["recipe_id"])


def _get_composition(app, params, query, body):
    return app.composition_of(params["recipe_id"])


def _add_evidence(app, params, query, body):
    _require(body, "kind", "source", "value", "method", "recorded_by")
    return app.add_evidence(params["recipe_id"], kind=body["kind"], source=body["source"],
                            value=body["value"], uncertainty=body.get("uncertainty", 0.0),
                            method=body["method"], recorded_by=body["recorded_by"])


def _request_review(app, params, query, body):
    _require(body, "trigger", "requested_by")
    return app.request_review(params["recipe_id"], body["trigger"], body["requested_by"])


def _list_reviews(app, params, query, body):
    return {"reviews": app.list_reviews(params["recipe_id"])}


def _decide_review(app, params, query, body):
    _require(body, "decision", "approver")
    return app.decide_review(params["review_id"], body["decision"], body["approver"],
                             body.get("comment", ""))


def _create_packaging(app, params, query, body):
    _require(body, "product_id", "claims", "design_ref", "created_by")
    return app.create_packaging(body["product_id"], claims=body["claims"],
                                design_ref=body["design_ref"], created_by=body["created_by"])


def _validate_packaging(app, params, query, body):
    _require_query(query, "market", "date")
    return app.validate_packaging(params["packaging_id"], query["market"], query["date"])


def _approve_transition(app, params, query, body):
    _require(body, "packaging_id", "approved_until", "max_quantity", "approved_by", "reason")
    return app.approve_transition(body["packaging_id"],
                                  approved_until=body["approved_until"],
                                  max_quantity=body["max_quantity"],
                                  approved_by=body["approved_by"], reason=body["reason"])


def _create_batch(app, params, query, body):
    _require(body, "recipe_id", "packaging_id", "market", "quantity", "production_date")
    return app.create_batch(body["recipe_id"], body["packaging_id"],
                            market=body["market"], quantity=body["quantity"],
                            production_date=body["production_date"])


def _get_batch(app, params, query, body):
    return app.batch_detail(params["batch_code"])


def _create_inspection(app, params, query, body):
    _require(body, "batch_code", "measured_ratio", "method", "agency", "inspector")
    return app.record_inspection(body["batch_code"],
                                 measured_ratio=body["measured_ratio"],
                                 uncertainty=body.get("uncertainty", 0.0),
                                 method=body["method"], agency=body["agency"],
                                 inspector=body["inspector"])


def _get_inspection(app, params, query, body):
    return app.inspection_detail(params["inspection_id"])


def _file_appeal(app, params, query, body):
    _require(body, "reason", "appellant")
    return app.file_appeal(params["inspection_id"], reason=body["reason"],
                           appellant=body["appellant"])


def _decide_appeal(app, params, query, body):
    _require(body, "outcome", "decided_by")
    return app.decide_appeal(params["appeal_id"], outcome=body["outcome"],
                             decided_by=body["decided_by"],
                             comment=body.get("comment", ""))


def _decide_recall(app, params, query, body):
    _require(body, "batch_code", "reason", "decided_by")
    return app.decide_recall(body["batch_code"], reason=body["reason"],
                             decided_by=body["decided_by"])


def _scan(app, params, query, body):
    return app.scan(params["batch_code"])


def _trace_claim(app, params, query, body):
    return app.trace_claim(params["claim_text"])


ROUTES = [
    ("GET", re.compile(r"/health"), _health),
    ("GET", re.compile(r"/contract"), _contract),
    ("POST", re.compile(r"/rules"), _create_rule),
    ("GET", re.compile(r"/rules"), _list_rules),
    ("GET", re.compile(r"/rules/match"), _match_rules),
    ("POST", re.compile(r"/ingredients"), _create_ingredient),
    ("GET", re.compile(r"/ingredients"), _list_ingredients),
    ("POST", re.compile(r"/suppliers"), _create_supplier),
    ("GET", re.compile(r"/suppliers"), _list_suppliers),
    ("POST", re.compile(r"/supply-batches"), _create_supply_batch),
    ("GET", re.compile(r"/supply-batches"), _list_supply_batches),
    ("POST", re.compile(r"/products"), _create_product),
    ("GET", re.compile(r"/products"), _list_products),
    ("POST", re.compile(r"/recipes"), _create_recipe),
    ("GET", re.compile(r"/recipes/(?P<recipe_id>[^/]+)"), _get_recipe),
    ("GET", re.compile(r"/recipes/(?P<recipe_id>[^/]+)/composition"), _get_composition),
    ("POST", re.compile(r"/recipes/(?P<recipe_id>[^/]+)/evidence"), _add_evidence),
    ("POST", re.compile(r"/recipes/(?P<recipe_id>[^/]+)/reviews"), _request_review),
    ("GET", re.compile(r"/recipes/(?P<recipe_id>[^/]+)/reviews"), _list_reviews),
    ("POST", re.compile(r"/reviews/(?P<review_id>[^/]+)/decision"), _decide_review),
    ("POST", re.compile(r"/packaging"), _create_packaging),
    ("GET", re.compile(r"/packaging/(?P<packaging_id>[^/]+)/validate"), _validate_packaging),
    ("POST", re.compile(r"/transition-approvals"), _approve_transition),
    ("POST", re.compile(r"/batches"), _create_batch),
    ("GET", re.compile(r"/batches/(?P<batch_code>[^/]+)"), _get_batch),
    ("POST", re.compile(r"/inspections"), _create_inspection),
    ("GET", re.compile(r"/inspections/(?P<inspection_id>[^/]+)"), _get_inspection),
    ("POST", re.compile(r"/inspections/(?P<inspection_id>[^/]+)/appeals"), _file_appeal),
    ("POST", re.compile(r"/appeals/(?P<appeal_id>[^/]+)/decision"), _decide_appeal),
    ("POST", re.compile(r"/recalls"), _decide_recall),
    ("GET", re.compile(r"/scan/(?P<batch_code>[^/]+)"), _scan),
    ("GET", re.compile(r"/trace/claims/(?P<claim_text>.+)"), _trace_claim),
]


class Handler(BaseHTTPRequestHandler):
    """提供健康检查、领域契约与全谷物合规业务接口。"""

    app = ComplianceService(Store())

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    self._send_json({"error": "请求体不是合法JSON"}, 400)
                    return
                if not isinstance(body, dict):
                    self._send_json({"error": "请求体必须是JSON对象"}, 400)
                    return
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if not match:
                continue
            try:
                payload = handler(self.app, match.groupdict(), query, body)
            except DomainError as error:
                self._send_json({"error": str(error)}, error.status)
            except Exception as error:  # 兜底，避免堆栈直接暴露给调用方
                self._send_json({"error": f"内部错误: {error}"}, 500)
            else:
                self._send_json(payload, 200)
            return
        self.send_error(404)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data-file", help="可选的JSON数据文件，用于持久化全部记录")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        ComplianceService(Store())
        print("基础检查通过")
        return
    store = Store(args.data_file) if args.data_file else Store()
    Handler.app = ComplianceService(store)
    print(f"{SERVICE_NAME} 已启动: http://0.0.0.0:{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
