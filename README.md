# 全谷物标签合规库

依据配方、供应批次、检测证据和标准版本核验全谷物标签。项目以领域契约约定参与者、状态和不可破坏的业务原则，基础服务提供稳定的运行检查与契约读取接口，便于各模块围绕同一语义协作。

## 架构

```
service.py            HTTP 接口层（路由、JSON、错误映射），保留 /health 与 /contract
wholegrain/
  models.py           冻结数据类实体与领域常量（状态、触发类型、精制方式）
  store.py            仅追加存储：记录不可改不可删，可选 JSON 文件持久化
  domain.py           应用服务：组成计算、规则匹配、审核流、过渡期、扫码与追溯
  errors.py           领域异常 → HTTP 状态码（400/404/409）
domain_contract.json  领域契约（参与者、状态、不变式）
test_service.py       基础契约测试
test_wholegrain.py    领域与端到端接口测试
```

## 核心业务规则

- **比例只能算出来，不能填出来**：配方由明细行构成（谷物原料 + 精制方式 + 投料比例 + 供应批次），服务端计算 `Σ(投料比例 × 供应批次全谷物含量)`；`/recipes` 接口拒绝 `final_ratio` 等直接填报字段。精制方式与供应批次含量做一致性校验（全粒 ≥0.9，部分精制 0.05~0.9，精制 ≤0.05）。
- **证据带来源**：实验室检测结果（含不确定度、检测方法）与生产损耗率必须注明来源、方法和记录人。真实比例区间 = `[min(计算值×(1−最大损耗), 各检测值−不确定度), max(计算值, 各检测值+不确定度)]`。
- **标准并行匹配**：规则按产品类别、地区、生效时间发布与匹配（国标/行标/团标并行）；主导规则按级别优先展示，宣称判定须满足全部适用标准中该宣称的**最严阈值**，且以区间**下界**判定。
- **四类审核触发**：试产、量产由企业发起；改配方、换供应商在新建版本时由系统自动触发，且须全部通过后才能申请量产。审核决定是独立追加记录，一经作出不得更改。
- **旧包装过渡期**：非现行包装只能凭获准过渡批准投产，校验截止日期与累计数量。
- **不覆盖原报告**：抽检报告（含当时的计算快照）、企业申辩、召回决定全部是仅追加记录；批次状态（正常流通/过渡销售/抽检复核/召回中）由记录流推导。

## 运行

```bash
python3 service.py --check                 # 核对服务配置
python3 service.py --port 8000             # 启动（内存存储）
python3 service.py --port 8000 --data-file data.json   # 启动并持久化
python3 -m unittest -v                     # 运行全部测试
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/contract` | 健康检查、领域契约 |
| POST/GET | `/rules`，GET `/rules/match?category=&region=&date=` | 发布标准、查询适用与主导规则 |
| POST/GET | `/ingredients` `/suppliers` `/supply-batches` `/products` | 基础数据登记 |
| POST | `/recipes` | 新建配方版本（自动触发改配方/换供应商审核） |
| GET | `/recipes/{id}` `/recipes/{id}/composition` `/recipes/{id}/reviews` | 配方详情、组成计算、审核记录 |
| POST | `/recipes/{id}/evidence` | 登记检测/损耗证据（带来源） |
| POST | `/recipes/{id}/reviews`，POST `/reviews/{id}/decision` | 发起试产/量产审核、作出决定 |
| POST | `/packaging`，GET `/packaging/{id}/validate?market=&date=` | 包装版本与宣称预校验 |
| POST | `/transition-approvals` | 批准旧包装过渡期 |
| POST/GET | `/batches` `/batches/{code}` | 创建生产批次（合规闸门）、批次详情 |
| POST/GET | `/inspections` `/inspections/{id}` | 登记抽检报告（快照计算值） |
| POST | `/inspections/{id}/appeals`，POST `/appeals/{id}/decision` | 企业申辩与结论 |
| POST | `/recalls` | 召回决定 |
| GET | `/scan/{code}` | 消费者扫码：适用定义、真实比例区间、认证状态 |
| GET | `/trace/claims/{宣传语}` | 监管追溯：配方版本、检测方法、批准人、仍在流通的包装范围 |

典型流程：登记基础数据 → 发布标准 → 新建配方 → 试产/量产审核通过 → 创建包装 → 投产批次 → 消费者扫码；抽检、申辩、召回随时以追加记录介入。
