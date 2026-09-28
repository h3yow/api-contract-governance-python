# API 契约注册与版本兼容治理

为 API 服务提供契约的注册、查询、更新、撤销，运行时请求/响应校验，
以及相邻版本的兼容性判定。所有判定结论可复现、可解释，错误原因可区分。

## 契约格式

契约由「标识 + 版本 + 字段集合」构成，字段定义如下：

| 属性       | 说明                                                         |
| ---------- | ------------------------------------------------------------ |
| `name`     | 字段名，同一契约内唯一                                       |
| `type`     | `string` / `integer` / `number` / `boolean` / `object` / `array` |
| `required` | 是否必填，默认 `false`                                       |

注册时初始版本为 1。每个版本的定义一旦写入即不可变并永久保留，
保证兼容性判定与审计可复现。

### 单侧契约与成对契约

契约支持两种形状写法，二者不能混用：

- **单侧老契约**：只声明 `fields`，继续使用原有 `validate(...)` 单侧入口，
  行为与历史完全一致。
- **成对新契约**：声明 `request_fields` / `response_fields`（可只声明一侧），
  使用 `validate_pair(...)` 一次同时提交两侧内容。对成对契约调单侧入口、
  或对单侧契约调成对入口，报 `validation_side_mismatch`；
  漏交已声明侧、或提交未声明侧，报 `invalid_contract`。

```python
spec = ContractSpec(
    request_fields=(FieldSpec("id", FieldType.INTEGER, required=True),),
    response_fields=(
        FieldSpec("code", FieldType.INTEGER, required=True),
        FieldSpec("ok", FieldType.BOOLEAN),
    ),
)
service.register("echo-api", spec)

# request/response 缺省为 MISSING（未提交该侧）；显式传 None 表示提交了非对象载荷
result = service.validate_pair(
    "echo-api", 1,
    request={"id": 1},
    response={"code": 0, "ok": True},
)
result.ok                       # 任一侧不过则整体 False
{s.side: s.issues for s in result.sides}   # 问题按 Side.REQUEST / Side.RESPONSE 归属
```

成对结果为 `PairedValidationResult`：每侧一个 `SideValidationResult`，
两侧问题**互不覆盖、各自保留**，`ok` 为各侧结论的合取。只声明一侧的契约
只返回该侧结果。

## 版本流转语义

- **注册**：`register(contract_id, spec)` → 版本 1。同一标识重复注册被拒绝：
  活跃契约报 `contract_exists`，已撤销契约报 `contract_revoked`。
- **更新（无门禁）**：`update(contract_id, version, spec)` → 仅允许
  `version == 当前版本 + 1` 的线性流转，跳号、回退、重复均报
  `illegal_version_transition`。语义保持不变，不走破坏性门禁。
- **发布（带门禁，推荐）**：`publish(contract_id, version, spec, accept_breaking=False)`
  在落库前先做相邻版本兼容性判定，详见下节。
- **撤销**：`revoke(contract_id)` → 状态置为 `revoked`，此后不得更新/发布、
  不得重新注册、不得参与校验（均报 `contract_revoked`）。
- **校验**：仅接受当前注册版本；版本不一致报 `version_mismatch`，
  未注册报 `contract_not_found`。

所有失败发生在落库之前，不残留部分状态，不污染已有注册数据。

## 版本发布门禁（破坏性变更）

`publish` 在上一版本 → 新版本落库前强制过一道门禁：

| 情形                                   | 默认行为                          | 原因码（与非法流转可区分）          |
| -------------------------------------- | --------------------------------- | ----------------------------------- |
| 兼容性判定为 `breaking`                | **拒绝，不落库、不写门禁记录**    | `breaking_change_rejected`          |
| 判定为 `breaking` 且 `accept_breaking=True` | 放行落库，记录判定依据与确认结果 | （成功）                            |
| 判定为 `compatible`                    | 直接放行（确认标记不改变记录）    | （成功）                            |
| 跳号 / 回退 / 重复（含并发落败方）     | 拒绝                              | `illegal_version_transition`        |
| 已撤销 / 未注册                        | 拒绝                              | `contract_revoked` / `contract_not_found` |

显式接受的范围仅限「这一次、这一对相邻版本」的破坏性变更；门禁记录
（结论、逐条变更依据、`breaking_accepted`）随版本永久保存，可通过
`get_publish_decision(contract_id, version)` 重新查到；尚未落库的版本
查询报 `decision_not_found`。同一目标版本被并发提交时，无论是否确认，
都只有一个成功（线性流转校验 + 版本落库 + 记录落库在同一把锁内原子完成），
落败方统一报 `illegal_version_transition`，不发生覆盖丢失。

## 兼容性规则（相邻版本）

| 变更                             | 分类                  | 结论   |
| -------------------------------- | --------------------- | ------ |
| 字段删除                         | `field_removed`       | 破坏性 |
| 类型收窄（除 integer→number 外） | `type_narrowed`       | 破坏性 |
| 必填范围扩大（可选→必填 / 新增必填） | `required_expanded` | 破坏性 |
| 类型放宽（integer→number）       | `type_widened`        | 兼容   |
| 必填放宽（必填→可选）            | `required_relaxed`    | 兼容   |
| 新增可选字段                     | `field_added_optional`| 兼容   |

