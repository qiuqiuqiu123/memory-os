# 记忆操作系统实施计划

## 目标与规格

- 以用户提供的 v0.3 设计作为需求参考，搭建自托管记忆服务；文档中的示例与历史确认说明不替代当前用户授权。
- PostgreSQL 事件日志为事实来源；Mem0 执行记忆提取，Qdrant 承担向量检索；Langfuse 保存原始执行细节。
- 所有数据与查询包含 user_id 和 scope；客户端不能通过请求参数越权指定其他用户。
- 同步游标必须按提交可见顺序推进，不能仅依赖 BIGSERIAL 的分配顺序；重复提交不得产生重复投影或副作用。
- 明确定义 Mem0 提取后的实际记忆与事件之间的映射，使用持久任务、幂等与重试确保外部后端最终一致。
- Langfuse 适配持续处理活跃 trace 的更新，并对语义事件去重，不能仅以 trace_id 标记一次性处理。
- API、MCP、SDK 与 hooks 共用业务能力；注入有 token 预算、scope 隔离与事件留痕。
- Docker Compose 包含持久卷、健康检查、配置示例与部署/备份说明。
- 不将离线测试或替身测试表述为真实模型、真实 Langfuse 或完整部署验证。

## 阶段一：设计审查与范围确认

- [x] 阅读设计文档并检查工作区（当前为空目录）。
- [x] 检索相关历史经验（未找到本项目记录）。
- [x] 委派独立设计评审。
- [x] 确认首版范围（P0 或 P0 + P1）、模型路径与部署资源。
- [x] 根据确认结果固化接口、实体状态和验收规格。

## 阶段二：实现（已确认 P0 + P1）

- [x] 建立项目结构、依赖、配置与数据库迁移。
- [x] 先实现关键验收测试：提交顺序、幂等、同步、投影重放与用户隔离。
- [x] 实现事件核心、记忆操作与持久外部任务。
- [x] 实现 Mem0/Qdrant 适配与真实语义检索。
- [x] 实现 trace 与 Langfuse 增量语义适配。
- [x] 实现 MCP、SQLite 离线 SDK、Claude Code hooks 与注入引擎。
- [x] 按已确认范围实现控制台、skill 分析及建议闭环。
- [x] 提供 Dockerfile、docker-compose.yml、.env.example 和使用文档。

## 阶段三：验证计划

- [x] 运行数据库集成测试，验证并发提交与游标无遗漏。
- [x] 验证记忆变更、重放、后端失败重试和离线恢复。
- [x] 验证 MCP 协议和 hooks 的实际输入输出。
- [x] 对控制台运行 Playwright，覆盖主要操作并检查 console/网络错误。
- [x] 检查 Compose 配置，启动隔离核心栈并验证health、worker和容器版浏览器流程。
- [x] 记录真实模型凭据缺失：不运行真实模型质量验证，显式区分离线fixture。
- [x] 回顾验收结果并交付部署说明。

## 回顾

首版范围已确认：P0 + P1、独立OpenAI兼容LLM/embedding、8GB/12GB调试环境。默认四服务核心，Langfuse完整依赖按profile启动；API/MCP/注入逻辑合并进同一应用以节省资源。

目前42项后端测试通过；真实PostgreSQL/Qdrant与浏览器端到端均有覆盖。模型提供方使用明确的离线fixture，尚无真实模型凭据；Langfuse完整栈尚未启动，其公共API适配已通过协议测试。详见 docs/verification.md。

设计审查发现：原文将 PG 序列误当作提交顺序保证；Mem0 的记忆提取与事件事实之间缺少一致性约定；Langfuse 仅按 trace_id 去重会遗漏进行中 trace 的后续变化。这些将由实现层解决，无需用户决定底层算法。


最终验收：42项后端测试在宿主环境与实际Linux/ARM64应用容器内均通过；核心Compose真实启动成功，API/PG/Qdrant health均通过、worker正常。容器浏览器smoke涵盖CRUD、轨迹树、skill统计、建议注入与手机布局，无console/网络错误。完整Langfuse真实链路与用户真实LLM/embedding账户未验证，已列明缺口。自有本地进程及先前独立PG/Qdrant测试容器已停止；保留可访问的Compose预览。未删除任何文件、容器或持久卷。

