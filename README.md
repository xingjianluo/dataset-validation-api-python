# 本地数据集校验 API

纯本地运行的数据集批量校验服务（FastAPI），不依赖任何外部服务、凭据或付费资源。
提供：

- 校验任务提交与结果查询（基于 `idempotency_key` 的幂等语义）
- 并发执行上限与准入控制（超限**立即拒绝，不排队**）
- 逐条记录的结构与取值校验（失败可定位到下标/字段/依据，部分失败不丢整批）

## 运行

```bash
uv sync
uv run uvicorn main:app --reload
# 交互文档： http://127.0.0.1:8000/docs
```

## 接口

### 1. 提交校验 `POST /validations`

请求体：

```json
{
  "idempotency_key": "batch-2026-09-23-001",
  "records": [
    {"id": 1, "name": "alice", "age": 30, "score": 0.9, "tags": ["vip"]}
  ]
}
```

- `idempotency_key`：非空字符串，最长 200，由调用方生成并保证唯一。
- `records`：数组，最多 1000 条。每条必须是对象，字段规则见下文。

成功响应（HTTP 200，同步返回最终结论）：

```json
{
  "idempotency_key": "batch-2026-09-23-001",
  "status": "COMPLETED",
  "cached": false,
  "conclusion": "VALIDATION_PASSED",
  "result": {
    "total": 1, "passed": 1, "failed": 0, "all_passed": true,
    "records": [{"index": 0, "valid": true, "field": null, "reason": null}]
  }
}
```

- `cached=false` 表示该请求是首次提交并真正执行了校验；
  `cached=true` 表示命中首次提交产生的同一份结论。
- 数据不通过时**仍是 HTTP 200**（请求被正常受理并执行），
  `conclusion=VALIDATION_FAILED`，`result.all_passed=false`，
  每条失败记录都带 `index / field / reason`。

### 2. 查询结果 `GET /validations/{idempotency_key}`

返回该标识对应任务的状态与结论。任务执行中返回 `"status": "RUNNING"`，
完成后返回 `COMPLETED` 与完整 `result`。标识不存在返回 404（见错误分类）。

## 幂等语义

- 同一个 `idempotency_key` 的**相同载荷**（以记录内容的规范化 SHA-256
  指纹判定，键顺序不影响）只真正执行**一次**校验。
- 首次提交者执行；并发到达的同 key 同载荷提交会等待执行完成，
  然后返回与首次**完全相同**的结论（`cached=true`），不会重复执行，
  也不会产生两份互相矛盾的结果。
- 同一个 key 提交**不同载荷**视为调用方错误，返回
  `409 IDEMPOTENCY_CONFLICT`，不会覆盖首次结论。
- 结论保存在进程内存中（纯本地、无持久化），进程重启后缓存清空。

## 并发与准入规则

- 全局并发执行上限 `MAX_CONCURRENCY = 2`（见 `app/api.py`），
  只统计“正在执行中”的不同 key 任务；同 key 重复提交复用结果，不占新槽位。
- 当在执行任务数已达上限时，新的**不同 key**提交被**立即拒绝**：
  `503 CAPACITY_REJECTED`，响应体携带当前上限。
  服务端**不排队、不阻塞等待**，因此不会出现“静默排队到超时”。
- 被拒绝的请求不写入任何缓存、不占用槽位；等有槽位空出后，
  用原请求重试即可被正常受理。
- 阻塞式校验运行在线程池中，提交接口本身不会阻塞事件循环。

## 错误分类（三类结论严格区分，不混用）

| 场景 | HTTP | 稳定错误码 `error.code` | 是否执行 |
| --- | --- | --- | --- |
| JSON 非法、字段缺失/类型错误、记录数超限 | 400 | `INVALID_REQUEST` | 未执行 |
| 并发槽位已满，准入拒绝 | 503 | `CAPACITY_REJECTED` | 未执行 |
| 同 key 携带不同载荷 | 409 | `IDEMPOTENCY_CONFLICT` | 未执行 |
| 查询未知标识 | 404 | `NOT_FOUND` | — |
| **数据校验不通过** | **200** | 无（`conclusion=VALIDATION_FAILED`） | **已执行一次** |

错误响应统一结构：

```json
{"error": {"code": "CAPACITY_REJECTED",
           "message": "并发执行数已达上限 2，请求被拒绝（不排队）",
           "details": {"max_concurrency": 2}}}
```

## 记录校验规则

| 字段 | 类型 | 取值要求 |
| --- | --- | --- |
| `id` | 整数（布尔值不算整数） | 非负 |
| `name` | 字符串 | 去空白后非空，长度 ≤ 100 |
| `age` | 整数 | [18, 120] |
| `score` | 数值（布尔值不算） | [0.0, 1.0] |
| `tags` | 字符串数组 | 元素个数 [1, 10]，元素均为字符串 |

- 记录必须是 JSON 对象；出现未定义字段判为“未知字段”。
- 字段缺失、类型不匹配、取值非法分别给出明确中文依据；
  每条记录只报首个失败依据，检查顺序固定，因此判定可复现。
- 每条记录都返回判定结果；部分记录失败不影响其他记录，也不丢失整批结果。
- 汇总结论只取决于记录集合：总条数、通过数、失败数及各失败依据
  不受记录提交顺序影响。

## 本地验证方法

```bash
uv run pytest -v
```

测试会打印每个用例的输入（`校验输入: records=...`）与判定依据
（`判定依据: index=.. field=.. reason=..`）。覆盖场景：

1. **并发重复提交**：6 个线程同时提交同一 `idempotency_key`，
   断言底层校验只执行 1 次、6 份响应结论逐字段一致、仅 1 份 `cached=false`。
2. **超限拒绝**：占满 2 个槽位后第 3 个不同 key 请求立即收到
   `503 CAPACITY_REJECTED`，并与 `400 INVALID_REQUEST`、
   `200 VALIDATION_FAILED` 逐场景区分。
3. **部分失败**：好/坏/缺字段记录混提，断言整批保留、计数正确、
   失败依据可定位，且打乱顺序后结论不变。
4. 幂等冲突（409）、未知标识查询（404）等错误码稳定性。

手动快速验证（另开终端）：

```bash
curl -s -X POST http://127.0.0.1:8000/validations \
  -H 'content-type: application/json' \
  -d '{"idempotency_key":"demo","records":[{"id":1,"name":"a","age":30,"score":1,"tags":["x"]}]}'
curl -s http://127.0.0.1:8000/validations/demo
```

## 代码结构

- `app/models.py`：请求/响应与错误码模型
- `app/validation.py`：逐条记录结构与取值校验（纯函数，顺序无关）
- `app/manager.py`：幂等缓存、同 key 等待、并发槽位准入（线程安全）
- `app/api.py`：HTTP 接口、参数校验、错误分类
- `app/errors.py`：统一错误响应结构
- `tests/`：校验器、管理器、HTTP 三层测试
