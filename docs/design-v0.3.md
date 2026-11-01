# Agent 记忆插件系统设计（云端事件模型版）

> 版本：v0.3（2026-10-27）
> 约束：单用户优先、预留多用户、自托管（self-hosted）
> 核心原则：记忆与执行路径统一为「事件」，状态只是事件的投影；优先借鉴成熟开源项目，不重复造轮子。
>
> **v0.2 变更**：技术基调确认 — 记忆执行后端 **Mem0** + trace 采集/可视化 **Langfuse** + 向量库 **Qdrant**（Mem0 默认向量后端即 Qdrant，与官方 OpenMemory 自托管栈一致）。Langfuse 由「可选增强」升级为 P0 正式组件。
>
> **v0.3 变更**：① Langfuse 确认保留为正式组件，明确「原始层 + 语义层」分层集成（上下游关系、trace_id 关联、adapter 语义抽取、允许短时滞后，详见 §3.8）；② 新增「记忆注入引擎」组件（触发器/组装器/通道，详见 §3.10），补齐「分析反哺 agent」的最后一公里；③ 事件目录新增 `memory.injected`，API 新增 `memory.inject()`，测试用例新增 G 组。

---

## 1. 需求与约束回顾

三大核心功能：

| # | 功能 | 说明 |
|---|------|------|
| F1 | 记忆与检索 | 跨会话记忆用户偏好、历史决策、知识沉淀，语义检索 |
| F2 | 执行路径可视化与分析 | 记录 agent/skill 执行轨迹（工具调用、LLM 调用、成败、耗时、token），可视化并分析，反哺优化 agent/skill |
| F3 | 云端服务 + 多端同步 | 记忆服务化部署，多设备/多 agent 共享一致记忆，增量同步、离线可用 |

硬约束：

- **单用户优先**：初期所有数据归一人，但 schema 与接口必须预留 `user_id`
- **后期支持多用户**：不返工的前提下平滑迁移
- **自托管**：数据不出自己网络，docker-compose 单栈起步

---

## 2. 选型：云端服务版对比

### 2.1 评估维度（针对「云端服务 + 多端同步」）

| 维度 | 含义 |
|------|------|
| 自托管成熟度 | 是否官方提供 server/docker 化部署 |
| 服务架构 | 是否原生 server 形态，还是仅库 |
| 存储后端 | PG/pgvector 是否一等公民 |
| 多租户预留 | 是否内置 user/agent 隔离模型 |
| 事件/历史机制 | 是否有修订历史、时间戳，便于事件化改造 |
| MCP 就绪 | 能否直接暴露给 agent |

### 2.2 候选项目横向对比

| 项目 | 自托管成熟度 | 服务架构 | 存储后端 | 多租户预留 | 事件/历史机制 | MCP | 综合定位 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|------|
| **Mem0** | ★★★★★ 官方 REST API + docker compose + dashboard | Server + 嵌入式库双形态 | Qdrant / PGVector / FAISS / FalkorDB 可插拔 | ★★★★★ 内置 user/agent/session/organization scope | ★★☆ 黑盒管道，无修订历史 | ★★★★★ 官方 MCP | **记忆执行后端首选** |
| **Letta** | ★★★★★ Docker + 外部化 PG | 完整 Server（REST + ADE 管理端） | PostgreSQL + pgvector（全量） | ★★★★★ organizations/users 表 | ★★★★ block_history 修订历史 + messages 全量存储 | ★★★☆ 有 API 生态 | 记忆即服务完整形态，但绑定 agent 运行时，偏重 |
| **Graphiti (Zep)** | ★★★☆ 库形态为主，需自组服务 | 库 + MCP server | Neo4j / FalkorDB 图库 + 向量 | ★★★☆ 需自建 | ★★★★★ bi-temporal 时序图谱天然事件化 | ★★★★★ MCP 1.0 | 图谱层增强候选 |
| **Hindsight** | ★★★★ PG 单库即服务 | 库为主 | PostgreSQL 单库 | ★★☆ 需自建 | ★★★★ retain/recall/reflect 网络模型 | ★★★☆ | 记忆机制借鉴对象 |
| **Cognee** | ★★★ 本地嵌入式或自组服务 | 库 + MCP | SQLite/LanceDB/Ladybug 或服务化 | ★★☆ 需自建 | ★★★ ECL 管道 | ★★★★★ MCP 就绪 | 备选 |
| **Langfuse** | ★★★★★ 官方自托管 | 完整 Server | PostgreSQL + ClickHouse | ★★★★★ 多项目/多用户 | ★★★★★ trace 天然事件化 | ★★★★★ 官方 MCP + Skill | **trace 采集/可视化底座** |