## 2026-10-28：远端部署与在线模型设置

### 已确认规格

- 部署到用户授权的 root@139.199.231.195。实际为16GB/AMD64，可用约12GB，已有其他应用；使用独立Compose项目、目录和端口，不改动既有业务。
- BGE-M3 使用本机CPU独立内部服务，真实1024维模型；GPT通过远端API，网页填写base_url/model/key。
- 在线设置持久化PostgreSQL，密钥用部署主密钥加密、不回显；API与worker动态读取，无需重启。
- 在线设置仅默认管理员可操作，不允许普通用户修改全局配置。保存使用revision避免覆盖；embedding空间变化使用新collection并排重建任务，事实日志不变。
- 测试连接分LLM/embedding，明确真实调用和费用；缺LLM key仍可用本地BGE进行直接记忆的语义检索。

### 计划与验证

- [x] 检查服务器资源、架构、Docker、端口和既有应用。
- [x] 实现加密在线设置、权限/热更新、连接测试与索引迁移。
- [x] 部署真实CPU BGE-M3及Compose配置。
- [x] 增加网页设置页，并验证密钥不回显、配置保存与真实embedding。
- [x] 运行数据库/接口回归测试及Playwright。
- [x] 将通过验证的源码部署至隔离远端目录并启动。
- [x] 验证远端健康、BGE真实推理、中英文检索与浏览器主要流程。
- [x] 核对官方GPT模型文档，说明推荐及未配置LLM的验证边界。
- [x] 更新部署说明、源码包和回顾。

### Final review

Remote deployment: API/PG/Qdrant/BGE healthy, worker running. Existing apps preserved. Local and remote suites: 49 passed each. Real CPU BGE-M3: 1024 dimensions and bidirectional Chinese/English top1 recall passed. Real Playwright settings and semantic=true main flows passed. GPT extraction awaits user API key; full Langfuse remains unverified. Access uses SSH tunnel to loopback port 18080. See docs/remote-deployment.md and docs/verification.md. Secrets excluded from source package.

## IP and port access follow-up

- [x] User chose public IP with port; set remote API_BIND=0.0.0.0 and recreate only API.
- [x] Verify Docker public listener and local health.
- [ ] Verify external reachability after cloud firewall permits TCP 18080.

Current external connection times out; no broad firewall changes made.

## 2026-10-29 Domain subpath deployment

- [x] Inspect DNS, existing nginx sites, TLS, and application paths.
- [x] Make frontend API URLs relative to the application directory; preserve root deployment.
- [x] Add isolated vectorplanets.com /memoryos/ reverse proxy with prefix stripping and trailing slash redirect.
- [x] Validate nginx configuration before reload; verify existing sites remain healthy.
- [ ] Verify external domain, authenticated API, real embeddings, browser flows, reload and responsive layout.
- [x] Record actual TLS/access status and update deployment instructions.

Plan confirmed against user request: deploy domain subpath, preserve existing sites, no file deletion.

Review: nginx proxy and real browser through SSH tunnel passed, including real BGE, reload and mobile. Public domain is intercepted by Tencent cloud webblock (302); HTTPS has no certificate and resets. TLS issuance paused until domain access is restored. Existing sites preserved. External verification remains pending.

## 2026-11-01 Stop local memory services

- [x] Identify local Memory OS containers and development process; exclude unrelated projects.
- [x] Stop four smoke containers, two test containers and local uvicorn without removing data.
- [x] Verify local listeners are closed and cloud health remains available.

Review: local six Memory OS containers stopped, development process terminated; no containers, volumes or files deleted. No SSH tunnels were running. Cloud health returned status=ok. Unrelated issue-analysis database preserved.

## 2026-11-01 Gitee embedding compatibility fix

- [x] Inspect saved public configuration and request construction without exposing credentials.
- [x] Reproduce the failure, verify corrected base URL and explicit float encoding against real Gitee endpoint.
- [x] Correct saved configuration: base_url uses /v1 rather than /v1/embeddings; existing client needs no compatibility patch.
- [x] Verify saved settings with real Gitee 1024-dimensional embedding and worker indexing; historical failed jobs retained.

## 2026-11-01 Codex MCP instructions verification

