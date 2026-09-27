# 全谷物标签合规库

依据配方、供应批次、检测证据和标准版本核验全谷物标签。项目以领域契约约定参与者、状态和不可破坏的业务原则，各模块围绕同一语义协作。

## 模块

- `domain.py` — 领域模型与组成计算：谷物原料、精制方式（全谷物/精制/回填）、投料比例与允差、供应批次、带来源的实验室结果与生产损耗证据。全谷物比例由明细与证据计算出区间，企业直接填报最终百分比的请求会被拒绝。
- `standards.py` — 标准规则按产品类别、地区与生效时间发布；地区专属优先于全国，国家标准优先于行业标准与团体规则，回答"某批产品上市时满足哪一条"。
- `store.py` — 追加式内存存储：同键写入即拒绝，抽检报告与证据不可覆盖。
- `compliance.py` — 核心服务：试产、量产、改配方、换供应商分别触发审核；包装批次登记时锁定当时适用标准并校验比例下限；旧包装仅在获准过渡期内流通；抽检差异、企业申辩、召回决定仅作关联追加；消费者扫码视图与监管追溯视图。
- `service.py` — HTTP 接口与服务入口。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/contract` | 运行状态与领域契约 |
| POST | `/supply-batches` | 登记原料供应批次 |
| POST | `/evidence` | 录入实验室结果或生产损耗（须携带来源） |
| POST | `/recipes` | 创建配方版本（自动触发试产/改配方/换供应商审核） |
| GET | `/recipes/{id}` `/recipes/{id}/composition` | 配方状态、审核记录与计算组成 |
| POST | `/standards` | 发布标准规则；GET `/standards/match?category=&region=&date=` 匹配适用标准 |
| POST | `/reviews` `/reviews/{id}/decision` | 触发审核（如量产）与审核决定 |
| POST | `/packaging-batches` `/transitions` | 登记包装批次、批准旧包装过渡期 |
| POST | `/sampling-reports` `/sampling-reports/{id}/appeals` `/sampling-reports/{id}/recalls` | 抽检报告、企业申辩、召回决定（均只追加） |
| GET | `/sampling-reports/{id}` | 原报告及关联申辩、召回 |
| POST | `/claims` | 登记营养或节粮宣传语 |
| GET | `/scan/{batch_code}` | 消费者扫码：适用定义、真实比例区间、认证状态 |
| GET | `/trace/claims/{claim_id}` | 监管追溯：配方版本、检测方法、批准人、仍在流通的包装 |

## 运行

`python3 service.py --check` 核对服务配置；`python3 service.py --port 8000` 启动服务。使用 `python3 -m unittest -v` 运行全部测试。
