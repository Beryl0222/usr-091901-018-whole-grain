"""全谷物标签合规核心服务：审核、包装、抽检与追溯。"""
from dataclasses import asdict, replace
from datetime import date

from domain import (
    STATUS_BLOCKED,
    STATUS_DRAFT,
    STATUS_IN_REVIEW,
    STATUS_LABEL_OK,
    STATUS_RECALL,
    STATUS_RECHECK,
    STATUS_TRANSITION,
    Appeal,
    DomainError,
    Evidence,
    EvidenceKind,
    IngredientLine,
    MarketingClaim,
    NotFoundError,
    PackagingBatch,
    RecallDecision,
    RecipeVersion,
    Refining,
    Review,
    ReviewTrigger,
    SamplingReport,
    SupplyBatch,
    TransitionWindow,
    compute_composition,
)
from standards import ISSUER_PRECEDENCE, StandardRule, match_standard
from store import Store

# 投料比例之和允许的误差
RATIO_SUM_TOLERANCE = 0.02
# 仍在流通的包装状态
CIRCULATING_STATUSES = (STATUS_LABEL_OK, STATUS_TRANSITION)


class ComplianceService:
    """围绕追加式存储编排全部合规操作。"""

    def __init__(self, store=None, clock=None):
        self.store = store or Store()
        self._clock = clock or (lambda: date.today().isoformat())

    # ---- 供应批次与证据 ----

    def register_supply_batch(self, supplier_id, ingredient_id, received_at=None):
        batch = SupplyBatch(
            supply_batch_id=self.store.next_id("SUP"),
            supplier_id=supplier_id,
            ingredient_id=ingredient_id,
            received_at=received_at or self._clock(),
        )
        return self.store.put(self.store.supply_batches, batch.supply_batch_id, batch)

    def add_evidence(self, kind, subject_id, source, ranges, recorded_at=None):
        """录入带来源的实验室结果或生产损耗证据。"""
        kind = EvidenceKind(kind)
        cleaned = {name: self._checked_range(name, bounds) for name, bounds in ranges.items()}
        if kind is EvidenceKind.LAB_RESULT:
            if subject_id not in self.store.supply_batches:
                raise NotFoundError(f"供应批次 {subject_id} 不存在")
            if "whole_grain_fraction" not in cleaned:
                raise DomainError("实验室结果必须给出 whole_grain_fraction 区间")
            if not source.get("lab") or not source.get("method"):
                raise DomainError("实验室结果必须携带来源实验室与检测方法")
            if cleaned["whole_grain_fraction"][1] > 1.0:
                raise DomainError("全谷物含量分数不能超过 1")
        else:
            if subject_id not in self.store.recipes:
                raise NotFoundError(f"配方 {subject_id} 不存在")
            if "loss_rate" not in cleaned:
                raise DomainError("生产损耗证据必须给出 loss_rate 区间")
            if not source.get("line"):
                raise DomainError("生产损耗证据必须携带产线来源")
            if cleaned["loss_rate"][1] >= 1.0:
                raise DomainError("生产损耗率必须小于 1")
        evidence = Evidence(
            evidence_id=self.store.next_id("EV"),
            kind=kind,
            subject_id=subject_id,
            source=dict(source),
            ranges=cleaned,
            recorded_at=recorded_at or self._clock(),
        )
        return self.store.put(self.store.evidence, evidence.evidence_id, evidence)

    @staticmethod
    def _checked_range(name, bounds):
        try:
            lo, hi = float(bounds[0]), float(bounds[1])
        except (TypeError, IndexError, ValueError):
            raise DomainError(f"证据区间 {name} 必须是 [下限, 上限]")
        if not 0.0 <= lo <= hi:
            raise DomainError(f"证据区间 {name} 非法: [{lo}, {hi}]")
        return [lo, hi]

    # ---- 配方与组成 ----

    def create_recipe(self, product_id, product_category, region, lines, created_at=None):
        """创建配方版本；试产、改配方、换供应商分别自动触发审核。"""
        if not lines:
            raise DomainError("配方至少需要一行谷物原料")
        parsed = tuple(self._parse_line(item) for item in lines)
        total = sum(line.ratio for line in parsed)
        if abs(total - 1.0) > RATIO_SUM_TOLERANCE:
            raise DomainError(f"投料比例之和必须为 1，当前为 {total:.4f}")
        previous = self._versions_of(product_id)
        recipe = RecipeVersion(
            recipe_id=self.store.next_id("RCP"),
            product_id=product_id,
            product_category=product_category,
            region=region,
            version=len(previous) + 1,
            lines=parsed,
            created_at=created_at or self._clock(),
        )
        self.store.put(self.store.recipes, recipe.recipe_id, recipe)
        if previous:
            self._open_review(ReviewTrigger.RECIPE_CHANGE, recipe.recipe_id)
            if self._supplier_changed(previous[-1], recipe):
                self._open_review(ReviewTrigger.SUPPLIER_CHANGE, recipe.recipe_id)
        else:
            self._open_review(ReviewTrigger.TRIAL_PRODUCTION, recipe.recipe_id)
        return recipe

    def _parse_line(self, item):
        try:
            refining = Refining(item["refining"])
            ratio = float(item["ratio"])
            tolerance = float(item.get("ratio_tolerance", 0.0))
            supply_batch_id = item["supply_batch_id"]
            ingredient_id = item["ingredient_id"]
        except (KeyError, ValueError) as exc:
            raise DomainError(f"投料行非法: {exc}")
        if supply_batch_id not in self.store.supply_batches:
            raise NotFoundError(f"供应批次 {supply_batch_id} 不存在")
        if ratio <= 0.0:
            raise DomainError("投料比例必须为正")
        if not 0.0 <= tolerance < ratio:
            raise DomainError("投料允差必须非负且小于投料比例")
        return IngredientLine(
            ingredient_id=ingredient_id,
            name=item.get("name", ingredient_id),
            grain_type=item.get("grain_type", ""),
            refining=refining,
            ratio=ratio,
            ratio_tolerance=tolerance,
            supply_batch_id=supply_batch_id,
        )

    def _versions_of(self, product_id):
        versions = [r for r in self.store.recipes.values() if r.product_id == product_id]
        return sorted(versions, key=lambda r: r.version)

    def _supplier_changed(self, previous, current):
        def suppliers(recipe):
            return {
                line.ingredient_id: self.store.supply_batches[line.supply_batch_id].supplier_id
                for line in recipe.lines
            }

        old, new = suppliers(previous), suppliers(current)
        return any(ing in old and old[ing] != sup for ing, sup in new.items())

    def recipe_composition(self, recipe_id):
        """由配方明细与最新证据计算全谷物组成，企业无法直接填报。"""
        recipe = self._recipe(recipe_id)
        lab_index = {}
        loss_evidence = None
        for evidence in self.store.evidence.values():  # 后录入者覆盖索引
            if evidence.kind is EvidenceKind.LAB_RESULT:
                lab_index[evidence.subject_id] = evidence
            elif evidence.subject_id == recipe_id:
                loss_evidence = evidence
        return compute_composition(recipe, lab_index, loss_evidence, self._clock())

    # ---- 标准规则 ----

    def publish_rule(
        self,
        issuer_level,
        product_category,
        region,
        effective_from,
        min_whole_grain_ratio,
        definition_text,
        effective_to=None,
        published_at=None,
    ):
        if issuer_level not in ISSUER_PRECEDENCE:
            raise DomainError(f"未知发布层级: {issuer_level}")
        if not 0.0 < min_whole_grain_ratio <= 1.0:
            raise DomainError("全谷物比例下限必须在 (0, 1] 内")
        if effective_to is not None and effective_to <= effective_from:
            raise DomainError("失效日期必须晚于生效日期")
        rule = StandardRule(
            rule_id=self.store.next_id("STD"),
            issuer_level=issuer_level,
            product_category=product_category,
            region=region,
            effective_from=effective_from,
            effective_to=effective_to,
            min_whole_grain_ratio=float(min_whole_grain_ratio),
            definition_text=definition_text,
            published_at=published_at or self._clock(),
        )
        return self.store.put(self.store.rules, rule.rule_id, rule)

    def applicable_standard(self, product_category, region, on_date=None):
        """回答某批产品上市时究竟满足哪一条标准。"""
        return match_standard(
            list(self.store.rules.values()),
            product_category,
            region,
            on_date or self._clock(),
        )

    # ---- 审核 ----

    def trigger_review(self, trigger, recipe_id):
        """手动触发审核（如量产）；试产、改配方、换供应商由配方流程自动触发。"""
        self._recipe(recipe_id)
        return self._open_review(ReviewTrigger(trigger), recipe_id)

    def _open_review(self, trigger, recipe_id):
        for review in self.store.reviews.values():
            if (
                review.recipe_id == recipe_id
                and review.trigger == trigger
                and review.status == "pending"
            ):
                raise DomainError("同类审核仍在进行中")
        review = Review(
            review_id=self.store.next_id("REV"),
            trigger=trigger,
            recipe_id=recipe_id,
            status="pending",
            created_at=self._clock(),
        )
        return self.store.put(self.store.reviews, review.review_id, review)

    def decide_review(self, review_id, approver, approved, note=""):
        review = self.store.reviews.get(review_id)
        if review is None:
            raise NotFoundError(f"审核 {review_id} 不存在")
        if review.status != "pending":
            raise DomainError("审核已定论，不可重复决定")
        decided = replace(
            review,
            status="approved" if approved else "rejected",
            approver=approver,
            decided_at=self._clock(),
            note=note,
        )
        self.store.reviews[review_id] = decided
        return decided

    def recipe_status(self, recipe_id):
        self._recipe(recipe_id)
        reviews = [r for r in self.store.reviews.values() if r.recipe_id == recipe_id]
        if any(r.status == "pending" for r in reviews):
            return STATUS_IN_REVIEW
        if not reviews or any(r.status == "rejected" for r in reviews):
            return STATUS_DRAFT
        return STATUS_LABEL_OK

    def recipe_view(self, recipe_id):
        recipe = self._recipe(recipe_id)
        reviews = [
            asdict(r) for r in self.store.reviews.values() if r.recipe_id == recipe_id
        ]
        return {
            "recipe": asdict(recipe),
            "status": self.recipe_status(recipe_id),
            "reviews": reviews,
        }

    # ---- 包装与过渡期 ----

    def register_packaging(self, recipe_id, design_version, quantity, produced_at=None, is_old_stock=False):
        """登记包装批次：配方须已通过审核，且计算比例达到当时适用标准。"""
        recipe = self._recipe(recipe_id)
        if self.recipe_status(recipe_id) != STATUS_LABEL_OK:
            raise DomainError("配方尚未通过审核，不允许登记包装批次")
        produced_at = produced_at or self._clock()
        rule = match_standard(
            list(self.store.rules.values()),
            recipe.product_category,
            recipe.region,
            produced_at,
        )
        composition = self.recipe_composition(recipe_id)
        if composition.ratio_range[0] < rule.min_whole_grain_ratio:
            raise DomainError("全谷物比例区间下限低于适用标准，禁止上市")
        batch = PackagingBatch(
            batch_code=self.store.next_id("PKG"),
            recipe_id=recipe_id,
            design_version=design_version,
            produced_at=produced_at,
            quantity=int(quantity),
            standard_rule_id=rule.rule_id,
            is_old_stock=bool(is_old_stock),
        )
        return self.store.put(self.store.packaging, batch.batch_code, batch)

    def approve_transition(self, recipe_id, design_version, valid_from, valid_until, approved_by, reason=""):
        """批准旧包装在限定过渡期内继续流通。"""
        self._recipe(recipe_id)
        if valid_from > valid_until:
            raise DomainError("过渡期起止时间非法")
        window = TransitionWindow(
            window_id=self.store.next_id("TRN"),
            recipe_id=recipe_id,
            design_version=design_version,
            approved_by=approved_by,
            valid_from=valid_from,
            valid_until=valid_until,
            reason=reason,
        )
        return self.store.put(self.store.transitions, window.window_id, window)

    def _transition_open(self, batch, today):
        return any(
            window.recipe_id == batch.recipe_id
            and window.design_version == batch.design_version
            and window.valid_from <= today <= window.valid_until
            for window in self.store.transitions.values()
        )

    # ---- 抽检、申辩与召回（仅追加，不覆盖原报告） ----

    def file_sampling_report(self, batch_code, agency, method, measured_ratio, created_at=None):
        batch = self._packaging(batch_code)
        measured = float(measured_ratio)
        if not 0.0 <= measured <= 1.0:
            raise DomainError("实测比例必须在 [0, 1] 内")
        lo, hi = self.recipe_composition(batch.recipe_id).ratio_range
        if measured < lo:
            deviation = measured - lo
        elif measured > hi:
            deviation = measured - hi
        else:
            deviation = 0.0
        report = SamplingReport(
            report_id=self.store.next_id("SMP"),
            batch_code=batch_code,
            agency=agency,
            method=method,
            measured_ratio=measured,
            deviation=round(deviation, 6),
            created_at=created_at or self._clock(),
        )
        return self.store.put(self.store.sampling_reports, report.report_id, report)

    def file_appeal(self, report_id, company, argument, created_at=None):
        self._report(report_id)
        appeal = Appeal(
            appeal_id=self.store.next_id("APL"),
            report_id=report_id,
            company=company,
            argument=argument,
            created_at=created_at or self._clock(),
        )
        return self.store.put(self.store.appeals, appeal.appeal_id, appeal)

    def file_recall(self, report_id, decided_by, scope, created_at=None):
        report = self._report(report_id)
        recall = RecallDecision(
            recall_id=self.store.next_id("RCL"),
            report_id=report_id,
            batch_code=report.batch_code,
            decided_by=decided_by,
            scope=scope,
            created_at=created_at or self._clock(),
        )
        return self.store.put(self.store.recalls, recall.recall_id, recall)

    def report_view(self, report_id):
        """原报告连同关联的申辩与召回一并返回，原报告字段保持不变。"""
        report = self._report(report_id)
        return {
            "report": asdict(report),
            "appeals": [
                asdict(a) for a in self.store.appeals.values() if a.report_id == report_id
            ],
            "recalls": [
                asdict(r) for r in self.store.recalls.values() if r.report_id == report_id
            ],
        }

    # ---- 状态、扫码与追溯 ----

    def packaging_status(self, batch_code, today=None):
        batch = self._packaging(batch_code)
        today = today or self._clock()
        if any(r.batch_code == batch_code for r in self.store.recalls.values()):
            return STATUS_RECALL
        if any(
            report.deviation != 0.0
            for report in self.store.sampling_reports.values()
            if report.batch_code == batch_code
        ):
            return STATUS_RECHECK
        if batch.is_old_stock:
            return STATUS_TRANSITION if self._transition_open(batch, today) else STATUS_BLOCKED
        return STATUS_LABEL_OK

    def scan(self, batch_code):
        """消费者扫码视图：适用定义、真实比例区间与认证状态。"""
        batch = self._packaging(batch_code)
        recipe = self._recipe(batch.recipe_id)
        rule = self.store.rules[batch.standard_rule_id]
        composition = self.recipe_composition(recipe.recipe_id)
        return {
            "batch_code": batch.batch_code,
            "product_id": recipe.product_id,
            "product_category": recipe.product_category,
            "region": recipe.region,
            "applicable_definition": {
                "rule_id": rule.rule_id,
                "issuer_level": rule.issuer_level,
                "definition_text": rule.definition_text,
                "min_whole_grain_ratio": rule.min_whole_grain_ratio,
                "effective_from": rule.effective_from,
                "effective_to": rule.effective_to,
            },
            "whole_grain_ratio_range": composition.ratio_range,
            "meets_standard": composition.ratio_range[0] >= rule.min_whole_grain_ratio,
            "certification_status": self.packaging_status(batch_code),
        }

    def register_claim(self, text, recipe_id, registered_by, created_at=None):
        self._recipe(recipe_id)
        if not text or not text.strip():
            raise DomainError("宣传语不能为空")
        claim = MarketingClaim(
            claim_id=self.store.next_id("CLM"),
            text=text.strip(),
            recipe_id=recipe_id,
            registered_by=registered_by,
            created_at=created_at or self._clock(),
        )
        return self.store.put(self.store.claims, claim.claim_id, claim)

    def trace_claim(self, claim_id):
        """监管追溯：从一句宣传追到配方版本、检测方法、批准人与流通包装。"""
        claim = self.store.claims.get(claim_id)
        if claim is None:
            raise NotFoundError(f"宣传语 {claim_id} 不存在")
        recipe = self._recipe(claim.recipe_id)
        composition = self.recipe_composition(recipe.recipe_id)
        evidence_used = [self.store.evidence[eid] for eid in composition.evidence_ids]
        approvals = [
            {
                "review_id": r.review_id,
                "trigger": r.trigger,
                "status": r.status,
                "approver": r.approver,
                "decided_at": r.decided_at,
            }
            for r in self.store.reviews.values()
            if r.recipe_id == recipe.recipe_id
        ]
        circulating = []
        for batch in self.store.packaging.values():
            if batch.recipe_id != recipe.recipe_id:
                continue
            status = self.packaging_status(batch.batch_code)
            if status in CIRCULATING_STATUSES:
                circulating.append(
                    {
                        "batch_code": batch.batch_code,
                        "design_version": batch.design_version,
                        "produced_at": batch.produced_at,
                        "quantity": batch.quantity,
                        "status": status,
                    }
                )
        return {
            "claim": asdict(claim),
            "recipe": {
                "recipe_id": recipe.recipe_id,
                "product_id": recipe.product_id,
                "product_category": recipe.product_category,
                "region": recipe.region,
                "version": recipe.version,
                "lines": [asdict(line) for line in recipe.lines],
            },
            "composition": {
                "ratio_range": composition.ratio_range,
                "evidence": [asdict(e) for e in evidence_used],
            },
            "approvals": approvals,
            "circulating_packaging": circulating,
        }

    # ---- 内部查询 ----

    def _recipe(self, recipe_id):
        recipe = self.store.recipes.get(recipe_id)
        if recipe is None:
            raise NotFoundError(f"配方 {recipe_id} 不存在")
        return recipe

    def _packaging(self, batch_code):
        batch = self.store.packaging.get(batch_code)
        if batch is None:
            raise NotFoundError(f"包装批次 {batch_code} 不存在")
        return batch

    def _report(self, report_id):
        report = self.store.sampling_reports.get(report_id)
        if report is None:
            raise NotFoundError(f"抽检报告 {report_id} 不存在")
        return report
