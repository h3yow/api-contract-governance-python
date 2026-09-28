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

### 成对契约：分别声明请求侧与响应侧形状

`ContractSpec` 除请求侧 `fields` 外，还可以声明 `response_fields`：

- 只声明 `fields`（`response_fields` 留空为 `None`）：**老的单侧契约**，
  继续用 `validate(...)` 按原有方式校验，历史行为不变。
- 声明 `response_fields`（包括传空元组 `()`，表示成对但响应侧无字段约束）：
  **成对契约**，用 `validate_pair(...)` 一次提交两侧内容。

```python
spec = ContractSpec(
    fields=(FieldSpec("id", FieldType.INTEGER, required=True),),          # 请求侧
    response_fields=(FieldSpec("ok", FieldType.BOOLEAN, required=True),),  # 响应侧
)
svc.register("user-api", spec)
result = svc.validate_pair("user-api", 1, {"id": 1}, {"ok": True})
result.ok                 # 任一侧不过则整体 False
result.request.issues     # 请求侧问题，只归属请求侧
result.response.issues    # 响应侧问题，只归属响应侧
result.side(Side.REQUEST) # 按侧别取单侧结论
```

成对校验的问题按侧分别归集、互不覆盖；任何一侧不通过，整体即不通过，
但两侧结论都会完整返回。对老契约调用 `validate_pair` 会报 `invalid_contract`。

## 版本流转语义

- **注册**：`register(contract_id, spec)` → 版本 1。同一标识重复注册被拒绝：
  活跃契约报 `contract_exists`，已撤销契约报 `contract_revoked`。
- **更新**：`update(contract_id, version, spec)` → 仅允许 `version == 当前版本 + 1`
  的线性流转，跳号、回退、重复均报 `illegal_version_transition`。
- **撤销**：`revoke(contract_id)` → 状态置为 `revoked`，此后不得更新、
  不得重新注册、不得参与校验（均报 `contract_revoked`）。
- **校验**：仅接受当前注册版本；版本不一致报 `version_mismatch`，
  未注册报 `contract_not_found`。

所有失败发生在落库之前，不残留部分状态，不污染已有注册数据。

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
变更列表按字段名排序，同一对版本的结论必然一致。

## 发布门禁（`publish`）

直接 `update` 只校验线性版本流转；`publish` 在落库前额外强制一道
破坏性变更门禁：

- 先取「当前版本 → 目标版本」的相邻版本判定；目标版本必须恰为当前版本 + 1，
  跳号、回退、重复一律报 `illegal_version_transition`，与门禁拒绝原因区分。
- 判定为 `breaking` 时**默认拒绝落库**，报 `breaking_change_rejected`，
  不留版本、不留档案。
- 调用方显式传 `accept_breaking=True` 才允许破坏性版本落库。
  兼容变更不受该标记影响（不会被记成“接受过破坏性变更”）。
- 落库成功后，这对版本的判定结论、变更依据与确认结果可通过
  `get_release_record(contract_id, version)` 重新查到；查不到报
  `release_record_not_found`。
- 同一目标版本并发提交（无论是否带确认）都只有一个成功，其余收到
  `illegal_version_transition`，不会覆盖丢失。

```python
svc.publish("user-api", 2, spec_v2)                          # 破坏性 -> 拒绝
svc.publish("user-api", 2, spec_v2, accept_breaking=True)    # 显式接受 -> 落库
record = svc.get_release_record("user-api", 2)              # 复查判定依据与确认结果
record.verdict, record.changes, record.accepted_breaking
```

显式接受的作用范围仅限**本次发布的这一对相邻版本**；它不是全局豁免，
后续版本仍需各自过门。

## 校验时间预算

单侧与成对校验都受可配置的耗时上限约束：

- 全局配置：`GovernanceLimits.validation_timeout_seconds`（默认 2.0 秒，
  `<=0` 表示不设预算）；单次调用可用 `timeout_seconds=` 覆盖（含 `0/-1`
  表示本次不限）。成对校验的两侧共用同一个预算。
- 超限时**整体拒绝**，抛 `validation_timeout`（HTTP 408），不返回半截结果，
  消息不含字段名或堆栈等内部细节；预算之内的校验结果与无预算时完全一致。
