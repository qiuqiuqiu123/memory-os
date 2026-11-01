# 验证记录

日期：2026-10-27。后端完整测试集：**42 passed**。此文件区分真实基础设施、离线模型协议与尚未验证的外部服务。

## 已执行

- PostgreSQL 16 真实实例：事件幂等、UUID 内容冲突、100 并发写入、提交可见顺序、回滚后的版本计数、批量原子性、分页同步、LWW、old_content、用户/scope 隔离、不可变事件表、投影重建。
- 注入/分析：真实数据库上的标签/skill 隔离、预算包含模板、截断留痕、注入可追溯、终态调用统计、样本不足不宣称有效、Langfuse 状态 A→B→A 的持久修订。
- 可靠任务：真实 PostgreSQL，Mem0 返回结果用明确离线 fixture。验证事件/投影/job 一起提交、零事实、失败重试、第五次终止、attempt fencing、非法事实全事务回滚。
- Qdrant v1.15.5 真实 HTTP 服务：租户/scope/标签过滤、删除、过期版本过滤。向量是已知3维离线fixture，用于验证存储与过滤，不能证明真实模型召回质量。
- Mem0 2.2.1 真实 SDK 构造及真实 Memory.add + 内存Qdrant完整管道；模型调用使用明确离线 transport/fixture。检查不同 LLM/embedding 配置、成功提取、合法零事实、坏JSON、embedding失败和缺配置错误。
- SDK自动注入：真实OpenAI SDK + 离线HTTP transports验证预算、包装、调用顺序和失败传播，不修改原消息。
- SDK/hook 真实 HTTP 端到端：SQLite 本地事实写入、push/pull、客户端游标、SessionStart 注入均通过；不涉及真实模型调用。
- MCP 真实 SDK HTTP 协议：Bearer 认证、tools/list、tools/call remember 均通过。
- 中文控制台：真实 Chromium + 真实应用 HTTP API，无浏览器请求mock。覆盖记忆直接保存/编辑/取消删除/确认删除、500以内预算、事件读取、scope、trace父子树/错误、技能10次调用80%成功率、生成建议与skill注入、评估样本展示、手机布局、断开清除key。执行轨迹是经真实事件API提交的手工测试样例，不是假称真实agent运行。console/page errors、requestfailed、HTTP错误均为0。
- 核心与完整 Langfuse profile：`docker compose --env-file .env.example [--profile langfuse] config --quiet` 均通过。
- Python 模块静态格式与基础lint通过。

## 验证命令

```bash
TEST_DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/TEST_DB \
QDRANT_TEST_URL=http://HOST:6333 .venv/bin/python -m pytest -q

NODE_PATH=/path/to/node_modules MEMORY_OS_URL=http://127.0.0.1:8000 \
MEMORY_OS_API_KEY=YOUR_KEY node tests/browser_smoke.cjs
```

## 未覆盖

- 未提供真实模型凭据，未验证付费LLM提取、实际embedding模型维度/兼容性和语义召回质量；浏览器测试 `semantic:false`。不能把离线fixture或真实Qdrant验证当作这一项通过。
- 未启动完整 Langfuse 依赖栈；公共API适配与 ingestion 用离线协议测试验证，尚未做真实 Langfuse ingestion → worker → adapter 全链路。
- Claude Code hooks 的输入/输出协议、采集降级和真实服务注入已测试，但没有在实际 Claude Code 会话中启用用户hook配置。未验证其UI实际接收额外上下文。
- 本机为 ARM64 Docker 环境；AMD64/Linux服务器与8GB/12GB实机容量未作压测。部署资源建议是调试配置建议，不是负载容量承诺。

## 构建与运行

应用镜像构建成功。独立项目 `memory-os-smoke` 的postgres16.11、Qdrant1.15.5、API和worker均已启动；前三项healthcheck通过，worker进程正常。容器内独立测试数据库运行完整测试：**42 passed**。真实浏览器对容器API（127.0.0.1:18000）再次运行全套smoke通过，semantic=false，console/请求失败/HTTP错误均为0。

少量调试数据下四容器内存快照约433MiB（API178MiB、worker129MiB、PG38MiB、Qdrant88MiB），不含Docker虚拟机、模型服务和Langfuse，也不代表负载容量。

Docker Hub曾多次返回EOF；基镜像从镜像站拉取后保留官方名，Python依赖使用公开镜像源加速，不修改默认官方依赖源。首次构建约15分钟，后续依赖缓存命中时源码变更镜像构建不到1秒。

容器内测试有只读应用目录导致的pytest缓存warning；第三方SDK还有弃用/临时Qdrant索引warning，均不影响42项用例通过。没有把容器内运行当作AMD64实机验证。

## 2026-10-28：在线模型设置与远端部署进展

