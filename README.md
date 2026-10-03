# 本地数据集校验 API

纯本地运行的数据集批量校验服务（FastAPI），不依赖任何外部服务、凭据或付费资源。
提供：

- 校验任务提交与结果查询（基于 `idempotency_key` 的幂等语义）
- 并发执行上限与准入控制（超限**立即拒绝，不排队**）
- 逐条记录的结构与取值校验（一条记录一次性给出全部命中问题，
  可定位到下标/字段/数组位置/依据，部分失败不丢整批）
- **执行失败结论**（校验执行过程本身异常时给出明确结论并缓存复用）
- 本地容量观察端点 `GET /capacity`（压测后一眼确认槽位无泄漏）

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
  每条失败记录都带 `index / field / reason`（首个命中问题，兼容字段），
  并通过 `records[].issues` 一次性给出该记录命中的**全部**问题。

数据不通过的响应片段（issues 为新增字段）：

```json
{
  "conclusion": "VALIDATION_FAILED",
  "result": {
    "total": 1, "passed": 0, "failed": 1, "all_passed": false,
    "records": [{
      "index": 0, "valid": false,
      "field": "extra", "reason": "未知字段，不在允许的字段集合内",
      "issues": [
        {"field": "extra",  "reason": "未知字段，不在允许的字段集合内"},
        {"field": "name",   "reason": "字段缺失"},
        {"field": "id",     "reason": "取值非法：id 必须为非负整数"},
        {"field": "age",    "reason": "类型不匹配：age 必须是整数"},
        {"field": "score",  "reason": "取值非法：score 必须在 [0.0, 1.0] 范围内"},
        {"field": "tags[1]","reason": "类型不匹配：tags 元素必须是字符串"}
      ]
    }]
  }
}
```

- 每个问题都带 `field`（字段名；数组元素为 `tags[下标]`；记录本身非法时为
  `null`）与 `reason`（为什么不行），命中多少报多少，互不掩盖。
- 问题顺序固定：未知字段（字段名升序）→ 缺失字段（schema 顺序）→
  字段级问题（schema 顺序，字段内先类型后取值）→ tags 元素（下标升序）。
  因此键顺序打乱、整批记录乱序提交，单条记录的问题清单与汇总数字都不变。

### 执行失败结论 `EXECUTION_FAILED`

校验任务**被受理且执行过一次**，但执行过程本身抛异常（而非数据问题）时，
返回 HTTP 200、`conclusion=EXECUTION_FAILED`，`result=null`，
失败结论放在 `failure` 中：

```json
{
  "conclusion": "EXECUTION_FAILED",
  "result": null,
  "failure": {"type": "RuntimeError", "message": "执行过程失败（模拟）"},
  "cached": false
}
```

- 四类结论严格区分、互不糊弄：

  | 结论 | 形态 |
  | --- | --- |
  | 参数写错 | HTTP 400 `INVALID_REQUEST`（未执行） |
  | 超限拒绝 | HTTP 503 `CAPACITY_REJECTED`（未执行、不排队） |
  | 数据不通过 | HTTP 200 `conclusion=VALIDATION_FAILED` |
  | **执行失败** | HTTP 200 `conclusion=EXECUTION_FAILED`，`failure` 非空 |

- 执行失败同样是**最终结论并被缓存**：同一个 `idempotency_key` 的**相同载荷**
  再次提交（无论首次提交的等待者还是事后重提）直接复用同一份失败结论
  （`cached=true`），**不会重复执行**，也不会冒出与第一次矛盾的结论。
  失败结论同样可以通过 `GET /validations/{key}` 查询到。
- 执行失败后并发槽位**立即归还**，承载能力回到基线，不会占着容量不放；
  执行期间并发到达的同 key 提交在任务失败后被**唤醒并收敛到同一份结论**，
  及时返回，不永久卡住，也不变成事实上的排队。

### 2. 查询结果 `GET /validations/{idempotency_key}`

返回该标识对应任务的状态与结论。任务执行中返回 `"status": "RUNNING"`，
完成后返回 `COMPLETED`、`conclusion` 与完整 `result`（执行失败时为
`failure`）。标识不存在返回 404（见错误分类）。

### 3. 容量观察 `GET /capacity`

```json
{"inflight": 0, "max_concurrency": 2, "available": 2}
```

压测前后各查一次即可确认 `inflight` 回到 0、没有槽位泄漏。

## 幂等语义

- 同一个 `idempotency_key` 的**相同载荷**（以记录内容的规范化 SHA-256
  指纹判定，键顺序不影响）只真正执行**一次**校验。
- 首次提交者执行；并发到达的同 key 同载荷提交会等待执行完成，
  然后返回与首次**完全相同**的结论（`cached=true`），不会重复执行，
  也不会产生两份互相矛盾的结果。**校验通过、数据不通过、执行失败**
  三种最终结论都遵循该复用规则。
- 同一个 key 提交**不同载荷**视为调用方错误，返回
  `409 IDEMPOTENCY_CONFLICT`，不会覆盖首次结论。
- 结论保存在进程内存中（纯本地、无持久化），进程重启后缓存清空。

## 并发与准入规则

- 全局并发执行上限 `MAX_CONCURRENCY = 2`（见 `app/api.py`），
  只统计“正在执行中”的不同 key 任务；同 key 重复提交复用结果，不占新槽位。
- 当在执行任务数已达上限时，新的**不同 key**提交被**立即拒绝**：
  `503 CAPACITY_REJECTED`，响应体携带当前上限。
  服务端**不排队、不阻塞等待**，因此不会出现“静默排队到超时”。
