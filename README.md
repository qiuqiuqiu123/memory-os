# Memory OS · Agent 记忆操作系统

按《Agent 记忆插件系统设计 v0.3》实现的自托管 P0 + P1 调试版本。提供事件事实日志、Mem0 提取、Qdrant 语义检索、离线同步、Claude Code hooks、Streamable HTTP MCP、记忆注入和中文控制台。

## 快速部署

需要 Docker Engine 与 Docker Compose v2。8GB 测试机建议先启动核心服务；12GB 可按需启动完整 Langfuse 栈，实际可用内存取决于数据量及其他进程。

```bash
cp .env.example .env
# 编辑 .env：设置 API_KEY、POSTGRES_PASSWORD，以及两组模型配置
# LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
# EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL / EMBEDDING_DIMENSIONS
docker compose up -d --build
```

控制台：<http://localhost:8000>，填入 `.env` 中的 `API_KEY`。API 文档：<http://localhost:8000/docs>。服务默认绑定服务器本机，远程调试使用 SSH 转发：

```bash
ssh -L 8000:localhost:8000 your-server
```

只测试事件、直接记忆操作、skill 分析、基础标签注入时不需要配置模型；**自动提取和语义检索必须配置真实模型**。没有模型不会回退到模拟或关键词结果。控制台取消“使用模型自动提取”可直接保存明确事实。

完整 trace 栈：先按 [部署说明](docs/deployment.md) 填好 Langfuse 密钥及数据库/对象存储密码，再执行：

```bash
docker compose --profile langfuse up -d --build
```

Langfuse UI：<http://localhost:3000>。模型服务是 OpenAI 兼容 HTTP API；选择外部服务时，记忆内容/检索问题会发送至配置的模型端点。若内容必须不出内网，请配置内网兼容端点。

## 在线设置与本地 BGE-M3 CPU

登录控制台后进入“模型设置”，分别填写LLM与embedding地址、模型和密钥。保存后API与worker后续请求即时生效。密钥加密持久化、不回显，留空保留原值；仅部署管理员可以修改。首次部署在.env设置独立的`SETTINGS_ENCRYPTION_KEY`并随备份保存。

本机CPU BGE-M3启动：

```bash
# 在.env设置独立MEMORY_OS_BGE_KEY，推荐BGE_SOURCE=modelscope用于国内调试网络
# 默认1024维，内置embedding URL http://bge-m3:8080/v1
docker compose -f docker-compose.yml -f compose.bge.yml up -d --build
```

BGE-M3真实权重首次下载约2.27GB，默认512token输入长度、单进程CPU推理；不需要购买embedding API。LLM提取仍需要在设置页填写GPT服务密钥。详见 [CPU服务说明](embedding_service/README.md) 和 [远端部署说明](docs/remote-deployment.md)。

更换embedding模型或维度时需确认重建，系统创建新集合并后台重索引；不会混用原模型向量。连接测试使用已保存配置，“测试LLM”会产生少量真实调用。

## 已实现的使用路径

- **记忆**：异步 Mem0 提取 / 直接事实写入 / 显式更新 / 软删除 / 标签 / scope 隔离 / 向量检索 / 查询审计。
- **事件**：事务追加、客户端 UUID 幂等、内容冲突返回 409、提交顺序游标、分页同步、全量重建投影和向量重建任务。
- **trace**：直接事件 span 树、错误挂载；Langfuse 原始采集与语义适配分层。适配路径保留语义摘要，原始 span 细节在 Langfuse 查看。
- **分析**：skill 最终状态聚合、成功率/耗时/token/主要错误、证据驱动的保守建议入记忆。累计至少 5 次且成功率低于 80% 时，失败事件触发自动建议任务；控制台可手动生成。
- **注入**：session.start / task.context / skill.invoked / event.pattern，最多 500 tokens，包含包装文本；注入日志与前后统计。样本不足返回证据不足，关联统计不宣称因果，不自动改写 skill。
- **接入**：8 个 MCP 工具、Python SDK SQLite durable outbox、离线本地记忆投影、Claude Code hooks、Langfuse ingestion 故障降级。
- **控制台**：记忆管理、语义检索、trace/skill、注入预览、事件增量和任务状态。API key 保存在当前浏览器会话，断开会清除。

## 接口示例

将 `YOUR_API_KEY` 替换为你自己的 key；scope 建议采用稳定项目名。

```bash
curl http://localhost:8000/api/v1/memories \
  -H 'Authorization: Bearer YOUR_API_KEY' -H 'Content-Type: application/json' \
  -d '{"content":"这个项目使用中文交流","tags":["preference"],"scope":"my-project","infer":false}'

curl http://localhost:8000/api/v1/recall \
  -H 'Authorization: Bearer YOUR_API_KEY' -H 'Content-Type: application/json' \
  -d '{"query":"交流语言偏好","scope":"my-project","top_k":5}'

curl http://localhost:8000/api/v1/inject \
  -H 'Authorization: Bearer YOUR_API_KEY' -H 'Content-Type: application/json' \
  -d '{"trigger":"session.start","scope":"my-project","budget":500}'
```

自动提取使用 `infer:true`，返回 `job_id`；查询 `GET /api/v1/jobs/{job_id}`。`completed` 才表示完成，结果含 `memory_ids`，模型判断没有事实时可为空列表。`retry`/`failed` 表示失败或等待重试；修好配置后 `POST /api/v1/jobs/{job_id}/retry` 可重试终止任务。向量索引异步更新，成功保存不等于此刻已被语义检索命中；`pending_jobs` 包含待处理索引任务。

写入/更新可传客户端 UUID `request_id` 保证网络重试安全。`POST /events` 的 `last_version` 仅为写入确认，**不能用于推进本地 pull cursor**。