### 2.3 技术基调（已确认）与推荐方案：分层架构，事件核心自建

**技术基调（用户确认）**：记忆执行后端 **Mem0** + trace 采集/可视化 **Langfuse** + 向量库 **Qdrant**。

```
┌─────────────────────────────────────────────────────┐
│  Agent 层：Claude Code / 自研 Agent / 任意 MCP 客户端  │
└───────────────┬──────────────────┬──────────────────┘
                │ MCP / Hooks       │ MCP
┌───────────────▼──────────────────▼──────────────────┐
│  【自建】事件核心服务（本次设计的核心）                 │
│  · 统一事件日志（append-only）                       │
│  · 投影：记忆索引 / trace 树 / skill 统计             │
│  · 多端同步协议（增量游标 + 幂等）                    │
│  · Langfuse adapter（原始 trace → 统一事件规约）      │
└───────┬───────────────────────────────┬────────────┘
        │                               │
┌───────▼────────────┐        ┌─────────▼──────────────┐
│ 记忆执行后端         │        │  Trace 底座（正式引入）  │
│ Mem0 自托管          │        │  Langfuse 自托管       │
│ 向量库 = Qdrant      │        │  hooks/OTel 采集原始    │
│ 元数据 = PostgreSQL  │        │  span + 可视化 UI      │
└────────────────────┘        └────────────────────────┘
```

**选型结论**：

1. **事件核心层：自建**（约 3 个服务，见 §3）。这是差异化核心，没有现成开源项目完全覆盖「记忆 + trace 统一事件 + 多端同步」，但每个子组件都有成熟方案可借鉴（事件日志参照 PostgreSQL 自身序列 + 业界 event sourcing 模式）。
2. **记忆执行后端：Mem0 自托管（已确认）**。理由：
   - 官方提供完整自托管栈（FastAPI + dashboard + API key 认证），最贴合「云端服务」
   - 内置 user/agent/session scope，多用户升级零重构
   - **向量后端选 Qdrant**：Mem0 官方 OpenMemory 栈默认即 Qdrant，配置开箱即用；且 Qdrant 是独立服务，可被事件核心的记忆检索直接复用
   - 官方 MCP，agent 接入成本最低
   - 注意：Mem0 的 add/search 是「LLM 提取 + 向量存储」黑盒，我们的**事件核心包在它外面**（先记事件、再调 Mem0 落库），这样事件模型不受其内部实现约束
3. **Trace 底座：Langfuse 自托管（已确认，P0 即引入）**。定位升级为正式组件而非可选增强：
   - 负责原始 trace 采集的**广度**（Claude Code hooks、OTel 标准）与**深度**（原始 span、token 明细）
   - 事件核心通过 **adapter** 消费 Langfuse 的 trace，**只抽取语义事件**（skill.invoked / trace.error / trace 级元数据）写入事件日志，原始 span 零复制（分层集成，详见 §3.8）
   - 可视化双视图：Langfuse UI 看原始执行细节，自建控制台看 skill 聚合分析
4. **向量库：Qdrant（已确认）**。承担两类职责：
   - Mem0 记忆的语义向量存储与检索（Mem0 vector_store 配置）
   - 事件核心 P1 记忆投影的语义检索（直接调用 Qdrant 查询）
5. **演进路径**：
   - 若记忆需要强关系/时序推理 → 引入 Graphiti 作为第二记忆后端（图谱投影），事件核心不变
   - 若想借鉴「反思/信念更新」机制 → 参考 Hindsight 的 reflect 网络设计

