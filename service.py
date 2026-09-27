"""全谷物标签合规库的服务入口与 HTTP 接口。"""
import argparse
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from compliance import ComplianceService
from domain import DomainError, NotFoundError, reject_direct_ratio

SERVICE_ID = "whole-grain-label"
SERVICE_NAME = "全谷物标签合规库"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """合规接口：组成计算、标准匹配、审核、包装、抽检与追溯。"""

    service = ComplianceService()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if method == "GET":
                payload = self._route_get(parts, parse_qs(parsed.query))
            else:
                payload = self._route_post(parts, self._read_body())
        except NotFoundError as exc:
            return self._send_json({"error": str(exc)}, 404)
        except KeyError as exc:
            return self._send_json({"error": f"缺少字段: {exc}"}, 400)
        except DomainError as exc:
            return self._send_json({"error": str(exc)}, 400)
        if payload is None:
            return self._send_json({"error": "not found"}, 404)
        self._send_json(payload)

    def _route_get(self, parts, query):
        svc = self.service
        if parts == ["health"]:
            return health_payload()
        if parts == ["contract"]:
            return load_contract()
        if parts == ["standards", "match"]:
            rule = svc.applicable_standard(
                query["category"][0], query["region"][0], query.get("date", [None])[0]
            )
            return asdict(rule)
        if len(parts) == 2 and parts[0] == "recipes":
            return svc.recipe_view(parts[1])
        if len(parts) == 3 and parts[0] == "recipes" and parts[2] == "composition":
            return asdict(svc.recipe_composition(parts[1]))
        if len(parts) == 2 and parts[0] == "sampling-reports":
            return svc.report_view(parts[1])
        if len(parts) == 2 and parts[0] == "scan":
            return svc.scan(parts[1])
        if len(parts) == 3 and parts[0] == "trace" and parts[1] == "claims":
            return svc.trace_claim(parts[2])
        return None

    def _route_post(self, parts, body):
        svc = self.service
        if parts == ["supply-batches"]:
            return asdict(
                svc.register_supply_batch(
                    body["supplier_id"], body["ingredient_id"], body.get("received_at")
                )
            )
        if parts == ["evidence"]:
            return asdict(
                svc.add_evidence(
                    body["kind"],
                    body["subject_id"],
                    body.get("source", {}),
                    body["ranges"],
                    body.get("recorded_at"),
                )
            )
        if parts == ["recipes"]:
            reject_direct_ratio(body)
            return asdict(
                svc.create_recipe(
                    body["product_id"],
                    body["product_category"],
                    body["region"],
                    body["lines"],
                )
            )
        if parts == ["standards"]:
            return asdict(
                svc.publish_rule(
                    body["issuer_level"],
                    body["product_category"],
                    body["region"],
                    body["effective_from"],
                    body["min_whole_grain_ratio"],
                    body["definition_text"],
                    body.get("effective_to"),
                )
            )
        if parts == ["reviews"]:
            return asdict(svc.trigger_review(body["trigger"], body["recipe_id"]))
        if len(parts) == 3 and parts[0] == "reviews" and parts[2] == "decision":
            return asdict(
                svc.decide_review(
                    parts[1], body["approver"], bool(body["approved"]), body.get("note", "")
                )
            )
        if parts == ["packaging-batches"]:
            return asdict(
                svc.register_packaging(
                    body["recipe_id"],
                    body["design_version"],
                    body["quantity"],
                    body.get("produced_at"),
                    bool(body.get("is_old_stock", False)),
                )
            )
        if parts == ["transitions"]:
            return asdict(
                svc.approve_transition(
                    body["recipe_id"],
                    body["design_version"],
                    body["valid_from"],
                    body["valid_until"],
                    body["approved_by"],
                    body.get("reason", ""),
                )
            )
        if parts == ["sampling-reports"]:
            return asdict(
                svc.file_sampling_report(
                    body["batch_code"], body["agency"], body["method"], body["measured_ratio"]
                )
            )
        if len(parts) == 3 and parts[0] == "sampling-reports" and parts[2] == "appeals":
            return asdict(svc.file_appeal(parts[1], body["company"], body["argument"]))
        if len(parts) == 3 and parts[0] == "sampling-reports" and parts[2] == "recalls":
            return asdict(svc.file_recall(parts[1], body["decided_by"], body["scope"]))
        if parts == ["claims"]:
            return asdict(
                svc.register_claim(body["text"], body["recipe_id"], body["registered_by"])
            )
        return None

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON")

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
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
