"""仅追加的记录存储。

所有领域记录一旦写入即不可修改、不可删除；配方与批次的状态由记录流
推导而非被改写，从存储层面支撑"抽检差异、企业申辩和召回决定不得覆盖
原报告"的业务原则。可选地以 JSON 文件持久化，重启后完整恢复。
"""
import json
import os
from dataclasses import asdict

from . import models

COLLECTIONS = {
    "rules": models.StandardRule,
    "ingredients": models.Ingredient,
    "suppliers": models.Supplier,
    "supply_batches": models.SupplyBatch,
    "products": models.Product,
    "recipes": models.RecipeVersion,
    "evidence": models.Evidence,
    "reviews": models.Review,
    "review_decisions": models.ReviewDecision,
    "packaging": models.PackagingVersion,
    "transition_approvals": models.TransitionApproval,
    "batches": models.ProductionBatch,
    "inspections": models.InspectionReport,
    "appeals": models.Appeal,
    "appeal_decisions": models.AppealDecision,
    "recalls": models.RecallDecision,
}


def _from_dict(cls, payload):
    custom = getattr(cls, "from_dict", None)
    if custom is not None:
        return custom(payload)
    return cls(**payload)


class Store:
    """按集合分组的仅追加存储，只提供 append / all / next_id。"""

    def __init__(self, path=None):
        self._path = path
        self._data = {name: [] for name in COLLECTIONS}
        self._counters = {}
        if path and os.path.exists(path):
            self._load()

    def next_id(self, prefix, width=4):
        """生成单调递增的业务编号，如 RULE-0001、WG-000001。"""
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:0{width}d}"

    def append(self, collection, record):
        if collection not in self._data:
            raise KeyError(f"未知集合: {collection}")
        self._data[collection].append(record)
        self._persist()
        return record

    def all(self, collection):
        if collection not in self._data:
            raise KeyError(f"未知集合: {collection}")
        return tuple(self._data[collection])

    def _persist(self):
        if not self._path:
            return
        payload = {
            "counters": self._counters,
            "collections": {
                name: [asdict(record) for record in records]
                for name, records in self._data.items()
            },
        }
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self._path)

    def _load(self):
        with open(self._path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self._counters = payload.get("counters", {})
        for name, records in payload.get("collections", {}).items():
            cls = COLLECTIONS[name]
            self._data[name] = [_from_dict(cls, record) for record in records]