### 2.4 为什么不选某些方案

- **不选 Letta 为底座**：它把「agent 运行时 + 记忆」耦合在一起，作为纯记忆插件太重，且它的 agent 模型与用户自研 Agent（AskCC/Jev 体系）冲突。但它的 **block_history 表设计值得参考**（修订历史的落地方式）。
- **不选 Hindsight 为底座**：模型设计新颖但社区/部署成熟度低于 Mem0，且无多租户；其 reflect 机制作为设计借鉴。
- **不选 Cognee 为底座**：本地嵌入式强但云端服务化/多用户需自建较多，与「自托管 + 多用户」目标契合度低。

---

## 3. 事件模型：统一记忆 + trace

### 3.1 核心思想：一切皆事件（Event Sourcing）

- **记忆的每次变更**（新建/更新/删除/检索）→ 一条事件
- **执行路径的每个步骤**（工具调用、LLM 调用、子 agent、错误、重试）→ 一条事件
- 事件是 **append-only 日志**，全局单调递增 version
- **状态是投影**：可查询的记忆、可展示的 trace 树、skill 统计，全部由事件流重放/增量得到

**为什么统一**：

| 问题 | 事件模型解法 |
|------|------|
| 多端同步 | 端侧只需拉取 `after_version` 之后的增量事件，本地重放，天然增量 |
| 冲突 | append-only 不丢任何写入；状态合并用 LWW/合并策略，历史完整可审计 |
| trace 可视化 | 本质就是「回放该 trace 的事件流」构建树 |
| 记忆溯源 | 每条记忆带 `source_trace_id`，可查「这条记忆来自哪次执行」 |
| 分析反哺 | skill 统计是事件流的聚合投影，失败模式可精确定位 |

### 3.2 统一事件 Schema

```sql
CREATE TABLE events (
  id           BIGSERIAL PRIMARY KEY,          -- 全局单调递增，即同步游标 version
  event_id     UUID NOT NULL UNIQUE,           -- 客户端生成，幂等去重键
  user_id      TEXT NOT NULL DEFAULT 'default',-- 预留多用户，单用户期恒为 'default'
  scope        TEXT NOT NULL,                  -- agent_id / session_id / project，隔离维度
  event_type   TEXT NOT NULL,                  -- 见 §3.3 事件目录
  entity_type  TEXT NOT NULL,                  -- memory / trace / skill
  entity_id    TEXT NOT NULL,                  -- 目标实体 ID
  payload      JSONB NOT NULL,                 -- 结构化负载
  metadata     JSONB NOT NULL DEFAULT '{}',    -- 端信息、耗时、token 等
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 索引
CREATE INDEX idx_events_user_version ON events (user_id, id);
CREATE INDEX idx_events_entity      ON events (entity_type, entity_id, id);
CREATE INDEX idx_events_type        ON events (event_type, created_at);
```

字段说明：

| 字段 | 说明 |
|------|------|
| `id` | 服务端分配的单调游标，多端同步的「水位线」 |
| `event_id` | 客户端生成 UUID，服务端 UNIQUE 去重，网络重试安全 |
| `user_id` | 现在恒为 `default`，多用户时按用户隔离数据 + 同步游标 |
| `scope` | 更细隔离：同一用户下的不同 agent/项目/会话 |
| `payload` | 事件主数据，如记忆内容、工具调用参数摘要、错误信息 |

### 3.3 事件类型目录（初期 13 类）

**记忆域（F1）**：

| event_type | payload 要点 | 用途 |
|------|------|------|
| `memory.created` | `content`, `tags`, `source_trace_id` | 新建记忆 |
| `memory.updated` | `new_content`, `old_content`, `reason` | 更新记忆（保留旧值） |
| `memory.deleted` | `reason` | 软删除 |
| `memory.retrieved` | `query`, `top_k`, `hit_ids` | 检索日志 → 分析记忆利用率 |
| `memory.conflict_resolved` | `conflict_ids`, `strategy`, `result` | 冲突合并记录 |
| `memory.injected` | `trigger`, `content`, `token_count`, `channel`, `context` | 注入记录 → 评估注入质量 |

