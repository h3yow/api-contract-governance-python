# API 契约注册与版本兼容治理

为 API 服务提供契约的**注册 / 查询 / 更新 / 撤销**、基于契约的**运行时校验**、
相邻版本间的**兼容性判定**，并在并发访问下保证状态原子、结论可复现、
失败原因可区分。

## 1. 运行环境

- Python >= 3.14
- 依赖管理：[uv](https://docs.astral.sh/uv/)
- Web 框架：FastAPI（仅 HTTP 入口；治理核心不依赖框架）

## 2. 契约格式

契约以 JSON 提交，结构如下：

```json
{
  "id": "user-service",
  "version": "1.0.0",
  "fields": [
    {"name": "user_id", "type": "INTEGER", "required": true},
    {"name": "nick",    "type": "STRING",  "required": false}
  ]
}
```

| 部分    | 规则                                                                 |
| ------- | -------------------------------------------------------------------- |
| `id`    | 字母开头，仅含字母/数字/`_`/`-`，长度 1–64                           |
| `version` | 语义版本 `主.次.补丁`，每段为非负整数、无前导零（如 `1.10.2`）     |
| `fields` | 字段数组；字段名为字母开头的 `[A-Za-z0-9_]{1,64}`，同一契约内唯一  |
| `type` | `STRING` / `INTEGER` / `NUMBER` / `BOOLEAN` / `OBJECT` / `ARRAY`    |
| `required` | 布尔，默认 `false`                                                |

**归一化**：服务端按字段名排序后生成 `normalized` 表示并随契约返回。
同一输入的归一化结果完全一致，是版本比较与结论复现的基础。

同一份字段集合同时应用于运行时校验的 `request` 与 `response` 两个区段
（即两区段都必须满足该消息契约）；缺失必填字段、类型不符、出现未声明字段
都会产生一条可区分的违规记录。

## 3. HTTP 接口与版本流转语义

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /contracts` | 注册新版本 |
| `GET  /contracts/{id}/versions/{version}` | 查询单个版本（已撤销返回 `410`） |
| `PUT  /contracts/{id}/versions/{version}` | 更新字段集合（需带 `expected_revision`） |
| `POST /contracts/{id}/versions/{version}/revoke` | 撤销版本（需带 `expected_revision`） |
| `POST /contracts/{id}/versions/{version}/validate` | 运行时校验 |
| `GET  /contracts/{id}/compatibility?from_version=&to_version=` | 相邻版本兼容性判定 |

### 3.1 注册流转

1. 同一 `id` 下 `(id, version)` 重复注册 → `409 DUPLICATE_CONTRACT`；
2. 新版本号必须**严格大于**该 `id` 当前最新版本，否则
   → `422 VERSION_NOT_NEWER`；
3. 契约本身格式不合法 → `400 INVALID_CONTRACT`（`details.reason` 细分到
   非法字段，如 `invalid_id`、`not_an_array`、`duplicate_field`、
   `unsupported_type`）；
4. 字段数超过 `max_fields` 或版本数超过 `max_versions`
   → `413 LIMIT_EXCEEDED`，且不会残留任何部分状态。

### 3.2 更新与撤销（乐观并发）

- 每个契约带有单调递增的 `revision`，注册时为 `0`，每次成功的
  更新/撤销 +1；
- 更新与撤销都必须在请求体中携带 `expected_revision`，与当前注册态不一致时
  → `409 REVISION_CONFLICT`，调用方重新读取后重试，因此并发写入不会
  覆盖丢失；
- 撤销是终态：已撤销版本**不再参与校验、更新与再次撤销**，统一返回
  `410 REVOKED`；显式查询历史需在内部接口带 `include_revoked=true`。
  撤销后仍可注册更新的版本。

### 3.3 运行时校验

请求体：

```json
{
  "request":  {"user_id": 1},
  "response": {"user_id": 2},
  "declared_version": "1.0.0"
}
```

三类“未按契约调用”被严格区分：

| 场景 | 结果 |
| --- | --- |
| `id` 或版本未注册 | `404 NOT_FOUND` |
| 版本已撤销 | `410 REVOKED` |
| `declared_version` 与目标版本不一致 | `422 VERSION_MISMATCH` |
| 载荷违反字段契约 | HTTP 200，响应中 `valid=false` + 违规列表 |

违规原因（可区分）：`missing_required` / `wrong_type` / `unknown_field`。
校验是纯读操作：**失败不会修改注册表，也不会影响后续请求**。

### 3.4 错误响应与细节暴露

统一形态：

```json
{"error": {"code": "REVOKED", "message": "契约版本已撤销……"}}
```

`code` 始终返回以保证原因可区分；内部 `details` 仅在
`LimitConfig.expose_internal_details=true` 时返回，默认不泄漏内部细节。

## 4. 兼容性规则

仅支持**相邻注册版本**（按注册顺序相邻，含被撤销版本）之间的判定；
非相邻对返回 `422 VERSION_NOT_ADJACENT`，并在内部详情中给出已注册顺序。

从“旧版本的调用方在新版本下仍可工作”的视角：

| 变更 | 分类 | 结论 |
| --- | --- | --- |
| 删除字段 | `FIELD_REMOVED` | 破坏性 |
| 可选字段变为必填 | `REQUIRED_SCOPE_EXPANDED` | 破坏性 |
| 新增字段直接必填（必填范围扩大） | `REQUIRED_SCOPE_EXPANDED` | 破坏性 |
| 类型收窄（`NUMBER → INTEGER`） | `TYPE_NARROWED` | 破坏性 |
| 互不兼容的类型替换（`STRING → INTEGER` 等） | `TYPE_INCOMPATIBLE` | 破坏性 |
| 新增可选字段 | `FIELD_ADDED` | 兼容 |
| 必填字段变为可选 | `OPTIONAL_RELAXED` | 兼容 |
| 类型放宽（`INTEGER → NUMBER`） | `TYPE_WIDENED` | 兼容 |
| 无字段差异 | — | 兼容 |

响应同时包含逐条 `changes` 与人类可读的 `basis`（判定依据）。
判定为纯函数，并按 `(id, from_version, to_version, 双方 revision)` 缓存：
同一对版本、相同修订的结论**逐字节可复现**；字段集合被更新后缓存自动失效。

## 5. 可配置上限

`app/limits.py::LimitConfig`：

| 配置 | 默认 | 超限行为 |
| --- | --- | --- |
| `max_fields` | 128 | 拒绝注册 / 更新（`LIMIT_EXCEEDED`） |
| `max_versions` | 64 | 拒绝继续注册新版本 |
| `max_payload_nodes` | 10 000 | 拒绝校验（递归计数 JSON 节点） |
| `max_validation_ms` | 1000 | 超过耗时预算拒绝校验 |
| `max_error_details` | 10 | 违规条目上限，按策略 `REJECT` 或 `TRUNCATE` |
| `on_limit_exceeded` | `REJECT` | `REJECT`（拒绝）/ `TRUNCATE`（截断违规列表并标记 `truncated=true`） |
| `expose_internal_details` | `false` | 是否在错误响应中回传内部 `details` |

所有拒绝路径都不写入状态，因此不会残留半成品或污染已有契约。

## 6. 并发保证

- 全部注册态由单一互斥锁守护，写操作在锁内“构造不可变 `Contract` →
  一次性发布”，不存在半更新；
- 读操作在锁内只取不可变快照，耗时校验在锁外进行，读方不会看到
  未完成写入或过期快照；
- 更新 / 撤销基于 `revision` 的乐观并发控制，冲突显式返回
  `REVISION_CONFLICT`，杜绝覆盖丢失。

## 7. 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（重点覆盖：非法版本流转、破坏性变更、并发写入）
uv run pytest -q

# 打印契约标识、判定依据与归一化结果
uv run pytest -o log_cli=true -o log_cli_level=INFO

# 启动服务
uv run uvicorn main:app --reload
```

手动体验：

```bash
curl -s -X POST localhost:8000/contracts -H 'content-type: application/json' \
  -d '{"id":"user-service","version":"1.0.0",
       "fields":[{"name":"user_id","type":"INTEGER","required":true}]}'

curl -s -X POST localhost:8000/contracts/user-service/versions/1.0.0/validate \
  -H 'content-type: application/json' \
  -d '{"request":{"user_id":"bad-id"},"response":{}}'
```

## 8. 代码结构

```
app/
  errors.py      # 稳定错误码 ErrorCode 与 ContractError
  version.py     # 语义版本解析 / 比较
  contract.py    # 契约模型与归一化
  limits.py      # 上限与超限策略配置
  validation.py  # 运行时纯函数校验
  compat.py      # 相邻版本兼容性规则
  registry.py    # 线程安全注册表（锁 + 不可变快照 + revision CAS）
  service.py     # 编排门面（HTTP 入参解析）
  api.py         # FastAPI 入口与错误码 → HTTP 状态码映射
  tests/         # 版本流转 / 兼容性 / 并发 / 校验与 HTTP 测试
main.py          # uvicorn 入口
```