任一破坏性变更即判定 `breaking`，否则 `compatible`。判定为纯函数，
变更列表按字段名排序，同一对版本的结论必然一致。成对契约对请求侧、
响应侧**逐侧比较**，每条变更带 `side`（`request` / `response`）归属，
排序按（侧、字段名）；单侧老契约的变更 `side` 为 `None`，输出与历史一致。
单侧形状与成对形状之间不可比较，报 `invalid_contract`。

## 校验时间预算

单侧 `validate` 与成对 `validate_pair` 都受时间预算约束：

- 预算由 `GovernanceLimits.validation_timeout`（秒，默认 1.0s）配置；
  设为 `None` 表示不设预算。也可在单次调用用 `timeout=` 参数覆盖：
  负数（默认）沿用配置，`None` 显式禁用本次预算，正数为本次上限。
- 超预算时**整单拒绝**，抛 `BUDGET_EXCEEDED`（HTTP 504），
  不返回「单侧已完成」之类的半截结果；对外消息只有通用文案
  `validation rejected: time budget exceeded`，不暴露内部细节。
- 每次校验取不可变快照、在独立守护线程内执行，字段循环中周期性
  协作检查截止时间。个别调用超时不拖住调用方、不影响其他并发校验，
  也不改变任何已注册契约的状态（失败不留状态）。
- 预算之内完成时，校验结果与不设预算完全一致。

## 并发语义

- 写操作在单把锁内完成「校验 + 落库」，写与写原子，无半更新、无覆盖丢失；
  并发更新同一版本时恰有一个成功，其余被归类为 `illegal_version_transition`。
  带门禁的 `publish` 同样在锁内完成「线性流转校验 + 版本落库 + 门禁记录落库」，
  同一目标版本并发发布（无论是否 `accept_breaking`）都恰有一个成功。
- 读操作返回不可变快照，进行中的校验不受并发写入影响，
  读取方不会观察到未完成的写入或过期快照。
- 校验在独立线程内执行并受时间预算保护，单个超时调用不阻塞其他请求。

## 可配置上限（`GovernanceLimits`）

| 配置项                     | 默认  | 超限行为                                   |
| -------------------------- | ----- | ------------------------------------------ |
| `max_contracts`            | 1024  | 拒绝注册，报 `limit_exceeded`              |
| `max_fields_per_contract`  | 256   | 拒绝注册/更新/发布，报 `limit_exceeded`（单侧与每侧分别计数） |
| `max_validation_issues`    | 32    | 单侧校验错误列表截断并附 `issues_truncated` 标记（两侧各自截断） |
| `expose_error_details`     | True  | 关闭时错误消息替换为通用文案，不泄漏字段细节 |
| `validation_timeout`       | 1.0s  | 单次校验耗时上限；超预算整单报 `budget_exceeded`，`None` 为不设预算 |

## HTTP 接口

| 方法   | 路径                                     | 说明                       |
| ------ | ---------------------------------------- | -------------------------- |
| POST   | `/contracts`                             | 注册契约（支持 `fields` / `request_fields` / `response_fields`） |
| GET    | `/contracts/{id}`                        | 查询当前版本               |
| PUT    | `/contracts/{id}`                        | 更新到下一版本（无门禁）   |
| POST   | `/contracts/{id}/publish`                | 带破坏性门禁发布，body 可带 `accept_breaking` |
| GET    | `/contracts/{id}/decisions/{version}`    | 查询某次发布的门禁判定与确认结果 |
| DELETE | `/contracts/{id}`                        | 撤销契约                   |
| POST   | `/contracts/{id}/validate`               | 运行时单侧校验 payload     |
| POST   | `/contracts/{id}/validate-pair`          | 成对校验 `request` / `response`，结果按侧归属 |
| GET    | `/contracts/{id}/compatibility?from_version=&to_version=` | 兼容性判定 |

错误统一返回 `{"error": {"reason": <原因码>, "message": <安全消息>}}`，
原因码与 HTTP 状态码稳定映射：`contract_not_found` / `decision_not_found` → 404；
`contract_exists` / `contract_revoked` / `illegal_version_transition` /
`breaking_change_rejected` → 409；`invalid_contract` /
`validation_side_mismatch` → 422；`limit_exceeded` → 413；
`budget_exceeded` → 504。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（含下列新行为用例）
uv run pytest -q -o log_cli=true   # 查看契约标识、判定依据与归一化结果日志
uv run uvicorn main:app --reload   # 启动 HTTP 服务
```

新行为对应的测试文件，可单独复现：

```bash
# 请求/响应两侧归属与合并结论、老契约不回归
uv run pytest -q tests/test_paired_validation.py tests/test_paired_compat.py
# 超预算整单拒绝、失败不留状态、并发不互相拖累（含真实墙钟硬超时）
uv run pytest -q tests/test_budget.py
# 破坏性变更默认拒绝 / 显式接受两条路径、判定依据可复查
uv run pytest -q tests/test_publish_gate.py
# 同一目标版本并发提交（确认/未确认/兼容三种情形）恰一个成功
uv run pytest -q tests/test_publish_concurrency.py
# 新 HTTP 端点与原因码映射（504 budget_exceeded 等）
uv run pytest -q tests/test_api_new.py
# 回归：原注册/流转/撤销、校验、兼容性、并发行为
uv run pytest -q tests/test_registry.py tests/test_validation.py \
  tests/test_compat.py tests/test_limits.py tests/test_concurrency.py tests/test_api.py
```
