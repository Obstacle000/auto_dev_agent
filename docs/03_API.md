# Auto-Dev Agent 接口文档

## 1. 接口总览

统一前缀 `/api/v1`，响应结构使用标准 JSON 或 `text/event-stream` (SSE)。所有项目与数据源配置均由用户动态传入，不存在默认测试路由或测试注入接口。

| 模块 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 任务编排 | `POST` | `/api/v1/tasks/chat-stream` | 创建或继续研发任务，SSE 流式推送进度 |
| 任务编排 | `POST` | `/api/v1/tasks/{task_id}/approval` | 审批代码 Diff，唤醒人工中断断点 |
| 任务编排 | `GET` | `/api/v1/tasks/{task_id}` | 查询任务详情、证据池、补丁与审批状态 |
| 任务编排 | `GET` | `/api/v1/tasks/{task_id}/evidences/{ev_id}` | 查看单条证据原文片段 |
| 项目管理 | `POST` | `/api/v1/projects` | 注册本地代码仓库工作区 |
| 项目管理 | `GET` | `/api/v1/projects` | 查询已注册项目列表 |
| 数据源 | `POST` | `/api/v1/datasources` | 接入用户外部数据源 (DB / 日志) |
| 数据源 | `POST` | `/api/v1/datasources/{ds_id}/test` | 测试数据源连通性 (只读校验) |
| 数据源 | `GET` | `/api/v1/datasources` | 查询已配置数据源清单 (脱敏返回) |
| 报错缓存 | `GET` | `/api/v1/error-cache` | 分页查询历史报错与修复经验库 |
| 报错缓存 | `POST` | `/api/v1/error-cache/search` | 调试 ES (BM25) + Qdrant (向量) + RRF 混合检索 |

## 2. 统一错误码

| HTTP 状态码 | 业务错误码 | 含义 | 处理建议 |
|---|---|---|---|
| `400` | `INVALID_PARAM` | 入参格式或业务校验失败 | 检查请求体字段类型与必填项 |
| `400` | `UNSAFE_SQL_BLOCKED` | SQL 校验未通过只读限制 | 仅允许 `SELECT` / `SHOW` / `EXPLAIN` |
| `404` | `PROJECT_NOT_FOUND` | 指定项目 ID 不存在 | 先调用 `/api/v1/projects` 注册 |
| `404` | `DATASOURCE_NOT_FOUND` | 指定数据源 ID 不存在 | 核对 `datasource_ids` 或移除不存在项 |
| `404` | `TASK_NOT_FOUND` | 指定任务 ID 不存在 | 检查 `task_id` |
| `409` | `TASK_ALREADY_RUNNING` | 同一 `task_id` 正在执行中 | 等待当前轮次结束再发起请求 |
| `409` | `TASK_NOT_WAITING_APPROVAL` | 当前任务未处于人工审批挂起态 | 检查任务状态后调用审批接口 |
| `422` | `DATASOURCE_UNREACHABLE` | 外部数据源连接失败 | 检查用户提供的连接串与网络策略 |
| `503` | `SANDBOX_UNAVAILABLE` | Docker 引擎不可用 | 确认 Linux 容器运行环境已启动 |

## 3. 核心接口详述

### 3.1 发起或继续任务流式执行

- **路径**：`POST /api/v1/tasks/chat-stream`
- **响应类型**：`text/event-stream`

#### 入参 (`TaskStreamRequest`)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_id` | `string \| null` | 否 | 传 `null` 创建新任务，传已有 ID 则复用 `thread_id` 多轮对话 |
| `project_id` | `string` | 是 | 目标代码仓库项目 ID |
| `user_query` | `string` | 是 | 研发需求描述或报错现象 |
| `datasource_ids` | `list[string] \| null` | 否 | 本次允许探查的外部数据源 ID 列表；为空时跳过数据库证据收集 |
| `log_path` | `string \| null` | 否 | 可选本地运行日志相对路径（未配置 ES 时用于降级扫描定位） |

请求示例：

```json
{
  "task_id": null,
  "project_id": "proj_order_api",
  "user_query": "结算接口偶发 TypeError，排查原因并修复补充单测",
  "datasource_ids": ["ds_order_ro_mysql"],
  "log_path": "logs/app.log"
}
```

