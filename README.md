# 本地数据集校验接口

一个纯本地运行的数据集校验服务：提交一批记录，逐条做结构与取值校验，并给出
**一致、可解释、与提交顺序无关**的结论。所有校验数据与运行状态都在进程内存中
本地产生，**不依赖任何外部服务、凭据或付费资源**。

- 框架：FastAPI（同步路由 + 线程安全的内存管理器）
- Python：>= 3.14，依赖管理：uv

---

## 1. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/validations` | 提交（或幂等复用）一个校验任务 |
| `GET`  | `/api/v1/validations/{idempotency_key}` | 按幂等键查询已固化的结论 |
| `GET`  | `/health` | 健康检查，返回当前活动任务数与并发上限 |

### 提交请求体

```json
{
  "idempotency_key": "batch-2026-001",
  "records": [
    {"id": 1, "name": "alice", "age": 30, "email": "a@x.com", "score": 92.5}
  ]
}
```

- `idempotency_key`：必填，去空白后长度 1~128 的字符串。
- `records`：数组，最多 1000 条；允许空数组（空批次整体判定为合法）。
- 请求信封不允许出现额外字段。

### 单条记录校验规则（schema）

| 字段 | 类型 | 取值要求 |
| --- | --- | --- |
| `id` | 整数（`bool` 不算） | 正整数 `>= 1` |
| `name` | 字符串 | 去空白后非空，长度 `<= 100` |
| `age` | 整数（`bool` 不算） | `1 ~ 150` |
| `email` | 字符串 | 长度 `<= 254`，形如 `local@domain.tld` |
| `score` | 数值（`bool` 不算） | 有限数，`0 ~ 100` |

每个问题都返回可定位的依据：`field`（字段名；整条记录不是对象时为 `__record__`）、
`code`（问题分类）、`message`（人话说明）、`expected`（合法期望）、`actual`（实际值）。

记录级问题码：

- `missing_field`：必填字段缺失
- `type_mismatch`：类型不匹配
- `invalid_value`：类型正确但取值非法
- `unknown_field`：出现 schema 之外的字段

响应中的每条记录保留其在批次中的下标 `index`；部分记录失败不会导致整批结果丢失，
只会把对应记录标记为 `valid=false`，并令 `overall_valid=false`。

---

## 2. 幂等语义

- 结论以 `idempotency_key` 为唯一标识，**首次提交真正执行一次**，随后结论被固化。
- 同一幂等键的重复提交（无论请求体里的 `records` 是否相同）一律返回**首次结论**，
  且 HTTP 响应体逐字节一致；重复提交不占用并发执行名额。
- **并发提交相同键**也安全：每个键有一把串行锁，首个持锁者执行，其余同键提交者
  在锁上短暂等待后复用同一结论对象。因此同键绝不会执行两次，也不会产生两份
  互相矛盾的结论（实现见 `app/manager.py`，证据见 `tests/test_manager.py` 与
  `tests/test_api.py` 的并发用例）。
- 结论与提交顺序无关：逐条校验是纯函数，同一条记录在批次任意位置得到相同问题；
  问题列表按字段名/问题码排序，汇总只取决于记录内容。

---

## 3. 并发执行上限与准入规则

- 真正“正在执行”的任务数有全局上限，默认 `4`，可用环境变量
  `VALIDATION_MAX_CONCURRENCY` 覆盖（启动服务时设置）。
- 准入判断是**非阻塞**的：当活动任务数已达上限时，**全新键**的提交被**立即拒绝**，
  返回 `503` 与可区分原因 `concurrency_limit`，**不排队、不静默等待到超时**。
- 同键的并发重复提交不算新增负载：即使名额已满，它也只是等待首个执行结束并复用，
  不会被当作超限请求拒绝。
- `/health` 实时返回 `active`（活动数）与 `max_concurrency`（上限）。

---

## 4. 错误分类（三类结论严格区分，互不混淆）

| 结论 | HTTP 状态码 | 响应 `category` | `error` | 含义 |
| --- | --- | --- | --- | --- |
| 参数非法 | `422` | `invalid_request` | `invalid_request` | 信封/字段不合规，任务未进入执行 |
| 超限拒绝 | `503` | `rejected` | `admission_rejected` | 参数合法但准入控制拒绝，`details.reason=concurrency_limit` |
| 校验不通过 | `200` | —（正常业务响应） | — | `outcome.status=completed` 且 `result.overall_valid=false` |
| 查询未命中 | `404` | `not_found` | `not_found` | 该幂等键尚无固化结论 |

错误响应统一形态：

```json
{
  "error": "admission_rejected",
  "category": "rejected",
  "message": "已达并发执行上限……",
  "details": {"reason": "concurrency_limit", "max_concurrency": 4, "active": 4}
}
```

> 注意：“记录里有非法值”属于**校验不通过（200 + overall_valid=false）**，
> 与“请求参数本身非法（422）”和“系统繁忙被拒绝（503）”是三类完全不同的结论。

---

## 5. 代码结构

```
app/
  models.py     # 请求/响应/枚举等类型定义
  errors.py     # 统一错误分类（参数非法 / 超限拒绝 / 未命中）
  validator.py  # 逐条记录的纯函数校验（无 I/O、确定性）
  manager.py    # 幂等去重（每键一把锁）+ 非阻塞并发准入
  api.py        # FastAPI 路由、统一错误响应与日志
main.py         # 服务入口（main:app）
tests/          # pytest 单测（并发幂等 / 超限拒绝 / 部分失败 / 分类稳定性）
```

---

## 6. 本地验证方法

以下命令均在本机离线运行，无需任何凭据。

### 安装依赖

```bash
uv sync --group dev
```

### 运行测试（含输入与判定依据日志）

```bash
uv run pytest
```

- 控制台实时打印 INFO 日志；
- 每条用例的**输入**与**判定依据**同时写入 `tests/test-run.log`，可事后核查，例如：

```bash
grep "判定依据" tests/test-run.log
grep "执行次数" tests/test-run.log      # 并发同键只执行一次的证据
```

测试覆盖：
- 并发重复提交：16 / 8 个线程同键并发，断言执行器真实调用仅 1 次、响应完全一致；
- 超限拒绝：占满名额后新键立即 503，`reason=concurrency_limit`，不排队；
- 部分失败：一批中部分记录非法，整批结果不丢失、计数与定位准确；
- 错误分类稳定可区分：422 / 503 / 200(overall_valid=false) / 404 互不混淆；
- 顺序无关：打乱记录顺序后汇总与逐条问题一致。

### 启动服务

```bash
VALIDATION_MAX_CONCURRENCY=4 uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

### 手工调用示例

```bash
# 提交（部分失败：第 2 条记录非法）
curl -s -X POST http://127.0.0.1:8000/api/v1/validations \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"demo-1","records":[
    {"id":1,"name":"alice","age":30,"email":"a@x.com","score":90},
    {"id":"bad","age":200}
  ]}'

# 重复提交同一键（返回与首次完全一致的结论）
curl -s -X POST http://127.0.0.1:8000/api/v1/validations \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"demo-1","records":[]}'

# 查询
curl -s http://127.0.0.1:8000/api/v1/validations/demo-1

# 参数非法（422 invalid_request）
curl -i -X POST http://127.0.0.1:8000/api/v1/validations \
  -H 'Content-Type: application/json' -d '{"idempotency_key":"","records":[]}'
```

交互式文档：服务启动后访问 `http://127.0.0.1:8000/docs`。
