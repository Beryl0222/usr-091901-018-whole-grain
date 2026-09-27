"""全谷物标签合规的应用服务。

职责：
- 由配方明细（谷物原料、精制方式、投料比例、供应批次）计算全谷物比例，
  企业不能绕过明细直接填报最终百分比；
- 实验室结果与生产损耗作为带来源的证据，共同推导真实比例区间；
- 按产品类别、地区、生效时间匹配并行标准，宣称须满足全部适用标准的
  最严阈值（以比例区间下界判定）；
- 管理试产、量产、改配方、换供应商四类审核，以及旧包装获准过渡期；
- 提供消费者扫码视图与监管追溯视图。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from .errors import NotFound, StateError, ValidationError
from .models import (
    APPEAL_OUTCOMES,
    APPEAL_PENDING,
    APPEAL_OVERTURNED,
    AUTO_TRIGGERS,
    BATCH_INSPECTION,
    BATCH_NORMAL,
    BATCH_RECALLED,
    BATCH_TRANSITION,
    DECISION_APPROVE,
    DECISION_REJECT,
    EVIDENCE_KINDS,
    EVIDENCE_LAB,
    EVIDENCE_LOSS,
    LEVEL_RANK,
    RECIPE_DRAFT,
    RECIPE_LABEL_APPROVED,
    RECIPE_TRIAL_VERIFIED,
    REFINE_NONE,
    REFINE_PARTIAL,
    REFINE_REFINED,
    REFINE_WHOLE,
    REVIEW_APPROVED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    RULE_LEVELS,
    TRIGGER_MASS,
    TRIGGER_RECIPE_CHANGE,
    TRIGGER_SUPPLIER_CHANGE,
    TRIGGER_TRIAL,
    Appeal,
    AppealDecision,
    Evidence,
    Ingredient,
    InspectionReport,
    PackagingVersion,
    Product,
    ProductionBatch,
    RecallDecision,
    RecipeLine,
    RecipeVersion,
    Review,
    ReviewDecision,
    StandardRule,
    Supplier,
    SupplyBatch,
    TransitionApproval,
    refine_content_ok,
)
from .store import Store

CERTIFICATION_LABELS = {
    BATCH_NORMAL: "认证有效",
    BATCH_TRANSITION: "过渡期内销售",
    BATCH_INSPECTION: "抽检复核中",
    BATCH_RECALLED: "已召回",
}

RATIO_SUM_TOLERANCE = 1e-6


def _round6(value):
    return round(value, 6)


def _check_date(value, field):
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是YYYY-MM-DD格式的字符串")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValidationError(f"{field}不是合法日期: {value}")
    return value


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    return value.strip()


class ComplianceService:
    """全谷物标签合规的应用服务入口。"""

    def __init__(self, store=None, clock=None):
        self.store = store or Store()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return self._clock().isoformat(timespec="seconds")

    # ---------- 基础查询 ----------
    def _find(self, collection, field, value):
        for record in self.store.all(collection):
            if getattr(record, field) == value:
                return record
        return None

    def _get(self, collection, field, value, label):
        record = self._find(collection, field, value)
        if record is None:
            raise NotFound(f"{label}不存在: {value}")
        return record

    # ---------- 标准规则 ----------
    def publish_rule(self, *, name, level, product_category, region, effective_from,
                     effective_to=None, claim_thresholds, issued_by):
        """发布标准规则：按产品类别、地区和生效时间生效。"""
        if level not in RULE_LEVELS:
            raise ValidationError(f"标准级别必须是: {'、'.join(RULE_LEVELS)}")
        _require_text(name, "标准名称")
        _require_text(product_category, "产品类别")
        _require_text(region, "适用地区")
        _require_text(issued_by, "发布方")
        _check_date(effective_from, "生效日期")
        if effective_to is not None:
            _check_date(effective_to, "失效日期")
            if effective_to < effective_from:
                raise ValidationError("失效日期不能早于生效日期")
        if not isinstance(claim_thresholds, dict) or not claim_thresholds:
            raise ValidationError("标准必须定义至少一个宣称词的全谷物比例阈值")
        thresholds = {}
        for term, threshold in claim_thresholds.items():
            _require_text(term, "宣称词")
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                raise ValidationError(f"宣称[{term}]的阈值必须是数字")
            if not 0 < threshold <= 1:
                raise ValidationError(f"宣称[{term}]的阈值必须在(0,1]区间")
            thresholds[term.strip()] = float(threshold)
        rule = StandardRule(
            rule_id=self.store.next_id("RULE"),
            name=name.strip(),
            level=level,
            product_category=product_category.strip(),
            region=region.strip(),
            effective_from=effective_from,
            effective_to=effective_to,
            claim_thresholds=thresholds,
            issued_by=issued_by.strip(),
            published_at=self._now(),
        )
        self.store.append("rules", rule)
        return asdict(rule)

    def list_rules(self, category=None, region=None, date=None):
        if date is not None:
            _check_date(date, "日期")
        result = []
        for rule in self.store.all("rules"):
            if category is not None and rule.product_category != category:
                continue
            if region is not None and rule.region != region:
                continue
            if date is not None and not self._in_effect(rule, date):
                continue
            result.append(asdict(rule))
        return result

    @staticmethod
    def _in_effect(rule, date):
        return rule.effective_from <= date and (rule.effective_to is None or date <= rule.effective_to)

    def resolve_rules(self, category, region, date):
        """匹配某批产品上市时适用的全部规则，并给出主导规则。

        主导规则按 级别(国家>行业>团体) > 生效时间 > 发布顺序 取最优先；
        宣称判定时须同时满足全部适用规则中定义该宣称的最严阈值。
        """
        _check_date(date, "日期")
        applicable = []
        for index, rule in enumerate(self.store.all("rules")):
            if rule.product_category not in (category, "*", "全部"):
                continue
            if rule.region not in (region, "全国"):
                continue
            if not self._in_effect(rule, date):
                continue
            applicable.append((index, rule))
        governing = None
        if applicable:
            governing = max(
                applicable,
                key=lambda pair: (LEVEL_RANK[pair[1].level], pair[1].effective_from, pair[0]),
            )[1]
        return [rule for _, rule in applicable], governing

    def match_rules(self, category, region, date):
        applicable, governing = self.resolve_rules(category, region, date)
        return {
            "applicable": [asdict(rule) for rule in applicable],
            "governing": asdict(governing) if governing else None,
        }

    # ---------- 基础数据 ----------
    def register_ingredient(self, *, name, grain_type="", is_grain):
        _require_text(name, "原料名称")
        if not isinstance(is_grain, bool):
            raise ValidationError("is_grain必须是布尔值")
        if is_grain:
            _require_text(grain_type, "谷物种类")
        ingredient = Ingredient(
            ingredient_id=self.store.next_id("ING"),
            name=name.strip(),
            grain_type=grain_type.strip() if is_grain else "",
            is_grain=is_grain,
        )
        self.store.append("ingredients", ingredient)
        return asdict(ingredient)

    def list_ingredients(self):
        return [asdict(item) for item in self.store.all("ingredients")]

    def register_supplier(self, *, name, region):
        _require_text(name, "供应方名称")
        _require_text(region, "供应方地区")
        supplier = Supplier(
            supplier_id=self.store.next_id("SUP"), name=name.strip(), region=region.strip()
        )
        self.store.append("suppliers", supplier)
        return asdict(supplier)

    def list_suppliers(self):
        return [asdict(item) for item in self.store.all("suppliers")]

    def register_supply_batch(self, *, supplier_id, ingredient_id, batch_no,
                              whole_grain_content, certificate_ref):
        """登记供应批次及其全谷物含量证明（组成计算的来源之一）。"""
        self._get("suppliers", "supplier_id", supplier_id, "供应方")
        ingredient = self._get("ingredients", "ingredient_id", ingredient_id, "原料")
        if not ingredient.is_grain:
            raise ValidationError("供应批次仅用于谷物原料")
        if isinstance(whole_grain_content, bool):
            raise ValidationError("全谷物含量必须是数字")
        content = float(whole_grain_content)
        if not 0.0 <= content <= 1.0:
            raise ValidationError("供应批次全谷物含量必须在[0,1]区间")
        _require_text(batch_no, "供应批号")
        _require_text(certificate_ref, "全谷物含量证明")
        batch = SupplyBatch(
            supply_batch_id=self.store.next_id("SB"),
            supplier_id=supplier_id,
            ingredient_id=ingredient_id,
            batch_no=batch_no.strip(),
            whole_grain_content=content,
            certificate_ref=certificate_ref.strip(),
            received_at=self._now(),
        )
        self.store.append("supply_batches", batch)
        return asdict(batch)

    def list_supply_batches(self, ingredient_id=None):
        return [
            asdict(item) for item in self.store.all("supply_batches")
            if ingredient_id is None or item.ingredient_id == ingredient_id
        ]

    def register_product(self, *, name, category, markets):
        _require_text(name, "产品名称")
        _require_text(category, "产品类别")
        if not isinstance(markets, (list, tuple)) or not markets:
            raise ValidationError("产品必须登记至少一个销售区域")
        cleaned = []
        for market in markets:
            cleaned.append(_require_text(market, "销售区域"))
        product = Product(
            product_id=self.store.next_id("PRD"),
            name=name.strip(),
            category=category.strip(),
            markets=tuple(dict.fromkeys(cleaned)),
        )
        self.store.append("products", product)
        return asdict(product)

    def list_products(self):
        return [asdict(item) for item in self.store.all("products")]

    # ---------- 配方与组成计算 ----------
    def create_recipe(self, product_id, *, lines, created_by, note=""):
        """新建配方版本。全谷物比例只能由明细计算，不接受直接填报。

        若产品已有获准使用标签的版本，自动触发"改配方"审核；若供应方集合
        发生变化，同时自动触发"换供应商"审核。
        """
        product = self._get("products", "product_id", product_id, "产品")
        _require_text(created_by, "配方创建人")
        if not isinstance(lines, (list, tuple)) or not lines:
            raise ValidationError("配方必须包含原料明细行")
        built = []
        total = 0.0
        for index, raw in enumerate(lines, start=1):
            if not isinstance(raw, dict):
                raise ValidationError(f"第{index}行明细必须是对象")
            ingredient = self._get("ingredients", "ingredient_id", raw.get("ingredient_id"), "原料")
            ratio_value = raw.get("ratio")
            if isinstance(ratio_value, bool) or not isinstance(ratio_value, (int, float)):
                raise ValidationError(f"第{index}行投料比例必须是数字")
            ratio = float(ratio_value)
            if not 0.0 < ratio <= 1.0:
                raise ValidationError(f"第{index}行投料比例必须在(0,1]区间")
            refining = raw.get("refining")
            supply_batch_id = raw.get("supply_batch_id")
            if ingredient.is_grain:
                if refining not in (REFINE_WHOLE, REFINE_PARTIAL, REFINE_REFINED):
                    raise ValidationError(f"第{index}行谷物原料必须标注精制方式(全粒/部分精制/精制)")
                if not supply_batch_id:
                    raise ValidationError(f"第{index}行谷物原料必须关联供应批次")
                supply = self._get("supply_batches", "supply_batch_id", supply_batch_id, "供应批次")
                if supply.ingredient_id != ingredient.ingredient_id:
                    raise ValidationError(f"第{index}行供应批次与原料不一致")
                if not refine_content_ok(refining, supply.whole_grain_content):
                    raise ValidationError(
                        f"第{index}行精制方式[{refining}]与供应批次全谷物含量"
                        f"{supply.whole_grain_content}不一致"
                    )
            else:
                if refining not in (None, REFINE_NONE):
                    raise ValidationError(f"第{index}行非谷物原料的精制方式应为[无]")
                if supply_batch_id:
                    raise ValidationError(f"第{index}行非谷物原料不应关联供应批次")
                refining = REFINE_NONE
                supply_batch_id = None
            built.append(RecipeLine(
                ingredient_id=ingredient.ingredient_id,
                refining=refining,
                ratio=ratio,
                supply_batch_id=supply_batch_id,
            ))
            total += ratio
        if abs(total - 1.0) > RATIO_SUM_TOLERANCE:
            raise ValidationError(f"投料比例合计必须为100%，当前为{total:.4%}")
        version = 1 + max(
            (r.version for r in self.store.all("recipes") if r.product_id == product.product_id),
            default=0,
        )
        recipe = RecipeVersion(
            recipe_id=self.store.next_id("RCP"),
            product_id=product.product_id,
            version=version,
            lines=tuple(built),
            created_by=created_by.strip(),
            created_at=self._now(),
            note=note or "",
        )
        self.store.append("recipes", recipe)
        previous = self._latest_approved_recipe(product.product_id, exclude=recipe.recipe_id)
        if previous is not None:
            self._auto_review(recipe, TRIGGER_RECIPE_CHANGE, created_by.strip())
            if self._suppliers_of(recipe) != self._suppliers_of(previous):
                self._auto_review(recipe, TRIGGER_SUPPLIER_CHANGE, created_by.strip())
        return self.recipe_detail(recipe.recipe_id)

    def _auto_review(self, recipe, trigger, requested_by):
        review = Review(
            review_id=self.store.next_id("REV"),
            recipe_id=recipe.recipe_id,
            trigger=trigger,
            requested_by=requested_by,
            requested_at=self._now(),
            auto=True,
        )
        self.store.append("reviews", review)
        return review

    def _suppliers_of(self, recipe):
        suppliers = set()
        for line in recipe.lines:
            if line.supply_batch_id:
                batch = self._get("supply_batches", "supply_batch_id", line.supply_batch_id, "供应批次")
                suppliers.add(batch.supplier_id)
        return suppliers

    def _latest_approved_recipe(self, product_id, exclude=None):
        latest = None
        for recipe in self.store.all("recipes"):
            if recipe.product_id != product_id or recipe.recipe_id == exclude:
                continue
            if self.recipe_status(recipe.recipe_id) == RECIPE_LABEL_APPROVED:
                latest = recipe
        return latest

    def composition_of(self, recipe_id):
        """计算配方组成：全谷物比例与真实比例区间。

        计算值 = Σ(投料比例 × 供应批次全谷物含量)；
        区间下界 = min(计算值 × (1 - 最大损耗率), 各检测结果 - 不确定度)；
        区间上界 = max(计算值, 各检测结果 + 不确定度)。
        """
        recipe = self._get("recipes", "recipe_id", recipe_id, "配方版本")
        computed = 0.0
        line_details = []
        for line in recipe.lines:
            ingredient = self._get("ingredients", "ingredient_id", line.ingredient_id, "原料")
            factor = 0.0
            batch_no = None
            if ingredient.is_grain:
                supply = self._get("supply_batches", "supply_batch_id", line.supply_batch_id, "供应批次")
                factor = supply.whole_grain_content
                batch_no = supply.batch_no
            contribution = line.ratio * factor
            computed += contribution
            line_details.append({
                "ingredient_id": ingredient.ingredient_id,
                "ingredient_name": ingredient.name,
                "is_grain": ingredient.is_grain,
                "refining": line.refining,
                "ratio": _round6(line.ratio),
                "whole_grain_factor": _round6(factor),
                "contribution": _round6(contribution),
                "supply_batch_id": line.supply_batch_id,
                "supply_batch_no": batch_no,
            })
        evidence = [e for e in self.store.all("evidence") if e.recipe_id == recipe_id]
        losses = [e.value for e in evidence if e.kind == EVIDENCE_LOSS]
        labs = [e for e in evidence if e.kind == EVIDENCE_LAB]
        after_loss = computed * (1.0 - max(losses)) if losses else computed
        low, high = after_loss, computed
        for lab in labs:
            low = min(low, lab.value - lab.uncertainty)
            high = max(high, lab.value + lab.uncertainty)
        low = min(max(low, 0.0), 1.0)
        high = min(max(high, 0.0), 1.0)
        return {
            "recipe_id": recipe_id,
            "computed_ratio": _round6(computed),
            "range_low": _round6(low),
            "range_high": _round6(high),
            "max_production_loss": _round6(max(losses)) if losses else 0.0,
            "lines": line_details,
            "evidence": [asdict(e) for e in evidence],
        }

    def add_evidence(self, recipe_id, *, kind, source, value, uncertainty=0.0,
                     method, recorded_by):
        """登记带来源的证据：实验室检测结果或生产损耗。"""
        self._get("recipes", "recipe_id", recipe_id, "配方版本")
        if kind not in EVIDENCE_KINDS:
            raise ValidationError(f"证据类型必须是: {'、'.join(EVIDENCE_KINDS)}")
        _require_text(source, "证据来源")
        _require_text(method, "检测方法或统计口径")
        _require_text(recorded_by, "记录人")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("证据数值必须是数字")
        value = float(value)
        uncertainty = float(uncertainty or 0.0)
        if kind == EVIDENCE_LAB:
            if not 0.0 <= value <= 1.0:
                raise ValidationError("检测比例必须在[0,1]区间")
            if uncertainty < 0:
                raise ValidationError("不确定度不能为负")
        else:
            if not 0.0 <= value < 1.0:
                raise ValidationError("生产损耗率必须在[0,1)区间")
            uncertainty = 0.0
        evidence = Evidence(
            evidence_id=self.store.next_id("EVD"),
            recipe_id=recipe_id,
            kind=kind,
            source=source.strip(),
            value=value,
            uncertainty=uncertainty,
            method=method.strip(),
            recorded_by=recorded_by.strip(),
            recorded_at=self._now(),
        )
        self.store.append("evidence", evidence)
        return asdict(evidence)

    def recipe_detail(self, recipe_id):
        recipe = self._get("recipes", "recipe_id", recipe_id, "配方版本")
        payload = asdict(recipe)
        payload["status"] = self.recipe_status(recipe_id)
        payload["composition"] = self.composition_of(recipe_id)
        payload["reviews"] = [
            self._review_dict(review)
            for review in self.store.all("reviews")
            if review.recipe_id == recipe_id
        ]
        return payload

    # ---------- 审核 ----------
    def _decisions_by_review(self):
        return {d.review_id: d for d in self.store.all("review_decisions")}

    def recipe_status(self, recipe_id):
        """配方状态由审核记录推导：草拟 -> 试产核验 -> 允许使用标签。"""
        decisions = self._decisions_by_review()
        trial_ok = mass_ok = False
        for review in self.store.all("reviews"):
            if review.recipe_id != recipe_id:
                continue
            decision = decisions.get(review.review_id)
            if decision is None or decision.decision != DECISION_APPROVE:
                continue
            if review.trigger == TRIGGER_TRIAL:
                trial_ok = True
            elif review.trigger == TRIGGER_MASS:
                mass_ok = True
        if mass_ok:
            return RECIPE_LABEL_APPROVED
        if trial_ok:
            return RECIPE_TRIAL_VERIFIED
        return RECIPE_DRAFT

    def _review_dict(self, review):
        decision = self._find("review_decisions", "review_id", review.review_id)
        payload = asdict(review)
        if decision is None:
            payload["status"] = REVIEW_PENDING
        else:
            payload["status"] = REVIEW_APPROVED if decision.decision == DECISION_APPROVE else REVIEW_REJECTED
        payload["decision"] = asdict(decision) if decision else None
        return payload

    def request_review(self, recipe_id, trigger, requested_by):
        """发起试产或量产审核；改配方/换供应商审核由系统自动触发。"""
        self._get("recipes", "recipe_id", recipe_id, "配方版本")
        if trigger in AUTO_TRIGGERS:
            raise ValidationError("改配方与换供应商审核由系统在新建版本时自动触发")
        if trigger not in (TRIGGER_TRIAL, TRIGGER_MASS):
            raise ValidationError(f"未知审核触发类型: {trigger}")
        _require_text(requested_by, "申请人")
        status = self.recipe_status(recipe_id)
        reviews = [r for r in self.store.all("reviews") if r.recipe_id == recipe_id]
        decisions = self._decisions_by_review()
        if trigger == TRIGGER_TRIAL:
            if status != RECIPE_DRAFT:
                raise StateError(f"配方当前处于[{status}]，不能重复发起试产审核")
            if any(r.trigger == TRIGGER_TRIAL and r.review_id not in decisions for r in reviews):
                raise StateError("已有待审核的试产申请")
        else:
            if status != RECIPE_TRIAL_VERIFIED:
                raise StateError("试产核验通过后才能申请量产审核")
            for review in reviews:
                if review.trigger in AUTO_TRIGGERS:
                    decision = decisions.get(review.review_id)
                    if decision is None or decision.decision != DECISION_APPROVE:
                        raise StateError(f"{review.trigger}审核未通过，不能申请量产审核")
            if any(r.trigger == TRIGGER_MASS and r.review_id not in decisions for r in reviews):
                raise StateError("已有待审核的量产申请")
        review = Review(
            review_id=self.store.next_id("REV"),
            recipe_id=recipe_id,
            trigger=trigger,
            requested_by=requested_by.strip(),
            requested_at=self._now(),
            auto=False,
        )
        self.store.append("reviews", review)
        return self._review_dict(review)

    def decide_review(self, review_id, decision, approver, comment=""):
        """审核决定是独立追加的记录，一经作出不得更改。"""
        review = self._get("reviews", "review_id", review_id, "审核")
        if decision not in (DECISION_APPROVE, DECISION_REJECT):
            raise ValidationError(f"审核决定只能是[{DECISION_APPROVE}]或[{DECISION_REJECT}]")
        _require_text(approver, "批准人")
        if self._find("review_decisions", "review_id", review_id):
            raise StateError("该审核已有决定，不得更改")
        self.store.append("review_decisions", ReviewDecision(
            review_id=review_id,
            decision=decision,
            approver=approver.strip(),
            decided_at=self._now(),
            comment=comment or "",
        ))
        return self._review_dict(review)

    def list_reviews(self, recipe_id):
        self._get("recipes", "recipe_id", recipe_id, "配方版本")
        return [
            self._review_dict(review)
            for review in self.store.all("reviews")
            if review.recipe_id == recipe_id
        ]

    # ---------- 包装与过渡期 ----------
    def create_packaging(self, product_id, *, claims, design_ref, created_by):
        self._get("products", "product_id", product_id, "产品")
        if not isinstance(claims, (list, tuple)) or not claims:
            raise ValidationError("包装必须至少包含一个宣称词")
        cleaned = []
        for claim in claims:
            cleaned.append(_require_text(claim, "宣称词"))
        _require_text(design_ref, "包装设计引用")
        _require_text(created_by, "创建人")
        packaging = PackagingVersion(
            packaging_id=self.store.next_id("PKG"),
            product_id=product_id,
            claims=tuple(dict.fromkeys(cleaned)),
            design_ref=design_ref.strip(),
            created_by=created_by.strip(),
            created_at=self._now(),
        )
        self.store.append("packaging", packaging)
        return asdict(packaging)

    def _latest_packaging(self, product_id):
        latest = None
        for packaging in self.store.all("packaging"):
            if packaging.product_id == product_id:
                latest = packaging
        return latest

    def _claim_verdicts(self, product, market, date, claims, range_low):
        """宣称判定：须满足全部适用标准中定义该宣称的最严阈值。"""
        applicable, _governing = self.resolve_rules(product.category, market, date)
        verdicts = {}
        for claim in claims:
            basis = [(r.rule_id, r.claim_thresholds[claim]) for r in applicable
                     if claim in r.claim_thresholds]
            if not basis:
                verdicts[claim] = {
                    "allowed": False,
                    "threshold": None,
                    "range_low": _round6(range_low),
                    "basis_rules": [],
                    "reason": "适用标准中未定义该宣称",
                }
                continue
            threshold = max(value for _rid, value in basis)
            allowed = range_low >= threshold
            verdicts[claim] = {
                "allowed": allowed,
                "threshold": _round6(threshold),
                "range_low": _round6(range_low),
                "basis_rules": [rid for rid, _value in basis],
                "reason": "" if allowed else "比例区间下界低于适用标准阈值",
            }
        return verdicts

    def validate_packaging(self, packaging_id, market, date):
        """预校验包装宣称：以产品最新获准配方的比例区间下界判定。"""
        packaging = self._get("packaging", "packaging_id", packaging_id, "包装版本")
        product = self._get("products", "product_id", packaging.product_id, "产品")
        _check_date(date, "日期")
        recipe = self._latest_approved_recipe(product.product_id)
        if recipe is None:
            raise StateError("产品尚无获准使用标签的配方版本")
        composition = self.composition_of(recipe.recipe_id)
        verdicts = self._claim_verdicts(product, market, date, packaging.claims,
                                        composition["range_low"])
        return {
            "packaging_id": packaging_id,
            "recipe_id": recipe.recipe_id,
            "market": market,
            "date": date,
            "range_low": composition["range_low"],
            "range_high": composition["range_high"],
            "verdicts": verdicts,
        }

    def approve_transition(self, packaging_id, *, approved_until, max_quantity,
                           approved_by, reason):
        """批准旧包装库存在过渡期内继续使用。"""
        self._get("packaging", "packaging_id", packaging_id, "包装版本")
        _check_date(approved_until, "过渡期截止日")
        if isinstance(max_quantity, bool) or not isinstance(max_quantity, int) or max_quantity <= 0:
            raise ValidationError("过渡期批准数量必须是正整数")
        _require_text(approved_by, "批准人")
        _require_text(reason, "批准理由")
        approval = TransitionApproval(
            approval_id=self.store.next_id("TRN"),
            packaging_id=packaging_id,
            approved_until=approved_until,
            max_quantity=max_quantity,
            approved_by=approved_by.strip(),
            reason=reason.strip(),
            created_at=self._now(),
        )
        self.store.append("transition_approvals", approval)
        return asdict(approval)

    # ---------- 生产批次 ----------
    def create_batch(self, recipe_id, packaging_id, *, market, quantity, production_date):
        """创建生产批次：配方须获准、包装须为现行版或在获准过渡期内、
        包装宣称须通过适用标准校验。"""
        recipe = self._get("recipes", "recipe_id", recipe_id, "配方版本")
        status = self.recipe_status(recipe_id)
        if status != RECIPE_LABEL_APPROVED:
            raise StateError(f"配方当前处于[{status}]，尚未获准使用标签")
        packaging = self._get("packaging", "packaging_id", packaging_id, "包装版本")
        if packaging.product_id != recipe.product_id:
            raise ValidationError("包装版本与配方不属于同一产品")
        product = self._get("products", "product_id", recipe.product_id, "产品")
        _require_text(market, "销售区域")
        if market not in product.markets:
            raise ValidationError(f"市场[{market}]不在产品登记的销售区域内")
        _check_date(production_date, "生产日期")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError("生产数量必须是正整数")
        approval_id = None
        latest = self._latest_packaging(product.product_id)
        if latest is None or latest.packaging_id != packaging_id:
            approval = self._valid_transition(packaging_id, production_date, quantity)
            approval_id = approval.approval_id
        composition = self.composition_of(recipe_id)
        verdicts = self._claim_verdicts(product, market, production_date,
                                        packaging.claims, composition["range_low"])
        failed = [claim for claim, verdict in verdicts.items() if not verdict["allowed"]]
        if failed:
            raise StateError("标签宣称未通过合规校验: " + "、".join(failed))
        batch = ProductionBatch(
            batch_code=self.store.next_id("WG", width=6),
            recipe_id=recipe_id,
            packaging_id=packaging_id,
            product_id=product.product_id,
            market=market,
            quantity=quantity,
            production_date=production_date,
            transition_approval_id=approval_id,
            created_at=self._now(),
        )
        self.store.append("batches", batch)
        return self.batch_detail(batch.batch_code)

    def _valid_transition(self, packaging_id, production_date, quantity):
        candidates = [
            a for a in self.store.all("transition_approvals")
            if a.packaging_id == packaging_id and a.approved_until >= production_date
        ]
        if not candidates:
            raise StateError("旧包装库存仅能在获准过渡期内使用")
        approval = candidates[-1]
        used = sum(
            b.quantity for b in self.store.all("batches")
            if b.transition_approval_id == approval.approval_id
        )
        if used + quantity > approval.max_quantity:
            raise StateError(
                f"过渡期批准数量不足: 已用{used}，本次{quantity}，上限{approval.max_quantity}"
            )
        return approval

    def batch_status(self, batch_code):
        """批次状态由记录流推导：召回 > 抽检复核 > 过渡销售 > 正常流通。"""
        batch = self._get("batches", "batch_code", batch_code, "生产批次")
        if self._find("recalls", "batch_code", batch_code):
            return BATCH_RECALLED
        inspections = [i for i in self.store.all("inspections") if i.batch_code == batch_code]
        if inspections:
            latest = inspections[-1]
            appeal = self._find("appeals", "inspection_id", latest.inspection_id)
            overturned = False
            if appeal is not None:
                decision = self._find("appeal_decisions", "appeal_id", appeal.appeal_id)
                overturned = bool(decision and decision.outcome == APPEAL_OVERTURNED)
            if not overturned:
                return BATCH_INSPECTION
        if batch.transition_approval_id:
            return BATCH_TRANSITION
        return BATCH_NORMAL

    def batch_detail(self, batch_code):
        batch = self._get("batches", "batch_code", batch_code, "生产批次")
        payload = asdict(batch)
        payload["status"] = self.batch_status(batch_code)
        return payload

    # ---------- 抽检、申辩、召回（仅追加，不覆盖原报告） ----------
    def record_inspection(self, batch_code, *, measured_ratio, uncertainty=0.0,
                          method, agency, inspector):
        """登记抽检报告，并快照当时的计算值与比例区间。"""
        batch = self._get("batches", "batch_code", batch_code, "生产批次")
        if isinstance(measured_ratio, bool) or not isinstance(measured_ratio, (int, float)):
            raise ValidationError("抽检比例必须是数字")
        measured = float(measured_ratio)
        uncertainty = float(uncertainty or 0.0)
        if not 0.0 <= measured <= 1.0:
            raise ValidationError("抽检比例必须在[0,1]区间")
        if uncertainty < 0:
            raise ValidationError("不确定度不能为负")
        _require_text(method, "检测方法")
        _require_text(agency, "检测机构")
        _require_text(inspector, "检验人")
        composition = self.composition_of(batch.recipe_id)
        low, high = composition["range_low"], composition["range_high"]
        within = (measured + uncertainty >= low) and (measured - uncertainty <= high)
        report = InspectionReport(
            inspection_id=self.store.next_id("INS"),
            batch_code=batch_code,
            measured_ratio=measured,
            uncertainty=uncertainty,
            method=method.strip(),
            agency=agency.strip(),
            inspector=inspector.strip(),
            computed_ratio=composition["computed_ratio"],
            range_low=low,
            range_high=high,
            within_range=within,
            created_at=self._now(),
        )
        self.store.append("inspections", report)
        return asdict(report)

    def inspection_detail(self, inspection_id):
        report = self._get("inspections", "inspection_id", inspection_id, "抽检报告")
        payload = asdict(report)
        appeal = self._find("appeals", "inspection_id", inspection_id)
        payload["appeal"] = self._appeal_dict(appeal) if appeal else None
        return payload

    def file_appeal(self, inspection_id, *, reason, appellant):
        """企业申辩：针对抽检报告的独立记录，不改动原报告。"""
        self._get("inspections", "inspection_id", inspection_id, "抽检报告")
        if self._find("appeals", "inspection_id", inspection_id):
            raise StateError("该抽检报告已有申辩记录")
        _require_text(reason, "申辩理由")
        _require_text(appellant, "申辩人")
        appeal = Appeal(
            appeal_id=self.store.next_id("APL"),
            inspection_id=inspection_id,
            reason=reason.strip(),
            appellant=appellant.strip(),
            created_at=self._now(),
        )
        self.store.append("appeals", appeal)
        return self._appeal_dict(appeal)

    def _appeal_dict(self, appeal):
        payload = asdict(appeal)
        decision = self._find("appeal_decisions", "appeal_id", appeal.appeal_id)
        payload["status"] = decision.outcome if decision else APPEAL_PENDING
        payload["decision"] = asdict(decision) if decision else None
        return payload

    def decide_appeal(self, appeal_id, *, outcome, decided_by, comment=""):
        """申辩结论（维持原报告/申辩成立）同样是追加记录。"""
        appeal = self._get("appeals", "appeal_id", appeal_id, "申辩")
        if outcome not in APPEAL_OUTCOMES:
            raise ValidationError(f"申辩结论必须是: {'、'.join(APPEAL_OUTCOMES)}")
        _require_text(decided_by, "决定人")
        if self._find("appeal_decisions", "appeal_id", appeal_id):
            raise StateError("该申辩已有结论，不得更改")
        self.store.append("appeal_decisions", AppealDecision(
            appeal_id=appeal_id,
            outcome=outcome,
            decided_by=decided_by.strip(),
            decided_at=self._now(),
            comment=comment or "",
        ))
        return self._appeal_dict(appeal)

    def decide_recall(self, batch_code, *, reason, decided_by):
        """召回决定：独立记录，不覆盖抽检报告。"""
        self._get("batches", "batch_code", batch_code, "生产批次")
        if self._find("recalls", "batch_code", batch_code):
            raise StateError("该批次已有召回决定")
        _require_text(reason, "召回理由")
        _require_text(decided_by, "决定人")
        recall = RecallDecision(
            recall_id=self.store.next_id("RCL"),
            batch_code=batch_code,
            reason=reason.strip(),
            decided_by=decided_by.strip(),
            created_at=self._now(),
        )
        self.store.append("recalls", recall)
        return asdict(recall)

    # ---------- 消费者扫码视图 ----------
    def scan(self, batch_code):
        """消费者扫码：该批次适用定义、真实比例区间和认证状态。"""
        batch = self._get("batches", "batch_code", batch_code, "生产批次")
        product = self._get("products", "product_id", batch.product_id, "产品")
        packaging = self._get("packaging", "packaging_id", batch.packaging_id, "包装版本")
        composition = self.composition_of(batch.recipe_id)
        applicable, governing = self.resolve_rules(
            product.category, batch.market, batch.production_date
        )
        verdicts = self._claim_verdicts(product, batch.market, batch.production_date,
                                        packaging.claims, composition["range_low"])
        status = self.batch_status(batch_code)
        transition = None
        if batch.transition_approval_id:
            approval = self._get("transition_approvals", "approval_id",
                                 batch.transition_approval_id, "过渡批准")
            transition = {
                "approval_id": approval.approval_id,
                "approved_until": approval.approved_until,
                "max_quantity": approval.max_quantity,
                "approved_by": approval.approved_by,
            }
        recall = self._find("recalls", "batch_code", batch_code)
        return {
            "batch_code": batch.batch_code,
            "product": {
                "product_id": product.product_id,
                "name": product.name,
                "category": product.category,
            },
            "production_date": batch.production_date,
            "market": batch.market,
            "packaging": {
                "packaging_id": packaging.packaging_id,
                "claims": list(packaging.claims),
            },
            "certification_status": CERTIFICATION_LABELS[status],
            "batch_status": status,
            "whole_grain_ratio": {
                "computed": composition["computed_ratio"],
                "range_low": composition["range_low"],
                "range_high": composition["range_high"],
            },
            "applicable_definitions": [self._rule_brief(rule) for rule in applicable],
            "governing_rule": self._rule_brief(governing) if governing else None,
            "claim_verdicts": verdicts,
            "transition": transition,
            "recall": asdict(recall) if recall else None,
        }

    @staticmethod
    def _rule_brief(rule):
        return {
            "rule_id": rule.rule_id,
            "name": rule.name,
            "level": rule.level,
            "claim_thresholds": rule.claim_thresholds,
            "effective_from": rule.effective_from,
            "effective_to": rule.effective_to,
            "issued_by": rule.issued_by,
        }

    # ---------- 监管追溯视图 ----------
    def trace_claim(self, claim_text):
        """从一句宣传语追到配方版本、检测方法、批准人及仍在流通的包装范围。"""
        _require_text(claim_text, "追溯的宣称内容")
        text = claim_text.strip()
        chains = []
        for packaging in self.store.all("packaging"):
            if not any(term in text or text in term for term in packaging.claims):
                continue
            product = self._get("products", "product_id", packaging.product_id, "产品")
            batches = [b for b in self.store.all("batches")
                       if b.packaging_id == packaging.packaging_id]
            recipe_ids = []
            circulating = []
            for batch in batches:
                status = self.batch_status(batch.batch_code)
                if batch.recipe_id not in recipe_ids:
                    recipe_ids.append(batch.recipe_id)
                if status == BATCH_RECALLED:
                    continue
                circulating.append({
                    "batch_code": batch.batch_code,
                    "market": batch.market,
                    "production_date": batch.production_date,
                    "quantity": batch.quantity,
                    "status": status,
                })
            chains.append({
                "packaging": {
                    "packaging_id": packaging.packaging_id,
                    "claims": list(packaging.claims),
                    "design_ref": packaging.design_ref,
                },
                "product": {
                    "product_id": product.product_id,
                    "name": product.name,
                    "category": product.category,
                },
                "recipes": [self._recipe_trace(recipe_id) for recipe_id in recipe_ids],
                "circulating_scope": {
                    "batches": circulating,
                    "total_quantity": sum(item["quantity"] for item in circulating),
                },
            })
        return {"claim": text, "chains": chains}

    def _recipe_trace(self, recipe_id):
        recipe = self._get("recipes", "recipe_id", recipe_id, "配方版本")
        composition = self.composition_of(recipe_id)
        reviews = []
        for review in self.store.all("reviews"):
            if review.recipe_id != recipe_id:
                continue
            decision = self._find("review_decisions", "review_id", review.review_id)
            reviews.append({
                "review_id": review.review_id,
                "trigger": review.trigger,
                "status": (
                    REVIEW_PENDING if decision is None
                    else REVIEW_APPROVED if decision.decision == DECISION_APPROVE
                    else REVIEW_REJECTED
                ),
                "approver": decision.approver if decision else None,
                "decided_at": decision.decided_at if decision else None,
            })
        return {
            "recipe_id": recipe.recipe_id,
            "version": recipe.version,
            "status": self.recipe_status(recipe_id),
            "computed_ratio": composition["computed_ratio"],
            "range_low": composition["range_low"],
            "range_high": composition["range_high"],
            "evidence": [
                {
                    "kind": item["kind"],
                    "method": item["method"],
                    "source": item["source"],
                    "value": item["value"],
                    "uncertainty": item["uncertainty"],
                    "recorded_by": item["recorded_by"],
                    "recorded_at": item["recorded_at"],
                }
                for item in composition["evidence"]
            ],
            "reviews": reviews,
        }