- 每次校验在独立守护线程内执行：个别调用超时只影响自身等待，不会拖住其他
  并发请求；校验是纯读取操作，超时也不会改动或污染已注册契约的状态。

## 并发语义

- 写操作在单把锁内完成「校验 + 落库」，写与写原子，无半更新、无覆盖丢失；
  并发更新同一版本时恰有一个成功，其余被归类为 `illegal_version_transition`。
- 读操作返回不可变快照，进行中的校验不受并发写入影响，
  读取方不会观察到未完成的写入或过期快照。

## 可配置上限（`GovernanceLimits`）

| 配置项                     | 默认  | 超限行为                                   |
| -------------------------- | ----- | ------------------------------------------ |
| `max_contracts`            | 1024  | 拒绝注册，报 `limit_exceeded`              |
| `max_fields_per_contract`  | 256   | 拒绝注册/更新，报 `limit_exceeded`         |
| `max_validation_issues`    | 32    | 校验错误列表截断并附 `issues_truncated` 标记 |
| `validation_timeout_seconds` | 2.0 | 单次校验耗时上限，超限整体报 `validation_timeout`；`<=0` 不设限 |
| `expose_error_details`     | True  | 关闭时错误消息替换为通用文案，不泄漏字段细节 |

## HTTP 接口

| 方法   | 路径                                     | 说明               |
| ------ | ---------------------------------------- | ------------------ |
| POST   | `/contracts`                             | 注册契约（可带 `response_fields` 声明成对形状） |
| GET    | `/contracts/{id}`                        | 查询当前版本       |
| PUT    | `/contracts/{id}`                        | 更新到下一版本（不经门禁，保留原语义） |
| POST   | `/contracts/{id}/releases`               | 门禁发布：破坏性变更需 `accept_breaking` |
| GET    | `/contracts/{id}/releases/{version}`     | 查询发布门禁判定依据与确认结果 |
| DELETE | `/contracts/{id}`                        | 撤销契约           |
| POST   | `/contracts/{id}/validate`               | 运行时单侧校验（可带 `timeout_seconds`） |
| POST   | `/contracts/{id}/validate-pair`          | 成对校验请求/响应两侧（可带 `timeout_seconds`） |
| GET    | `/contracts/{id}/compatibility?from_version=&to_version=` | 兼容性判定 |

错误统一返回 `{"error": {"reason": <原因码>, "message": <安全消息>}}`，
原因码与 HTTP 状态码稳定映射（404/409/413/422）。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（含成对校验、超时、发布门禁与并发）
uv run pytest -q -o log_cli=true   # 查看契约标识、判定依据与归一化结果日志
uv run uvicorn main:app --reload   # 启动 HTTP 服务
```

### 手工复现新行为

```bash
# 成对契约注册 + 成对校验（请求侧出错、响应侧通过，结论按侧归属）
curl -s -XPOST localhost:8000/contracts -H 'content-type: application/json' -d '{
  "contract_id": "pair-api",
  "fields": [{"name":"id","type":"integer","required":true}],
  "response_fields": [{"name":"ok","type":"boolean","required":true}]}'
curl -s -XPOST localhost:8000/contracts/pair-api/validate-pair \
  -H 'content-type: application/json' \
  -d '{"version":1,"request_payload":{"id":"x"},"response_payload":{"ok":true}}'

# 校验超预算：timeout_seconds 给一个极小值 -> 408 validation_timeout
curl -s -XPOST localhost:8000/contracts/pair-api/validate-pair \
  -H 'content-type: application/json' \
  -d '{"version":1,"request_payload":{},"response_payload":{},"timeout_seconds":0.0001}'

# 发布门禁：先注册 v1，再尝试删除字段的破坏性 v2
curl -s -XPOST localhost:8000/contracts -H 'content-type: application/json' \
  -d '{"contract_id":"pub","fields":[{"name":"id","type":"integer","required":true}]}'
curl -s -XPOST localhost:8000/contracts/pub/releases -H 'content-type: application/json' \
  -d '{"version":2,"fields":[]}'                       # 409 breaking_change_rejected
curl -s -XPOST localhost:8000/contracts/pub/releases -H 'content-type: application/json' \
  -d '{"version":2,"fields":[],"accept_breaking":true}'  # 201 落库并留档
curl -s localhost:8000/contracts/pub/releases/2         # 复查判定依据与确认结果
```
