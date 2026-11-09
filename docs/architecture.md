# 实现规格与设计修订

## 进程与存储

API 提供 REST、Streamable HTTP MCP、静态控制台和注入业务逻辑；worker 消费 PostgreSQL 任务并轮询 Langfuse。独立 injection-engine/MCP 容器在调试规模没有必要，逻辑组件共用业务 Service，减少重复配置与内存。

PostgreSQL 保存事实事件、记忆/trace/skill/偏好投影、任务和数据库版本。Qdrant 仅保存派生向量及过滤元数据。Langfuse 是独立观测系统，profile 仅控制本机是否同时启动其基础设施，不影响应用集成能力。

## 事件与游标

单行 `event_clock` 在事务内 `FOR UPDATE` 锁定，分配 id、追加事件、更新投影/任务及推进 clock 在同一事务提交。后续写者必须等待当前事务释放锁，因此已可见的高游标前不会再迟到低游标事件；回滚同时回滚计数器。优先保证小规模调试正确性，当前不是高吞吐架构。

幂等键为 `(user_id,event_id)`；相同请求返回原版本，内容不同返回 409。哈希以原始请求计算，服务端规范化 old_content 后仍可用原请求重试。内建触发器拒绝 UPDATE/DELETE/TRUNCATE 事件表；数据库管理员仍能变更触发器，因此不是抵抗管理员篡改的审计系统。

全局版本允许对用户/项目过滤后产生间隔；间隔不是数据丢失。分页返回该过滤器下的实际下一版本。SDK 缓存绑定服务地址/凭据/scope，切换身份必须使用另一缓存文件；推送确认和拉取水位分离。

## Mem0 与可靠任务

记忆输入请求先持久入 extract job，不提前发出 memory.created。Mem0 使用临时 Qdrant 内存集合，不访问正式向量集合；其真实 SDK/LLM 返回的事实列表被转换为确定 ID 的 memory.created 事件。事件与 job completed 在同一事务提交。

Mem0 只承担输入中的事实提取；跨既有记忆的自动合并未开启。0 个事实是合法完成，显式更新/删除独立事件化。

向量任务与记忆事件同事务入库。worker 使用 SKIP LOCKED 领取、5 分钟 lease、指数退避（最多60秒）、最多5次重试，attempt 代数校验阻止过期提取任务提交，失败状态可查并显式重试。单条记忆的向量写入用 PG advisory lock 串行化，并始终重新读取最新投影，因此迟到旧任务不会最终覆盖新向量。查询还用 PG 版本和软删除过滤 Qdrant 命中，代价是索引滞后时暂时少结果。

投影重建受 clock 锁保护，从事件顺序恢复全部业务投影，然后为最新记忆（含删除墓碑）排向量任务，不重新调用提取模型。若 embedding 模型/维度改变，应创建新 Qdrant collection 后重建，避免旧空间混用。现在不提供自动模型空间迁移。

## Langfuse 分层

hooks 优先将 trace/span 发到 Langfuse 公共 ingestion API；失败时 SQLite durable outbox 保存原事件，之后推往核心。适配器定期分页读取 trace 详情，抽取 trace 根、skill 最终态、错误与重试，**不复制原始 span 树**。控制台完整 span 树适用于直接采集/降级路径，常规 Langfuse 路径看语义摘要。

当前单用户调试版每30秒全量分页复查 trace，确保活跃/旧 trace 后补更新不漏；持久修订号 + UUID5 语义快照去重（状态 A→B→A 也可恢复），去重状态与事件同事务提交，skill 按稳定 observation ID upsert。此策略会增加 API 调用，不适合无限 trace 历史；后续应以官方增量接口和重叠扫描窗口替代。

Langfuse 的 user_id 由部署环境固定，不能信任 trace metadata 自报租户；scope 可使用 metadata.memory_os_scope（同一用户内项目划分）。正常采集与故障降级同 trace/span id 对齐；错误摘要可能来自不同路径，统计使用最终 skill 状态而非错误事件条数。未实现单次执行跨源exactly-once观测保证。

## 分析与注入

skill_calls 是稳定调用ID的最终状态投影。分析提供成功率、均值与失败类型；未完成调用不应被解释为一次失败。自动建议使用保守、确定性的统计模板，不调用 LLM 猜测根因。至少5次调用、成功率低于80%时，失败事件触发建议任务；相同统计快照幂等。

session.start 读取 preference/project 标签；skill/event.pattern 只读取对应 skill 标签；task.context 走语义检索。显式检索可对结果加标签过滤。注入文本标记为历史参考，不能提升为当前用户指令；包装计入预算，使用 cl100k_base 统计。

memory.injected 保存 trigger、channel、内容、实际入选ID、token、预算、截断和来源trace。前后对比按事件版本切分，至少各5次执行才提供候选判断；不宣称因果、不自动删除/降权。事件模式注入需通道调用触发，不主动越过客户端推送上下文。

