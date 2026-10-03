# 本地数据集校验 API

纯本地运行的数据集批量校验服务（FastAPI），不依赖任何外部服务、凭据或付费资源。
提供：

- 校验任务提交与结果查询（基于 `idempotency_key` 的幂等语义）
- 并发执行上限与准入控制（超限**立即拒绝，不排队**）
- 逐条记录的结构与取值校验（失败可定位到下标/字段/依据，部分失败不丢整批）
- 每条不通过记录一次性返回**完整问题清单**（有多少报多少，顺序固定、乱序稳定）
- 任务**执行失败**也有独立终态结论（`EXECUTION_FAILED`），可查询、可幂等复用，
  失败后容量自动回收、并发等待者收敛到同一份结论
- 本地容量占用观测端点（`GET /capacity`），压测后可一眼确认占用未泄漏

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
  `conclusion=VALIDATION_FAILED`，`result.all_passed=false`。
- 执行过程本身失败时同样是 **HTTP 200**，
  `conclusion=EXECUTION_FAILED`，携带 `execution_error`（见下文）。

#### 完整问题清单（新增 `issues`，老字段保留）

每条不通过记录会一次性列出**命中的全部问题**，而不是只报一个。每个问题包含
稳定机器码 `code`、精确定位 `location`、归属字段 `field`、中文依据 `reason`：

```json
{
  "conclusion": "VALIDATION_FAILED",
  "result": {
    "total": 1, "passed": 0, "failed": 1, "all_passed": false,
    "records": [{
      "index": 0, "valid": false,
      "field": "extra",
      "reason": "未知字段，不在允许的字段集合内",
      "issues": [
        {"code": "UNKNOWN_FIELD",  "location": "extra", "field": "extra",
         "reason": "未知字段，不在允许的字段集合内"},
        {"code": "MISSING_FIELD",  "location": "age",   "field": "age",
         "reason": "字段缺失"},
        {"code": "VALUE_INVALID",  "location": "id",    "field": "id",
         "reason": "取值非法：id 必须为非负整数"},
        {"code": "TYPE_MISMATCH",  "location": "score", "field": "score",
         "reason": "类型不匹配：score 必须是数值"},
        {"code": "INVALID_ELEMENT", "location": "tags[0]", "field": "tags",
         "reason": "类型不匹配：tags 元素必须是字符串"},
        {"code": "INVALID_ELEMENT", "location": "tags[2]", "field": "tags",
         "reason": "类型不匹配：tags 元素必须是字符串"}
      ]
    }]
  }
}
```

- 问题码 `code` 取值：`NOT_OBJECT`（记录不是对象）、`UNKNOWN_FIELD`（未知字段）、
  `MISSING_FIELD`（字段缺失）、`TYPE_MISMATCH`（类型不对）、
  `VALUE_INVALID`（取值越界/非法）、`INVALID_ELEMENT`（数组元素有问题）。
- `location` 的取值规则：字段级问题为字段名（`age`）；数组元素问题为元素位置
  （`tags[0]`）；记录级问题（不是 JSON 对象）为 `null`。
- **顺序固定且与提交顺序无关**：单条记录内按
  未知字段（字段名排序）→ 缺失字段（声明顺序）→ 各字段类型/取值
  （按 `id, name, age, score, tags` 声明顺序）→ 数组元素（按下标）排列。
  整批记录乱序提交时，同一条记录的问题清单与整批汇总数字都不变。
- **向后兼容**：老的记录级字段 `field` / `reason` 保留，语义不变
  （等于 `issues[0].location` / `issues[0].reason`）；新增内容只叠加，
  老调用方不升级也能继续解析。

#### 执行失败结论 `EXECUTION_FAILED`

执行过程中抛出异常（而非数据本身不合法）时返回：

```json
{
  "idempotency_key": "batch-...",
  "status": "COMPLETED",
  "result": null,
  "cached": false,
  "conclusion": "EXECUTION_FAILED",
  "execution_error": {"type": "RuntimeError", "message": "执行依赖不可用"}
}
```

- 执行失败是一种**终态结论**，与参数非法（400）、超限拒绝（503）、
  数据不通过（200 + `VALIDATION_FAILED`）四者严格区分，不会互相糊弄。
- 失败结论按 `idempotency_key` + 载荷指纹缓存：同一个标识、同样载荷再次提交
  （或首次执行期间并发到达的同 key 提交）一律返回同一份失败结论，
  **不会重复执行，也不会产生与第一次矛盾的结论**（响应中 `cached=true`）。

### 2. 查询结果 `GET /validations/{idempotency_key}`

返回该标识对应任务的状态与结论。任务执行中返回 `"status": "RUNNING"`，
完成后返回 `COMPLETED` 与完整 `result` / `conclusion`（执行失败时为
`conclusion=EXECUTION_FAILED` 且带 `execution_error`）。标识不存在返回 404。

### 3. 容量观测 `GET /capacity`

返回当前进程内的容量占用快照，无需任何参数：

```json
{"inflight": 0, "max_concurrency": 2, "available": 2}
```

压测/故障演练后调用一次，`inflight` 回到 `0` 即说明容量没有泄漏。

## 幂等语义

- 同一个 `idempotency_key` 的**相同载荷**（以记录内容的规范化 SHA-256
  指纹判定，键顺序不影响）只真正执行**一次**校验。