#### 出参 (SSE 事件流)

底层基于 LangGraph `stream_mode=["messages", "updates", "custom"]` 与 `subgraphs=True` 聚合输出，按用途分为三类事件：

1. **Token 与推理流事件 (`messages` 模式)**

| 事件名 | 载荷关键字段 | 含义 |
|---|---|---|
| `reasoning_token` | `content`, `node_name`, `subgraph`, `probe_type`, `run_id` | 推理模型思考过程增量文本 (`reasoning_content`) |
| `token` | `content`, `node_name`, `subgraph`, `probe_type`, `run_id` | 模型正文增量片段（修改规划、补丁生成、修复说明等） |

2. **工具调用事件 (`messages` / `custom` 模式)**

| 事件名 | 载荷关键字段 | 含义 |
|---|---|---|
| `tool_start` | `tool_name`, `args_summary`, `node_name`, `probe_type` | 开始调用探查或沙箱工具（如 SQL 探查、日志检索、缓存检索、容器测试） |
| `tool_end` | `tool_name`, `status`, `duration_ms`, `summary` | 工具执行结束与脱敏摘要 |

3. **状态流转与保活事件 (`updates` / `custom` 模式)**

| 事件名 | 载荷关键字段 | 含义 |
|---|---|---|
| `task_init` | `task_id`, `status`, `active_probes` | 任务初始化，返回本次启用的证据探查类型 |
| `node_status` | `node_name`, `subgraph`, `timestamp` | 节点流转进度通知 |
| `evidence_added` | `evidence_id`, `evidence_type`, `summary` | 探查子图经 `evidence_collector` 签发标准证据 (`db_query` / `log_error` / `source_code` / `error_cache`) |
| `diagnosis_ready` | `report`, `evidence_count` | 只读诊断/问答 (`qa`) 完成取证后输出最终分析报告 |
| `plan_ready` | `plan`, `target_files` | 基于证据生成的修改或创建计划 |
| `sandbox_result` | `test_passed`, `retry_count`, `test_output` | Linux 容器沙箱运行结果 |
| `waiting_approval` | `task_id`, `patch_diff`, `branch_name` | 沙箱测试通过后触发 `interrupt` 等待审批 |
| `task_done` | `task_id`, `status`, `commit_sha`, `cache_archived`, `diagnosis_report` | 流程结束（`qa` 直接完成并附带 `diagnosis_report`；改码分支经审批提交并沉淀缓存） |
| `task_error` | `error_code`, `message` | 运行期异常中断 |
| `heartbeat` | `timestamp` | 容器跑测试或构建耗时期间定时保活，防止长连接超时断开 |

### 3.2 人工审批与唤醒接口

- **JSON 路径**：`POST /api/v1/tasks/{task_id}/approval`（`application/json`，返回 `TaskApprovalResponse`）
- **SSE 流式路径**：`POST /api/v1/tasks/{task_id}/approval-stream`（`text/event-stream`，入参同 `TaskApprovalRequest`，适用于 `rejected` 重跑沙箱时实时推送 `token` / `node_status` / `sandbox_result` / `waiting_approval` 或批准后推送 `task_done`）

#### 入参 (`TaskApprovalRequest`)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `decision` | `"approved" \| "rejected"` | 是 | 批准提交或驳回修改 |
| `feedback` | `string \| null` | 否 | 驳回时的具体修改意见 |
| `commit_message` | `string \| null` | 否 | 自定义提交信息；不传则由模型生成 |

#### 出参 (`TaskApprovalResponse`)

```json
{
  "task_id": "task_20261111_8a3f1c",
  "status": "completed",
  "decision": "approved",
  "branch_name": "feat/fix-coupon-null-check",
  "commit_sha": "7d91e2a",
  "message": "补丁已应用并提交到目标分支"
}
```

### 3.3 任务详情与证据原文接口

- **路径**：`GET /api/v1/tasks/{task_id}`

#### 出参 (`TaskDetailResponse`)

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | `string` | 任务主键兼 `thread_id` |
| `project_id` | `string` | 关联项目 ID |
| `status` | `string` | `running` / `waiting_approval` / `completed` / `failed` |
| `evidences` | `list[EvidenceItem]` | 本次任务收集的全部证据元数据 |
| `patch_records` | `list[PatchItem]` | 各轮补丁 Diff 与沙箱测试记录 |
| `messages` | `list[MessageItem]` | 对话与关键执行日志 |