**执行域（F2）**：

| event_type | payload 要点 | 用途 |
|------|------|------|
| `trace.started` | `task`, `agent_version`, `parent_trace_id` | 执行开始 |
| `trace.finished` | `status`, `total_duration_ms`, `total_tokens` | 执行结束 |
| `trace.span.opened` | `span_type(tool/llm/agent)`, `name`, `parent_span_id`, `input_summary` | 步骤开始 |
| `trace.span.closed` | `status`, `duration_ms`, `tokens`, `output_summary`, `memories_written[]` | 步骤结束，标注产生的记忆 |
| `trace.error` | `error_type`, `message`, `span_id` | 错误详情 |
| `trace.retry` | `span_id`, `attempt`, `delay_ms` | 重试记录 |
| `skill.invoked` | `skill_name`, `span_id`, `status`, `duration_ms` | skill 级规约事件（分析用） |

**系统域**：

| event_type | payload 要点 | 用途 |
|------|------|------|
| `sync.pulled` | `after_version`, `count` | 端侧同步水位记录（可选） |

### 3.4 写路径（多端）

```
端侧（Hooks/SDK/MCP）捕获
      │  生成 event_id (UUID)，本地缓冲（离线可用）
      ▼
POST /api/v1/events  { events: [...], user_id, scope }
      │
      ▼
服务端：按 event_id 去重 → 分配全局 id → 追加日志
      │  事务提交
      ├──▶ 更新投影（记忆索引 / trace 树 / skill 统计）
      ▼
返回 { accepted: n, last_version: v }   ← 端侧更新本地水位线
```

要点：

- **幂等**：重复提交相同 `event_id` 返回已存在（不重复入库）
- **批量**：端侧按批提交（如 50 条/批），减少网络往返
- **离线**：端侧本地 SQLite 缓冲 + 投影，恢复在线后按水位线补推
- **串行分配**：服务端 PG 自增保证 version 全局唯一，并发写入安全

### 3.5 读路径与投影

投影（Projection）是事件流的「物化视图」，异步或同步更新：

| 投影 | 存储 | 服务能力 |
|------|------|------|
| P1 记忆投影 | `memory_items` 元数据在 PostgreSQL，embedding 与语义检索在 **Qdrant** | 语义检索 recall、标签过滤 |
| P2 trace 投影 | `trace_roots` + `trace_spans`（PostgreSQL） | trace 树查询 → 可视化 |
| P3 分析投影 | `skill_stats`（PostgreSQL 聚合） | 成功率/耗时/token/失败模式 → 优化建议 |
| P4 偏好投影 | `user_prefs`（PostgreSQL） | 快捷注入 agent 系统提示 |

存储分工：**PostgreSQL 管结构化事实（事件日志、投影元数据），Qdrant 管语义向量（记忆检索）**，两者通过记忆 ID 关联，无重复存储。Qdrant 同时服务 Mem0 与事件核心，避免两套向量库。

关键设计：**投影可重建**。清空投影表，从事件日志全量重放即可恢复一致状态（事件溯源的黄金法则），这保证「事件是唯一真相，投影只是缓存」。

### 3.6 多端同步协议

```
GET /api/v1/events/sync?user_id=default&after_version=1024&limit=1000
→ { events: [...], next_version: 1124, has_more: true }
```

- 端侧以 `after_version` 为水位线增量拉取，本地重放更新本地投影
- 分页用 `next_version` 继续
- 冲突（单用户阶段主要为同端并发，服务端串行分配即安全；跨端同改一条记忆）：
  - **默认策略 LWW**：按服务端接收顺序，后者覆盖，旧值保留在事件日志
  - 记忆合并：`memory.updated` 携带 old_content，分析端可追溯每次变更
  - 多用户阶段：`user_id` 隔离，各用户独立水位线

### 3.7 trace 与记忆的闭环