- 被拒绝的请求不写入任何缓存、不占用槽位（`GET /validations/{被拒绝的key}`
  返回 404，`/capacity` 占用不增加）；等有槽位空出后，用原请求重试即可受理。
- 无论任务以通过、不通过还是执行失败结束，槽位都会释放、容量回到基线；
  等待同一标识的并发提交在执行失败时同样被唤醒并收敛到同一份失败结论，
  不会永久卡住。
- 阻塞式校验运行在线程池中，提交接口本身不会阻塞事件循环。

## 错误分类（四类结论严格区分，不混用）

| 场景 | HTTP | 稳定错误码 / 结论 | 是否执行 |
| --- | --- | --- | --- |
| JSON 非法、字段缺失/类型错误、记录数超限 | 400 | `error.code=INVALID_REQUEST` | 未执行 |
| 并发槽位已满，准入拒绝 | 503 | `error.code=CAPACITY_REJECTED` | 未执行 |
| 同 key 携带不同载荷 | 409 | `error.code=IDEMPOTENCY_CONFLICT` | 未执行 |
| 查询未知标识 | 404 | `error.code=NOT_FOUND` | — |
| **数据校验不通过** | **200** | `conclusion=VALIDATION_FAILED` | **已执行一次** |
| **执行过程失败** | **200** | `conclusion=EXECUTION_FAILED`，`failure` 非空 | **已执行一次** |

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
  **一条记录一次性报出命中的全部问题**（`records[].issues`），有多少报多少，
  包括数组里逐个元素的位置错误（`tags[i]`），不再“改一条蹦一条”。
- 问题清单顺序固定（未知字段升序 → 缺失字段 → 各字段类型/取值 →
  tags 元素下标升序），与 JSON 键顺序、批次内记录顺序都无关，因此可复现。
- 每条记录都返回判定结果；部分记录失败不影响其他记录，也不丢失整批结果。
- 汇总结论只取决于记录集合：总条数、通过数、失败数及各问题清单
  不受记录提交顺序影响。

## 对老调用方的兼容范围

本次为**叠加式扩展**，老的字段与用法保持可用：

- 响应仍在原位置保留 `status / cached / result` 及
  `result.total/passed/failed/all_passed/records`。
- `records[].index / valid / field / reason` 语义不变：
  `field/reason` 仍为首个命中问题（老代码无需改动即可继续解析）；
  完整清单是**新增**的 `records[].issues` 字段，老代码忽略即可。
- `conclusion=VALIDATION_PASSED / VALIDATION_FAILED` 含义与判定方式不变；
  新增的第三种取值 `EXECUTION_FAILED` 与新增的顶层 `failure` 字段，
  对只认识前两种结论的老代码属于可忽略的额外内容。
- 新增 `GET /capacity` 端点，不改变任何既有端点的路径与状态码语义。

## 本地验证方法

```bash
uv run pytest -v
```

测试会打印每个用例的输入（`校验输入: records=...`）、每条命中问题的
判定依据（`判定依据: index=.. field=.. reason=..`）以及执行失败日志
（`执行失败: idempotency_key=.. error_type=.. message=..`）。覆盖场景：

1. **并发重复提交**：6/8 个线程同时提交同一 `idempotency_key`，
   断言底层校验只执行 1 次、全部响应结论逐字段一致、仅 1 份 `cached=false`。
2. **超限拒绝**：占满槽位后额外的不同 key 请求立即收到
   `503 CAPACITY_REJECTED`，被拒绝 key 不留占位、`/capacity` 占用不增加；
   并与 `400 INVALID_REQUEST`、`200 VALIDATION_FAILED`、
   `200 EXECUTION_FAILED` 逐场景区分。
3. **部分失败 + 完整问题清单**：好/坏/缺字段记录混提，断言整批保留、
   计数正确；单条记录同时命中未知字段/缺失/类型/越界/数组元素错误时
   issues 完整、顺序固定，打乱 JSON 键顺序与批次顺序后清单与汇总不变；
   老字段 `field/reason` 仍可用。
4. **执行失败收敛**：校验器抛异常时断言返回 `EXECUTION_FAILED`、
   失败后 `/capacity.inflight=0`、8 个同 key 并发提交全部收敛到同一份
   失败结论且只执行 1 次、事后同载荷重提仍复用（`cached=true`）、
   GET 可查询，失败类型与信息进日志。
5. 幂等冲突（409）、未知标识查询（404）等错误码稳定性。

手动快速验证（另开终端）：

```bash
uv run uvicorn main:app --reload
# 一次命中多个问题
curl -s -X POST http://127.0.0.1:8000/validations \
  -H 'content-type: application/json' \
  -d '{"idempotency_key":"demo","records":[{"id":-1,"age":"x","score":9,"tags":["ok",3],"extra":1}]}'
curl -s http://127.0.0.1:8000/validations/demo
curl -s http://127.0.0.1:8000/capacity
```

## 代码结构

- `app/models.py`：请求/响应、错误码与 `Issue / ExecutionFailure / CapacityView` 模型
- `app/validation.py`：逐条记录结构与取值校验（收集全部问题，纯函数，顺序无关）
- `app/manager.py`：幂等缓存（含失败结论）、同 key 等待收敛、并发槽位准入与容量快照
- `app/api.py`：HTTP 接口、参数校验、四类结论分类、`/capacity`
- `app/errors.py`：统一错误响应结构
- `tests/`：校验器、管理器、HTTP 三层测试