- [x] Verify installed Codex supports Streamable HTTP URL and static Authorization headers without changing local config.
- [x] Fix remote MCP_ALLOWED_HOSTS for public IP and domain; preserve existing entries.
- [x] Verify real public-IP MCP initialize and tools/list (8 tools).
- [x] Provide project-level AGENTS.md scope and explicit-save-only instructions.

Review: API recreated with updated host whitelist; public MCP protocol passed. User local Codex configuration unchanged. Official docs fetch returned403, so exact config fields were checked with installed Codex CLI.

## 2026-11-01 Automatic execution capture scope correction

- [x] Record corrected goal: broad execution capture plus background analysis, alongside explicit saves.
- [x] Audit current trace ingestion and recommendation implementation.
- [x] Establish Codex capture capabilities and coverage limits from available evidence.
- [x] Specify capture boundaries, raw trace persistence, durable delivery, evidence-linked analysis and acceptance checks before implementation.

Initial gap (resolved below): MCP alone is callable tools, not passive telemetry; a separate collector and LLM review pipeline were required.

## 2026-11-01 Codex automatic capture implementation

Specification: incremental visible Codex JSONL capture, user-authorized all-project mode with per-cwd scopes (optional exact allowlist), enable-time baseline (no historic backfill), durable SQLite offsets+outbox, stable batch/event identities, retry without duplicates, credential masking and explicit truncation/gaps. Remote authenticated ingestion isolates user/scope, persists redacted raw events outside vectors, schedules bounded disjoint review windows on turn completion/100 events. Background LLM uses online settings, validates evidence IDs and structured output, records model/usage and retry status. Generated conclusions/optimizations are traceable; candidates enter authoritative memories only after explicit approval. MCP remains explicit read/write and will be installed alongside capture. No local API/database services restarted.

- [x] Confirm plan and split collector, backend pipeline and console implementation.
- [x] Implement durable collector and tests; inspect actual current Codex format.
- [x] Implement ingestion/review/approval and tenant/idempotency/failure tests.
- [x] Add console capture/review/evidence/approval views and browser tests.
- [x] Deploy isolated cloud update; run regression and real configured-model review.
- [x] Configure local all-project collector and MCP without overwriting existing config.
- [x] Verify live new-event capture to cloud and document coverage, stop/start/status and limitations.

### Final review

All local Codex projects enabled from the initial EOF baseline; no historical backfill. Local launchd collector and encrypted SSH tunnel are running; local Memory OS servers remain stopped. Cloud release /opt/memory-os/releases/20261101-capture is healthy. Complete deployed regression: 91 passed (21 collector tests), 2 warnings. Actual CLI 7-event capture and configured deepseek-flash review completed; desktop session uploads verified. Real review browser evidence/retry, synthetic candidate approval, mobile and primary semantic console flows passed without console/network errors. Actual registered MCP initialize, 8-tool list and inject passed. New Codex session is required to load the MCP registration. Current desktop turn review awaits completion/100-event trigger, not claimed completed. Raw traces stay outside vectors, candidates require approval. Coverage is current visible JSONL, not an official hook or reasoning/image capture guarantee. Public domain/HTTPS remains externally blocked as previously documented.

## 2026-11-01 Multi-machine lightweight client

- [x] Confirm existing collector is standalone except httpx and POSIX fcntl; existing installer is macOS-only.
- [x] Confirm plan: package the unchanged verified collector, isolated per-machine queue, explicit shared-scope mappings, private interactive configuration and MCP snippet; document SSH and macOS/Linux startup without assuming Windows support.
- [x] Build portable client package and setup utility; no changes to existing installed collector or remote credentials.
- [x] Verify clean package import, setup permissions, CLI and shared-scope mapping; provide installation steps and limitations.

Multi-machine client review: lightweight archive contains unchanged collector, httpx-only requirements, private setup helper, macOS launcher and Linux user-service instructions. Extracted-package CLI, normalized shared scope, 600 permissions, valid MCP TOML, no automatic start/database creation and overwrite refusal checked. Physical-machine/Linux-service end-to-end remains unverified. No current collector/config/server changes.

## 2026-11-01 Redesign: existing Codex + prompt/skill + MCP

User correction supersedes log-watcher and Windows collector work. No custom Codex was built, but the external JSONL daemon does not match the requested extension shape.