```
执行（trace 事件流）
   │  trace.span.closed 携带 memories_written
   ▼
记忆（memory.created 携带 source_trace_id）
   │  下次检索命中 → memory.retrieved 记录
   ▼
分析（P3 skill_stats 聚合 trace 事件）
   │  发现 skill X 反复失败 → 生成「优化建议记忆」
   ▼
agent 下次执行 → 检索到该记忆 → 行为改进
```

关键点：**记忆可以指向 trace，trace 可以指向记忆**，双向可追溯，这是「执行 → 记忆 → 分析 → 优化」闭环的数据基础。

### 3.8 与 Langfuse 的关系（正式组件，分层不耦合）

技术基调已确认：**Langfuse 是 P0 正式组件**，负责原始 trace 的采集与可视化；事件核心与其通过 adapter 规约衔接，不产生硬依赖。

```
Claude Code hooks / OTel SDK
        │  采集原始 span（输入输出、token、耗时、错误）
        ▼
   Langfuse（自托管）
        │  UI 可视化：原始执行细节、token 明细
        │  事件核心 adapter：消费 trace，规约为统一事件
        ▼
   事件核心（统一事件日志）
        │  记忆 + trace 统一投影 + 多端同步
        ▼
   自建控制台：skill 聚合分析 / 优化建议
```

| 能力 | 承载方 | 说明 |
|------|------|------|
| 原始 trace 采集（Claude Code hooks / OTel） | **Langfuse（正式）** | 广度采集，token 明细，原始 span |
| 语义事件（规约后） | 自建事件核心 | 记忆 + trace 统一日志，同步与分析的数据源 |
| 可视化 | **Langfuse UI（原始）+ 自建控制台（skill 分析）** | 双视图 |

集成要点：

- **采集**：Claude Code 通过 hooks（`PostToolUse`/`PreToolUse` 等）上报 span 到 Langfuse；自研 Agent 走 OTel 标准导出
- **规约**：事件核心提供 Langfuse adapter，轮询/订阅 Langfuse API，**只抽取语义事件**（`trace.started` / `skill.invoked` / `trace.error` / `trace.retry` 及 trace 级元数据），原始 span 零复制
- **解耦**：Langfuse 不可用时，hooks 直接降级推送原始事件给事件核心，记忆与同步功能不受影响（Langfuse 只影响原始 trace 的深度分析）

**分层集成原则（为什么两套底层资源不一致不是问题）**：

- **上下游而非双写副本**：Langfuse 存原始 span（观测层，PG + ClickHouse + Redis + MinIO），事件核心存语义事件（分析层，自持 PostgreSQL + Qdrant）。两者是流水线上下游关系，存储模型天然不同，**无需一致、无需同步**
- **trace_id 贯穿**：Langfuse 每条 trace 有唯一 id，事件核心写入 trace 级事件时携带同一 `source_trace_id`，两边数据由此对齐；深挖细节时凭 id 从自建控制台跳转 Langfuse UI
- **单向流动**：数据只从 Langfuse 流向事件核心（adapter 拉取），不反向写；adapter 记录已处理 trace_id，**幂等防重复规约**
- **允许短时滞后**：语义事件是派生数据，adapter 定时/触发式拉取，滞后几十秒级可接受，不追求强一致

### 3.9 API / MCP 接口设计（暴露给 agent）

| 接口 | 方法 | 说明 |
|------|------|------|
| `remember(content, tags?, scope?)` | 记忆写入（F1） |
| `recall(query, filters?, top_k?)` | 语义检索（F1） |
| `forget(id)` | 删除记忆（F1） |
| `inject(context?)` | 注入：按触发上下文返回「应注入的记忆」文本（F1/F2，见 §3.10） |
| `trace.get(trace_id)` | 执行路径树（F2） |
| `trace.analyze(skill?)` | skill 优化分析（F2） |
| `events.sync(after_version)` | 多端同步（F3） |
| `events.subscribe` | 实时推送（WebSocket，可选） |

以上全部以 MCP Server 暴露，Claude Code / Cursor / 自研 Agent 均可直接调用；`inject` 同时可通过 Claude Code hooks（`SessionStart` / `PreToolUse`）自动触发，无需 agent 显式调用。

---

### 3.10 记忆注入引擎（v0.3 新增）