本节追加当日结果，保留上方2026-10-27的历史记录。更新后的本地完整测试集为 **49 passed**。模型调用仍通过明确标记的离线 fixture/transport 验证；不代表真实GPT账户或BGE模型质量通过。

已验证的新增能力：

- 真实PostgreSQL上的在线模型配置持久化、密钥加密、GET不回显、空密钥保留、revision并发冲突、管理员权限及配置热更新。
- 设置接口422校验失败不返回Pydantic原始input/ctx，缺失embedding维度和超长密钥都不会在响应中回显密钥。
- 真实Mem0 SDK消费每次读取的动态LLM/embedding地址、模型、密钥和维度；无需重启，密钥不进入返回结果或测试日志。提供商调用为离线fixture。
- 真实PostgreSQL + Qdrant验证同一长期Semantic实例切换embedding空间和向量集合，旧向量与新检索隔离，并产生持久重建任务；向量为固定测试向量，不代表真实语义召回质量。

远端为用户授权的 `139.199.231.195`，已核对8核/16GB/AMD64资源和既有服务边界。部署使用独立 `/opt/memory-os/releases/20261028`、Compose项目 `memory-os`、API回环端口18080和权限600的独立shared配置。远端已校验模型目录通过 `/opt/memory-os/shared/compose.host.yml` 绑定；运维命令须同时加载主Compose、BGE overlay和该host overlay，见 `remote-deployment.md`。BGE镜像已构建，API、PostgreSQL、Qdrant、BGE健康检查通过，worker进程运行。

当前验证边界：

- **真实BGE推理通过**：CPU输出1024维归一化向量；3条输入0.507秒，64条输入9.028秒，均为真实模型输出。英文问题召回中文语言偏好、中文问题召回英文Python/FastAPI技术栈，两个样例top1正确，查询约0.33秒。这是少量功能验证，不是大样本质量评估。
- **GPT待凭据**：尚无真实GPT API key，未验证LLM连接、Mem0真实事实提取、账户模型权限或实际费用。建议配置模型为 `gpt-4.1-mini`，仍需用户填入兼容服务地址与密钥后执行真实验证。
- **Langfuse未验证**：远端未启动完整Langfuse栈；此前协议测试不等于真实 ingestion → adapter → 语义事件全链路。
- 本地与远端AMD64容器完整测试均为49项通过。健康检查不构成8GB/12GB容量压测结论。

远端真实Chromium验证：模型设置保存、空密钥保留、重建确认、真实BGE连接（1024维）、缺LLM密钥明确失败均通过。控制台完整smoke启用semantic=true，覆盖记忆CRUD/异步索引/真实语义召回/轨迹树/skill建议与注入/手机布局，console错误、失败请求和HTTP错误均为0；轨迹为明确的手工事件样例。

远端运行快照：五容器合计约1.22GiB，其中BGE约749MiB；BGE cgroup历史峰值约778MiB，限额4GiB，OOM=false。该数据为少量调试流量快照，不代表并发容量。服务器既有业务容器持续运行7至8天，未重启。

模型推荐依据：当前Mem0 2.2.1调用参数与gpt-4.1-mini兼容，适合事实提取。gpt-5-mini默认参数需要额外适配，首版不作为默认值。官方模型文档访问返回403，未据此声称最新型号或价格。

Final remote regression: **49 passed in 8.07s** on AMD64, matching local 49 passed. Earlier environment-dependent fixtures now use a deterministic baseline and an explicit embedding outage fixture. Third-party warnings only. Final settings browser test with real BGE passed after image update. Business vector/recommend jobs are all completed with no failed or pending jobs.

Application image: `sha256:c0d2b9ebb57dcbc4c916475b7618a840773d07b7d1f46b3ec3691fefe68da87b`.

## 2026-10-29：域名子路径

前端API从绝对 `/api/v1` 改为相对应用目录，远端镜像已更新。新增域名独立Nginx配置，strip `/memoryos/` 前缀、308规范尾斜线；nginx -t通过，既有IP/ego站点200，Memory OS全部容器持续健康。通过SSH隧道访问真实Nginx并保留域名Host的Chromium验证通过：认证API、全部导航、设置、真实BGE、刷新、手机布局；console/page errors、requestfailed、HTTP错误均0。截图 `output/playwright/domain-subpath-settings.png` 和 `domain-subpath-mobile.png`。

公网域名仍302到腾讯备案拦截页，不计为外部端到端通过。HTTPS reset且无可用证书，证书签发已暂停。解除备案/接入拦截后需继续外网验证及TLS配置。

## 2026-11-01：Codex自动采集与真实后台复盘