## MCP 与客户端

MCP 地址 `http://localhost:8000/mcp/`，必须提供 `Authorization: Bearer YOUR_API_KEY`。支持 Streamable HTTP，不是 stdio。

Claude Code 示例（密钥使用你自己的本地环境配置）：

```bash
claude mcp add --transport http memory-os http://localhost:8000/mcp/ \
  --header 'Authorization: Bearer YOUR_API_KEY'
```

工具：`remember`、`recall`、`forget`、`inject`、`trace_get`、`trace_analyze`、`events_sync`、`job_get`。

自动注入/采集需要额外启用 hooks，仅添加 MCP 不会自动采集所有执行。安装及 Python SDK 用法见 [接入示例](examples/README.md)。默认不记录工具原始输入输出，仅记录脱敏摘要；任务上下文检索会发送用户 prompt 的截取部分至记忆服务。

标签约定：

- `preference`：会话开始注入用户偏好。
- `project`：会话开始注入项目约定。
- `skill:名称`：仅在对应 skill 场景注入经验。
- `recommendation`：分析生成的建议；同时含对应 skill 标签。

`task.context` 使用真实语义检索，需 query；`skill.invoked` 与 `event.pattern` 需要 skill_name。单次超预算记忆会被跳过，`truncated=true` 留痕。tokenizer 为 `cl100k_base`，对其他模型是明确的估算口径。

## 架构与一致性

详见 [架构规格](docs/architecture.md)。核心路径：

```text
客户端 / hooks / MCP → API → PostgreSQL events + projections + durable jobs
                                        ↓ worker
                              Mem0提取 / embedding → Qdrant
Langfuse原始trace → 公共API适配 → 语义事件 → skill分析 → 建议记忆 → 下次注入
```

Mem0 使用独立的临时内存 Qdrant 做每次提取，提取出的事实写成正式事件后再进入持久 Qdrant；不让 Mem0 的内部变更绕过事件日志。当前不跨既有记忆做自动语义合并，修改请使用显式更新。

多用户暂不提供账号管理界面，但凭据可通过 `API_KEYS_JSON` 绑定用户，服务端忽略客户端自报 user_id。默认 API_KEY 绑定 default。所有查询与投影按 user/scope 隔离。`scope` 是同一用户下的项目分区，不是独立授权边界。

软删除不会抹除历史事件中的内容；本版本没有彻底擦除历史/自动归档能力。不要将“forget”理解为合规数据清除。

## 本地开发与验证

Python 3.12：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.lock.txt
# 将.env里的DATABASE_URL指向可达的PostgreSQL，QDRANT_URL指向Qdrant
.venv/bin/uvicorn memory_os.app:app --host 127.0.0.1 --port 8000
# 另开终端
.venv/bin/python -m memory_os.worker
```

PostgreSQL 集成测试使用独立测试数据库；事件日志不可删除，测试用随机用户隔离数据：

```bash
TEST_DATABASE_URL=postgresql://memory:password@localhost:5432/memory_test \
  .venv/bin/python -m pytest -q
```

未设置 TEST_DATABASE_URL 时数据库测试标记 skip，而非用 SQLite 替代 PostgreSQL。集成模块的离线协议测试明确使用 MockTransport；这不证明真实模型和 Langfuse 可用。完整后端验证当前为42项通过，细节见验证记录。

浏览器 smoke：

```bash
NODE_PATH=/path/to/node_modules MEMORY_OS_URL=http://127.0.0.1:8000 \
  MEMORY_OS_API_KEY=YOUR_API_KEY node tests/browser_smoke.cjs
```

配置真实 embedding 且 worker 已处理索引任务后，可加 `MEMORY_OS_TEST_SEMANTIC=1` 验证检索。最终验证结果与未覆盖项见 [验证记录](docs/verification.md)。

## Codex 原生 Hook + MCP 记忆插件

当前接入原版Codex的原生生命周期Hook：用户输入、工具调用/结果、回合完成，SessionStart提供最多400token的项目记忆。插件仅处理本次stdin，先脱敏入本地SQLite队列，再有限上传退出；不修改Codex、不扫描会话日志、不常驻。断网留队列，下次事件或手动flush补传。

云端沿用capture API、增量LLM复盘、证据链接与记忆自动入库与事后剔除。MCP支持recall/inject、明确remember及capture_report补充决策和验证结论，不重复工具轨迹。本机旧日志扫描器已停用；原本地API/数据库容器保持停止，仅SSH隧道常驻。

见[插件安装与跨机器scope说明](clients/codex-plugin/README.md)。macOS/Linux/Windows共享标准库脚本，但原生Hook覆盖取决于客户端版本，真实平台验收边界见[验证记录](docs/verification.md)。旧日志适配器文档只作历史记录，不作为默认接入。

## Codex 一句话安装入口

在 Codex 中输入：

```text
使用 $skill-installer 从 qiuqiuqiu123/memory-os 安装 skills/memory-os-setup，安装完成后直接读取已安装目录的 SKILL.md，按其流程引导我配置这台机器的记忆接入。使用 MEMORY_OS_BASE_URL 和 MEMORY_OS_API_KEY 环境变量；如果我明确选择公网 HTTP 调试，则启用 MEMORY_OS_ALLOW_INSECURE_HTTP=1，不另建密钥文件。
```

安装助手会配置原版 Codex 的原生 Hook、MCP 和记忆操作 skill。需要 Memory OS 服务地址及访问密钥；信任 Hook、重启客户端后再验证实际上传。仓库现已公开；记忆服务的数据仍通过访问密钥隔离。详细步骤见 [插件接入说明](clients/codex-plugin/README.md)。