- **证据原文路径**：`GET /api/v1/tasks/{task_id}/evidences/{ev_id}`
  返回单条证据脱敏截断后的 `raw_snippet`、`query_args` 与采集时间。

### 3.4 项目注册接口

- **路径**：`POST /api/v1/projects`

#### 入参 (`ProjectCreateRequest`)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | `string` | 是 | 项目显示名称 |
| `namespace` | `string \| null` | 否 | 隔离命名空间；不传则由项目目录名加短哈希自动生成 |
| `repo_path` | `string` | 是 | 本地 Git 仓库绝对路径 |
| `test_cmd` | `string` | 否 | 默认沙箱测试命令，如 `pytest -q` |
| `lint_cmd` | `string \| null` | 否 | 可选静态检查命令 |
| `sandbox_image` | `string` | 否 | Linux 测试镜像名，默认 `python:3.12-slim` |

#### 出参

```json
{
  "project_id": "proj_order_api",
  "name": "order-service",
  "namespace": "order-service_8a2f",
  "repo_path": "/repos/order-service",
  "test_cmd": "pytest -q",
  "sandbox_image": "python:3.12-slim"
}
```

### 3.5 用户数据源接入接口

- **路径**：`POST /api/v1/datasources`

#### 入参 (`DatasourceCreateRequest`)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project_id` | `string` | 是 | 所属项目 ID |
| `name` | `string` | 是 | 数据源名称 |
| `ds_type` | `"mysql" \| "postgres" \| "elasticsearch" \| "log_file"` | 是 | 数据源类型 |
| `es_index_name` | `string \| null` | 否 | 用户可选的自定义 ES 日志索引名 (`ds_type=elasticsearch` 时生效，须满足特定日志字段结构) |
| `connection_uri` | `string` | 是 | 连接串或日志配置 (落库前加密) |
| `readonly_verified` | `bool` | 否 | 是否强制以只读权限连接 |

#### 出参

```json
{
  "ds_id": "ds_order_ro_mysql",
  "project_id": "proj_order_api",
  "name": "order-readonly-db",
  "ds_type": "mysql",
  "masked_endpoint": "10.0.1.25:3306/order_db",
  "enabled": true
}
```

### 3.6 报错缓存双路检索接口

- **路径**：`POST /api/v1/error-cache/search`

#### 入参 (`ErrorCacheSearchRequest`)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `namespace` | `string` | 是 | 所属项目隔离命名空间 |
| `query` | `string` | 是 | 报错关键词、异常堆栈或症状描述 |
| `top_k` | `int` | 否 | 融合后返回条数，默认 3 |

#### 出参 (`ErrorCacheSearchResponse`)

```json
{
  "bm25_hits": [{"cache_id": "err_20261104_01", "bm25_score": 8.42}],
  "vector_hits": [{"cache_id": "err_20261104_01", "vector_score": 0.89}],
  "fused_results": [
    {
      "cache_id": "err_20261104_01",
      "error_signature": "TypeError: unsupported operand type(s) for -",
      "root_cause": "coupon_type='RATE' 时 discount_amount 为 NULL",
      "resolution": "按优惠券类型区分折扣计算分支",
      "rrf_score": 0.032787
    }
  ]
}
```

## 4. 接口设计决策与理由

1. **数据源通过 `datasource_ids` 显式可选传入**：前端或不传或传 `[]` 时，服务端只激活日志与源码探查，避免强校验数据库存在。
2. **连接凭证出参强制脱敏**：`GET /api/v1/datasources` 仅返回主机端口与库名，不向前端回传密码与完整连接串。
3. **SSE 事件携带节点与探针来源标识**：`evidence_probe_subgraph` 内多路探针并发执行 LLM 推理，若 `token` 事件不带 `node_name`、`probe_type` 和 `run_id`，前端主消息框会出现多路文本交叉混排；前端依据这些标识按阶段或探针面板分流渲染。
4. **不暴露测试造数接口**：接口层完全不提供类似 `/trigger-fault` 的测试造数入口，测试数据通过独立的 `app/seed/seed_runner.py` 脚本注入，保证 `api` 与 `services` 层纯净。
