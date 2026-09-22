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
| `expose_error_details`     | True  | 关闭时错误消息替换为通用文案，不泄漏字段细节 |

## HTTP 接口

| 方法   | 路径                                     | 说明               |
| ------ | ---------------------------------------- | ------------------ |
| POST   | `/contracts`                             | 注册契约           |
| GET    | `/contracts/{id}`                        | 查询当前版本       |
| PUT    | `/contracts/{id}`                        | 更新到下一版本     |
| DELETE | `/contracts/{id}`                        | 撤销契约           |
| POST   | `/contracts/{id}/validate`               | 运行时校验 payload |
| GET    | `/contracts/{id}/compatibility?from_version=&to_version=` | 兼容性判定 |

错误统一返回 `{"error": {"reason": <原因码>, "message": <安全消息>}}`，
原因码与 HTTP 状态码稳定映射（404/409/413/422）。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试
uv run pytest -q -o log_cli=true   # 查看契约标识、判定依据与归一化结果日志
uv run uvicorn main:app --reload   # 启动 HTTP 服务
```