**为什么需要**：Mem0 的 `recall` 是「拉取式」——agent 主动查才给；但记忆系统的价值在于**系统主动把记忆送进 agent 上下文**（「注入式」），否则 agent 不知道「它应该知道什么」。独立记忆服务普遍缺这一环，需要自建。

**核心组件**：触发器 → 组装器 → 通道

```
┌───────────────────────────────────────────────────────┐
│ ① 触发器（何时注入）                                    │
│   session.start   → 注入用户偏好 + 项目历史             │
│   task.context    → 检测任务主题 → 注入相关领域记忆      │
│   skill.invoked   → 注入该 skill 历史经验/失败模式/建议  │
│   event.pattern   → 检测到失败重复 → 主动注入警示        │
└──────────────────────┬────────────────────────────────┘
                       ▼
┌───────────────────────────────────────────────────────┐
│ ② 组装器（注入什么）                                    │
│   按触发上下文从记忆库召回 → 模板组装                   │
│   token 预算（≤500，相关性截断）→ 注入事件记录          │
└──────────────────────┬────────────────────────────────┘
                       ▼
┌───────────────────────────────────────────────────────┐
│ ③ 通道（怎么送进 agent）                                │
│   MCP：inject() 工具（agent 主动/框架调用）             │
│   Claude Code hooks：SessionStart / PreToolUse 自动注入 │
│   自研 SDK：LLM 调用前自动附加（透明）                  │
└───────────────────────────────────────────────────────┘
```

**闭环（补齐 F2「分析反哺 agent」）**：

```
skill_stats 发现 skill X 失败率 40%
   → 生成「优化建议记忆」（memory.created，来源 trace 分析）
   → 下次 skill.invoked 触发注入（memory.injected 记录）
   → agent 按建议调整执行
   → Langfuse 记录新 trace → 验证失败率是否下降
   → 记忆更新（好建议保留，无效建议降权）
```

**注入质量评估**：`memory.injected` 事件记录注入内容、时机、token 数；通过后续 trace 对比「注入后执行是否改善」评估注入有效性，驱动注入策略自我优化（哪些记忆值得注入、何时注入、注入多少）。

**设计要点**：

- **注入与拉取互补**：`recall`（agent 主动查「我知道我要什么」）+ `inject`（系统主动给「你可能需要但没想起来」）
- **skill 级注入是最有价值的一档**：把 F2 分析结果直接作用到执行现场，是「记忆系统真正改变 agent 行为」的支点
- **注入可观测可回退**：每次注入都留痕，效果不佳的注入可调低权重或停用

---

## 4. 存储与部署（自托管）

**技术基调确认后的单机 docker-compose 单栈**：

```yaml
services:
  # —— 事件核心（自建） ——
  postgres:          # PostgreSQL 16：事件日志 + 投影元数据（结构化）
  event-api:         # FastAPI：事件写入 / 同步 / Langfuse adapter / 检索代理
  injection-engine:  # 记忆注入引擎：触发器 / 组装器 / 通道（§3.10）
  dashboard:         # 自建控制台：trace 可视化 + skill 分析
  mcp-server:        # 事件核心的 MCP 出口

  # —— 记忆执行后端（Mem0） ——
  mem0:              # Mem0 自托管服务（vector_store = qdrant）
  qdrant:            # Qdrant：语义向量存储与检索（Mem0 + 事件核心共用）

  # —— Trace 底座（Langfuse，内部含 PostgreSQL + ClickHouse + Redis + MinIO） ——
  langfuse:          # Langfuse web + worker + ingestion（官方 compose 原样跑）
```

- **存储分工**：PostgreSQL（事件核心）与 Qdrant（向量）是自建核心组件；Langfuse 自带一套 PostgreSQL + ClickHouse + Redis + MinIO，**与事件核心的 PostgreSQL 分库/分实例部署**，避免相互干扰
- **认证**：单用户期 API key；多用户期 token 表（每用户一 key）
- **演进**：事件核心 PostgreSQL 外部化为独立实例（RDS/自建），事件表按月分区归档；Qdrant 开启快照备份
- **资源预估（单机）**：事件核心 + Mem0 + Qdrant 约 4–6 GB 内存；Langfuse 全套约 4–8 GB（ClickHouse 是主要消耗）。单机 **16 GB 内存可完整起步**；若资源紧张，Langfuse 可先与核心分机部署
- **容量预估（单用户）**：记忆千级、trace 万级事件/月，单 PG + 单 Qdrant 绰绰有余，无引入 Kafka 等重组件必要

