"""内存存储：所有记录仅可追加，同键写入即拒绝。"""
from domain import DomainError


class Store:
    """按实体类型分桶的内存仓库。"""

    def __init__(self):
        self.supply_batches = {}
        self.evidence = {}
        self.recipes = {}
        self.rules = {}
        self.reviews = {}
        self.packaging = {}
        self.transitions = {}
        self.sampling_reports = {}
        self.appeals = {}
        self.recalls = {}
        self.claims = {}
        self._counters = {}

    def next_id(self, prefix):
        """生成可读的单调配号。"""
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def put(self, collection, key, record):
        """追加式写入：同键记录已存在时拒绝，保证原始记录不被覆盖。"""
        if key in collection:
            raise DomainError(f"记录 {key} 已存在，原始数据不可覆盖")
        collection[key] = record
        return record