## 边界

首版没有 Kafka、图谱、WebSocket、自动归档、多用户账号管理与生产HA。所有写入请求需要API key，user_id来自凭据。插件离线时可写入持久缓冲、查看已同步本地投影；语义检索和模型提取需要恢复服务器/模型连接。

## 在线模型设置（2026-10-28）

model_settings保存部署级配置的Fernet密文与revision。默认管理员密钥有写权限，其他用户不能读取设置；HTTP响应只含api_key_set，422校验错误不回显input/密钥。API与worker每次业务操作解析最新配置，数据库变更无需重启。主加密密钥来自SETTINGS_ENCRYPTION_KEY（本地开发无配置时退回API_KEY派生），部署与备份须保留独立主密钥。

embedding地址/模型/维度变化或显式reindex会在clock锁事务内创建新collection名并为所有未删除记忆排重建任务。旧在途任务只影响其取得的旧collection；新请求读新配置。旧集合不自动清理。修改key不改变向量空间。

本地CPU服务独立容器，真实BAAI/bge-m3、1024维dense+归一化。只在内部网络接受Bearer key请求，默认最多64条文本、内部batch2、最大512tokens截断，单进程锁串行控制峰值。模型文件固定revision并校验（ModelScope路径），不执行remote code；PyTorch采用CPU wheel。

## Codex capture pipeline (2026-11-01)

本地独立collector增量读取可见JSONL：user/assistant文本、工具call/result、轮次完成/中止。按日志文件身份排除子agent，避免其复用父session ID导致主会话误排除。排除reasoning/system/developer及自动AGENTS/environment包装。offset/source去重/outbox同SQLite事务提交，服务端batch ACK后出队；首次EOF基线不回灌历史。

capture_sessions/batches/events按user+scope隔离保存脱敏原始可见轨迹，不写向量库。调试版直接存PostgreSQL，不依赖未启动的Langfuse。task_complete/turn_aborted或100新增事件触发固定不重叠窗口（<=100事件、50k字符、12k输入tokens），持久job lease+attempt fence约束结果提交。模型使用在线设置，45秒单请求无SDK重试，6000输出tokens；官方DeepSeek禁thinking以避免推理耗尽结构化输出预算。length/content_filter、坏JSON、越界证据引用均失败重试，不能假成功。

复盘、有效候选自动保存、memory.created及job完成同事务，事件metadata保留review/candidate/evidence引用。候选状态区分auto_saved、approved（兼容历史）、rejected。用户可事后剔除记忆，留下删除事件和证据；原始轨迹、复盘建议、可检索记忆分别存储。自动保存标识来源，不代表模型结论已经人工确认。单worker调试模式复盘与现有提取/向量任务共享队列，不声称并发容量。当前没有自动修改代码或自动写全局规则的闭环。

## Native Hook and MCP adapter (2026-11-02, current)

The active Codex adapter is an event-driven stdlib command invoked by native hooks.json. It consumes only a whitelisted stdin payload, never transcript_path or session logs. SessionStart offers bounded cloud recall; UserPromptSubmit/PreToolUse/PostToolUse/Stop persist redacted events to a local SQLite outbox and send bounded batches. SessionEnd submits an empty completion batch so it can schedule outstanding events without generating a metadata-only review after Stop. Delivery is at least once with stable batch IDs, receipt deduplication, lease recovery and acknowledgement validation. No resident agent or collector service is required; offline backlog retries on subsequent hooks or manual flush.

The cloud event/review pipeline now auto-saves validated memory candidates, with after-the-fact removal and exact normalized-content deduplication. Authenticated MCP capture_report is a second adapter for model-reported decisions and verified outcomes. It does not claim exhaustive execution coverage, nor directly write authoritative memories. Hook tools exclude this Memory OS MCP namespace to avoid recursive telemetry. Per-project scopes remain path-based with explicit cross-machine overrides. The previous JSONL watcher is retired from the active local installation.


## 自动记忆与事后剔除（2026-11-09）

默认不再要求逐条审批：后台证据引用校验通过后，在同一事务写入复盘、候选、正式记忆事件和向量任务。跨复盘的完全相同内容按用户/scope及NFKC+空白规范化去重；这是文本去重，不是语义合并、矛盾检测或真实性担保。

剔除保留原始轨迹和复盘证据，追加memory.deleted并标记关联候选rejected。PG即时过滤删除记忆，Qdrant删除异步执行；候选重试或后续相同规范化内容不得自动复活。用户将记忆A修正为B后，历史候选A再次出现会被抑制，保留B；若从旧候选A点击剔除，会提示去记忆库核对B的当前版本后再删除。显式新事实、修正和恢复是独立操作，不由后台模型覆盖删除决定。历史pending候选可用有界backfill批处理转入同一路径，不重复调用LLM。