---

## 5. 测试用例设计（TDD，先写测试再实现）

> 约定：每个用例 Given/When/Then 结构，先落测试再开发。

### A. 事件写入与幂等

**T-A1 事件持久化与 version 单调**
- Given 空库
- When 依次提交 3 条事件
- Then 事件全部入库，`id` 为 1,2,3 严格递增

**T-A2 重复提交幂等**
- Given 已提交 event_id=E1 的事件
- When 再次提交相同 event_id=E1
- Then 不重复入库，返回原 version，事件表总数不变

**T-A3 并发写入安全**
- Given 空库
- When 100 个并发请求各写 1 条
- Then 100 条全部入库，version 无重复、无空洞

**T-A4 批量提交**
- Given 客户端缓冲 50 条事件
- When POST /events 批量提交
- Then 全部入库，返回 last_version 为最新游标

### B. 投影正确性

**T-B1 记忆投影最终一致**
- Given 对同一 memory 依次写入 created → updated → deleted 事件
- Then memory_items 投影中该记忆为已删除状态，且历史变更可追溯

**T-B2 trace 树构建**
- Given 一次执行的事件流（1 个 trace.started、1 个 agent span、3 个 tool span、1 个 error）
- Then trace.get 返回的树结构与父子关系正确，error 挂载到对应 span

**T-B3 投影可重建（事件溯源黄金法则）**
- Given 写入 500 条混合事件，投影已更新
- When 清空全部投影表，从事件日志全量重放
- Then 重建后投影与清空前完全一致

### C. 同步与冲突

**T-C1 增量拉取**
- Given 服务端已有 1124 条事件
- When 客户端 after_version=1024 拉取
- Then 仅返回 1025–1124 的事件，next_version=1124，has_more=false

**T-C2 分页游标**
- Given 服务端 2500 条事件
- When limit=1000 分页拉取
- Then 第 3 页返回 2001–2500，has_more=false

**T-C3 跨端冲突 LWW**
- Given 两端同时修改同一记忆 M
- When 两端各自提交 memory.updated
- Then 后者按服务端接收顺序生效，旧值完整保留在事件日志

**T-C4 离线恢复**
- Given 端侧离线产生 20 条事件（本地缓冲）
- When 恢复在线触发同步
- Then 缓冲事件全部推送成功，本地水位线与服务端一致

### D. 检索与记忆质量

**T-D1 语义检索**
- Given 记忆库含「偏好用中文回复」「擅长 Python」等多条
- When recall("回复语言偏好")
- Then 返回「偏好用中文回复」且排位靠前（向量检索用例）

**T-D2 检索日志**
- Given 完成若干次 recall
- Then memory.retrieved 事件均已记录，可按查询/命中分析记忆利用率

### E. 分析与优化

**T-E1 skill 统计聚合**
- Given skill X 本周调用 10 次，8 成功 2 失败
- Then skill_stats[X] 的成功率=80%、平均耗时/token 正确

**T-E2 失败模式定位**
- Given skill X 的 2 次失败均为「工具超时」错误
- Then top_errors 能聚合出「timeout」失败模式（供优化建议）

### F. 多用户隔离（预留）

**T-F1 数据隔离**
- Given user A 与 user B 各有记忆数据
- When 以 A 的凭据查询
- Then 仅返回 A 的数据；同步游标各自独立

### G. 记忆注入（v0.3 新增）

**T-G1 触发器正确性**
- Given 记忆库含 skill X 的失败模式记忆 + 用户偏好记忆
- When 触发 `skill.invoked`（skill=X）注入
- Then 注入内容包含 skill X 的经验/建议，且不包含无关的用户偏好