最终部署到 `/opt/memory-os/releases/20261101-capture`；Compose项目与shared配置/持久卷保持同一套。应用镜像sha256:b3e739ac1806667fa6f4ed2482608a21ebc427559560efb62cb00711a6a71288。远端隔离PG完整回归 **91 passed, 2 warnings in 11.60s**，含21项collector故障/性能/父子共享sessionID测试。模型部分离线fixtures与真实模型验收分开记录。

真实模型验收：明确手工验收轨迹5事件经真实配置deepseek-flash复盘，1310输出tokens；Playwright验证证据链接、优化建议、候选批准及手机布局，console/page错误、失败请求、HTTP错误均0。手工样例不冒充真实业务轨迹。

真实Codex验收：执行独立Codex CLI只读命令，实际输出memory-os-capture-live-check、exit0（系统cache权限警告也被如实记录）；collector从原生JSONL自动上传7条用户/assistant/工具/完成事件，服务端session a485ebcf-3605-52fb-8300-e44cdcda175d。真实LLM复盘在修复DeepSeek默认thinking耗尽输出预算后成功；浏览器操作“重试复盘”到结果成功，未批准该真实会话的候选，错误均0。CLI/desktop共享ID子日志污染问题已有回归测试，当前桌面主会话亦实际上传新增事件；开启后采集队列pending0/无错误。

旧控制台browser_smoke启用semantic=true再次通过：真实Gitee embedding、记忆CRUD、事件、轨迹树、skill建议注入、手机布局，错误0。当前云端网页LLM连接实测成功，Gitee BGE1024维实测成功；不代表完整Langfuse ingestion已验证。

本机用户级launchd采集器与SSH隧道18082已启用，权限600专用非管理员凭据，所有项目按cwd独立scope，mem-project保留旧scope。本地Memory OS API/PG/Qdrant/worker/test容器未重启（验证中曾短暂启动测试PG，已恢复停止），其他本地项目数据库保留。未删除任何文件或持久卷。当前实现覆盖本机已验证的JSONL格式，不保证未落盘记录、图像/音频和未来客户端版本。

最终补充：routing_metadata_v1恢复基线cwd且不重放历史；实际CLI复盘长证据ID页面完成1440px/390px布局、证据定位和刷新验证，无横向溢出、console或网络错误。实际Codex配置的MCP连接、8工具枚举和inject调用通过。

## 2026-11-02 原生Hook与MCP（当前版本）

当前release为 `/opt/memory-os/releases/20261102-hooks-v1`，应用镜像为 `sha256:aa5129d95af2ca8d3a6af59b203c212bafd4eed97041427592ebe2c4750c75e8`。不修改Codex、不扫描sessions、不运行常驻采集器。旧watcher代码、对应21项测试、自启plist及旧客户端包已移除；历史队列与云端数据保留。安装器需Python3.11+，Hook运行支持3.9+且仅标准库。

本机原版CLI0.161.0的hooks/list实际识别并精确信任本插件6个处理器，无全局绕过。输入/输出依据安装二进制内嵌schema，timeout经实际解析验证。当前CLI不注册独立PostToolUseFailure，因此只声称保留PostToolUse宿主返回的错误信息。SessionEnd限制3秒，使用空完成批次避免元信息单独消耗模型调用。

真实CLI会话source为 `01a197e2-1ca8-76c2-80e2-17814c354be9`，云端session为 `2569dce5-aa92-5e05-9c56-8b2650023912`，scope mem-project。实际print输出memory-os-native-hook-final-check，exit0；5条原生事件自动上传，1个真实deepseek-flash复盘完成，未批准任何真实候选。CLI同时提示既有disable_response_storage被忽略和既有实验特性告警；未改动这些无关设置。

MCP真实initialize/tools/list共9工具；capture_report提交与原批次幂等重试通过。合成验收scope为hook-mcp-acceptance-3ca47098d9，不冒充实际业务执行。

原生插件22项测试覆盖断网队列、重启、ACK、敏感工具跨进程抑制、自身MCP排除、配置绑定、真实loopback HTTP、空完成批次、并发FIFO及保留既有合法MCP配置。跨origin重定向另经独立loopback验证，不转发凭据。

Playwright针对上述真实Hook会话验证5条事件、来源、真实复盘、证据定位、桌面1440px/手机390px无横向溢出及刷新重连，console/page错误与失败请求均0；未操作候选批准。截图位于output/playwright/native-hook-review-desktop.png与native-hook-review-mobile.png。

未验收范围：Windows/Linux物理机器、Windows NTFS ACL及桌面端重启后的原生触发。当前桌面chat需重启或新开会话确认加载。无后续事件时断网队列不会自行补传，需下次Hook或手动flush。既有域名备案/HTTPS缺口未因插件完成而解决。

Final deployed regression for the current native plugin release: **95 passed, 2 warnings in12.51s**, including22 native Hook tests. The21 former watcher tests were removed with their implementation; this count replaces the transitional112-test result.