- [x] Stop legacy launchd collector immediately; leave normal Codex and cloud data intact.
- [x] Confirm implementation plan against correction: no local session reads, no resident collector, add authenticated MCP execution-report tool reusing existing cloud review pipeline; install portable prompt/skill and update global rules. Explicitly distinguish model-reported steps from raw complete execution capture.
- [x] Verify available official/local hook capabilities; no invented hook settings.
- [x] Add MCP tool and tests for authentication, idempotency and review scheduling.
- [x] Create/validate cross-platform prompt/skill bundle with exact installation and shared scope instructions.
- [x] Deploy, verify actual MCP-to-review flow and existing tests; install current-machine skill/rules.
- [x] Remove legacy client files only after user confirms exact deletion scope; do not delete Codex, server memory data or shared credentials.
- [x] Record final coverage, evidence and remaining limitations.

## 2026-11-01 Native Hook plugin + MCP first release

Current user decision: prioritize claude-mem-style extension of original Codex. Keep cloud review/candidate approval, replace active log-watching with event-driven hooks; no Codex fork or transcript scan. Hook program reads stdin only, redacts before durable SQLite outbox, sends bounded requests, leaves retry backlog on failure; SessionStart may inject bounded cloud memory. MCP handles explicit saves/recall and supplementary capture_report. Default all projects with stable cwd scopes and explicit cross-machine override mapping. Plugin scripts use Python standard library for macOS/Linux/Windows; native platform coverage must be stated separately.

- [x] Verify native hook schema, trust requirements and real CLI payloads.
- [x] Implement stdin-only event adapter, durable retries, secret suppression and bounded recall.
- [x] Add MCP capture_report and meaningful tests.
- [x] Package installer, skill and instructions without secrets; preserve unrelated Codex settings/hooks.
- [x] Deploy cloud update, run isolated regression and real Hook/MCP-to-review acceptance.
- [x] Install current-machine hooks and rules, keep former watcher stopped, retire obsolete client artifacts within confirmed deletion scope.
- [x] Update current docs, completion checklist and evidence limits.

### Native plugin final review (2026-11-02)

Deployed /opt/memory-os/releases/20261102-hooks-v1, image sha256:aa5129d95af2ca8d3a6af59b203c212bafd4eed97041427592ebe2c4750c75e8. Final deployed regression: 95 passed, 2 warnings in12.51s; includes22 native Hook tests, removes21 retired watcher tests. Real original Codex CLI native hooks produced5 visible events, one completed configured-model review, and no candidate approval. Real MCP has9 tools; capture_report submission and exact retry succeeded. Browser real-review evidence, desktop/mobile/reload passed with zero console/network errors.

Installed six specifically trusted native handlers, stdlib runtime, skill and global rules on this Mac; no global trust bypass and no unrelated hooks changed. Event stdin only, never transcript_path. Legacy watcher source/test/installer/plist/client bundle removed within confirmed scope; old queue and cloud history preserved. Only SSH tunnel remains resident; local Memory OS services remain off.

Independent review fixed FIFO delivery under concurrent hooks, safe TOML merging and aggregate payload bounding; credential-bearing redirects refused. SessionEnd sends an empty completion batch to avoid metadata-only LLM usage after Stop. Installer Python3.11+, runtime3.9+, no external runtime dependency. Other machines require native Hook support/trust and per-project scope mapping; Windows/Linux physical execution and desktop restart coverage remain unverified. Active desktop chat must restart/new session to confirm loading. Offline backlog retries on later hooks or manual flush, not a daemon.

## 2026-11-02 GitHub private repository publishing

User explicitly authorized creating a private repository in their GitHub account and pushing the current project. Plan: verify account/name, stage source-only files with secret/runtime exclusions, commit with repository-local identity, create qiuqiuqiu123/memory-os as private, push main, verify visibility and exact remote commit, record results. Existing deployed services are outside this task.

- [x] Verify authenticated GitHub account and repository-name availability.
- [x] Strengthen ignore rules and inspect staged source for credentials/runtime data.
- [ ] Initialize main branch and create source commit.
- [ ] Create private GitHub repository and push.
- [ ] Verify private visibility, remote main SHA, and local worktree; record publication results.