**T-G2 token 预算截断**
- Given 命中记忆 20 条，总长远超预算
- When 组装器按预算 ≤500 tokens 组装
- Then 注入文本不超预算，按相关性从高到低截断，截断动作留痕

**T-G3 注入事件可追溯**
- Given 完成一次 session.start 注入
- Then 事件日志新增 `memory.injected`（含 trigger、content、token_count、channel），可关联后续执行结果

**T-G4 注入效果评估**
- Given skill X 注入「优化建议」后产生新 trace
- When 对比注入前后执行统计
- Then 能输出注入有效性结论（失败率下降 → 建议保留；无改善 → 建议降权），驱动策略更新

---

## 6. 演进路线

| 阶段 | 内容 | 里程碑 |
|------|------|------|
| **P0 MVP（单用户）** | 事件核心 + 记忆投影（Qdrant）+ trace 投影 + 批量/增量同步 + Claude Code hooks → Langfuse → adapter 规约 + **基础注入**（session.start / task.context）+ MCP 接口 | 可记忆、可检索、可看执行路径、基础记忆注入 |
| **P1 分析闭环** | skill_stats 投影 + 控制台可视化 + 「优化建议自动入记忆」+ **skill 级注入**（skill.invoked 触发，§3.10） | 可发现 skill 问题并注入反哺 |
| **P2 多用户** | 激活 user_id 隔离 + token 认证 + 独立水位线 | 多用户上线 |
| **P3 增强** | 多 agent 框架采集扩展（Cursor/Codex 等）、注入策略自我优化、可选 Graphiti 图谱层、WebSocket 实时同步 | 企业级 |

P0 阶段明确**不引入**：Kafka/消息队列、图谱数据库、多租户。Langfuse 作为整体组件引入（其内部自带的 ClickHouse 属组件依赖，非自建业务组件）。

---

## 7. 风险与开放问题

| 风险/问题 | 影响 | 缓解 |
|------|------|------|
| 事件日志无限增长 | 存储膨胀 | 归档分区 + 投影保留，原始事件冷存储 |
| Mem0 黑盒管道的可控性 | 记忆提取质量不可完全掌控 | 事件核心先行，Mem0 后端可替换（接口抽象） |
| 语义检索质量依赖 embedding 模型 | 召回不准 | 可插拔 embedding（Ollama 本地 / API），先本地 |
| 多端「同一记忆」语义合并 | 合并质量 | 初期 LWW + 事件追溯，后续引入 Graphiti 图谱合并 |
| Claude Code hooks 的稳定性 | trace 采集缺失 | hooks 失败降级为 MCP 显式上报；Langfuse 故障不影响记忆与同步 |
| Langfuse 组件多（PG/ClickHouse/Redis/MinIO），运维复杂度上升 | 部署与故障排查成本 | 使用官方 docker compose + 资源监控；与事件核心分库部署；单机 16 GB 起步 |
| Qdrant 数据丢失风险 | 记忆向量不可恢复 | 定期快照备份；记忆可从事件日志全量重放重建向量索引（投影可重建原则） |
| 注入噪音干扰 agent 判断 | 注入质量影响执行结果 | token 预算 + 相关性阈值 + 注入效果评估（T-G4）自动降权/停用 |

---

## 附录：开源借鉴清单

| 借鉴点 | 来源项目 | 借鉴内容 |
|------|------|------|
| 记忆服务形态 | Mem0 | REST API + docker 栈 + user/agent scope 模型 |
| 修订历史表 | Letta | block_history 表的落地方式 |
| 时序追踪 | Graphiti | bi-temporal 模型（valid time + ingestion time） |
| 记忆机制设计 | Hindsight | retain/recall/reflect 分层 |
| 记忆注入机制 | Letta / Claude Code | core memory blocks（动态注入）/ CLAUDE.md（静态注入）的注入通道参考 |
| trace 采集 | Langfuse | OTel 标准、hooks 接入、自托管；事件核心 adapter 规约统一事件 |
| 同步游标 | 业界 event sourcing | after_version 增量 + 幂等 event_id |