- 首次提交者执行；并发到达的同 key 同载荷提交会等待执行完成，
  然后返回与首次**完全相同**的结论（`cached=true`），不会重复执行，
  也不会产生两份互相矛盾的结果。
- 上述复用对三种终态结论一视同仁：校验通过、数据不通过、**执行失败**。
  即执行抛异常后，同标识同载荷再提交仍返回首次那份 `EXECUTION_FAILED`。
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

### 失败后的容量与等待收敛

- 任务结束（**校验通过、数据不通过、执行失败均包括**）后，并发槽位一定释放，
  占用回到基线；可用 `GET /capacity` 的 `inflight` 直接核对。
- 首次执行期间在等待的同 key 并发提交，在执行失败时也会被**及时唤醒**，
  全部收敛到同一份 `EXECUTION_FAILED` 结论，不会永久卡住，
  更不会因此变成事实上的排队（它们不占额外槽位）。
- 执行失败的任务不影响后续其他 key：槽位释放后新任务可立即受理。

## 判定分类（四类严格区分，不混用）

| 场景 | HTTP | 稳定标识 | 是否执行 |
| --- | --- | --- | --- |
| JSON 非法、字段缺失/类型错误、记录数超限 | 400 | `error.code=INVALID_REQUEST` | 未执行 |
| 并发槽位已满，准入拒绝 | 503 | `error.code=CAPACITY_REJECTED` | 未执行 |
| 同 key 携带不同载荷 | 409 | `error.code=IDEMPOTENCY_CONFLICT` | 未执行 |
| 查询未知标识 | 404 | `error.code=NOT_FOUND` | — |
| **数据校验全部通过** | **200** | `conclusion=VALIDATION_PASSED` | **已执行一次** |
| **数据校验不通过** | **200** | `conclusion=VALIDATION_FAILED` | **已执行一次** |
| **执行过程失败（异常）** | **200** | `conclusion=EXECUTION_FAILED`（带 `execution_error`） | **已执行一次** |

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
  **每条记录一次性报出全部命中问题**，检查顺序固定（未知字段按名字排序 →
  缺失字段按声明顺序 → 类型/取值按字段声明顺序 → 数组元素按下标），
  因此问题清单可复现、乱序提交不变。
- 每条记录都返回判定结果；部分记录失败不影响其他记录，也不丢失整批结果。
- 汇总结论只取决于记录集合：总条数、通过数、失败数及各失败依据
  不受记录提交顺序影响。

## 本地验证方法

```bash
uv run pytest -v
```

测试会打印每个用例的输入（`校验输入: records=...`）、逐条判定依据
（`判定依据: index=.. code=.. field=.. reason=..`，一条记录多个问题会打多行），
以及执行失败告警（`执行失败: idempotency_key=.. error_type=.. error=..`）。
覆盖场景：

1. **并发重复提交**：多个线程同时提交同一 `idempotency_key`，
   断言底层校验只执行 1 次、所有响应结论逐字段一致、仅 1 份 `cached=false`。
2. **超限拒绝**：占满 2 个槽位后第 3 个不同 key 请求立即收到
   `503 CAPACITY_REJECTED`，并与 `400 INVALID_REQUEST`、
   `200 VALIDATION_FAILED` 逐场景区分。
3. **部分失败**：好/坏/缺字段记录混提，断言整批保留、计数正确、
   失败依据可定位，且打乱顺序后结论不变。
4. **完整问题清单**：一条记录同时命中未知字段、缺失、类型、越界、
   数组元素问题时，断言 `issues` 数量完整、顺序固定；打乱整批提交顺序后
   同一记录的清单逐字段一致；老的 `field/reason` 仍指向首个问题。
5. **执行失败与容量恢复**：校验器抛异常后，断言结论为
   `EXECUTION_FAILED`（带异常类型与信息）、`GET` 可查询、
   同标识同载荷再提交 `cached=true` 且只执行一次、`capacity.inflight` 回到 0、
   其他 key 不被阻塞。
6. **失败并发收敛**：6 个线程并发提交同一 key 且执行失败，
   断言全部线程都拿到同一份失败结论、无人卡死、容量回基线。
7. **容量观测**：`GET /capacity` 在执行中为 `inflight=1`、结束后为 0。
8. 幂等冲突（409）、未知标识查询（404）等错误码稳定性。

手动快速验证（另开终端）：

```bash
uv run uvicorn main:app --reload

# 一条记录命中多个问题
curl -s -X POST http://127.0.0.1:8000/validations \
  -H 'content-type: application/json' \
  -d '{"idempotency_key":"demo","records":[{"id":-1,"name":"a","score":"x","tags":[1],"extra":1}]}'

# 容量占用（压测后确认 inflight=0）
curl -s http://127.0.0.1:8000/capacity

# 查询结论
curl -s http://127.0.0.1:8000/validations/demo
```

## 代码结构

- `app/models.py`：请求/响应与错误码模型
- `app/validation.py`：逐条记录结构与取值校验（纯函数，顺序无关）
- `app/manager.py`：幂等缓存、同 key 等待、并发槽位准入（线程安全）
- `app/api.py`：HTTP 接口、参数校验、错误分类
- `app/errors.py`：统一错误响应结构
- `tests/`：校验器、管理器、HTTP 三层测试
