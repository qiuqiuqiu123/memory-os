> 历史实现：2026-11-02起本机日志扫描器停用，当前入口为[原生Hook + MCP插件](../clients/codex-plugin/README.md)。下文为旧版本记录，不代表当前已启用服务；不要按旧启用命令与新插件同时运行。

# Codex 自动采集：所有本地项目按工作区隔离

这是独立的本地采集进程，读取日志即可工作；本次另外配置了 Codex MCP 与全局 AGENTS.md 保存约定，二者不负责轨迹上报。
它从本机 `~/.codex/sessions/**/*.jsonl` 读取新增记录，将已授权项目的可见事件
脱敏后放入本地 SQLite outbox，再推送到 Memory OS 的 capture API。
本机现已配置自动采集与SSH隧道，默认所有项目；从启用水位开始，不回灌旧历史。实际验收结果见本文末尾。

## 覆盖范围与格式依据

开发时只读检查了本机32个JSONL的元数据、记录类型、字段名和数量，没有输出会话正文。
其中包括历史0.161.0以及当前0.164.0-alpha.11.1。所有已发现日志的history_mode为paginated，
但当前主会话仍在持续追加response_item与event_msg；已验证2026-11-01当天新增消息和工具记录。
这只证明当前已发现日志和当前活跃会话，不能保证没有JSONL的其他客户端或未来版本也被覆盖。
支持 `session_meta` 的 `session_id` / `id`、`cwd`；按 `turn_context.cwd` 更新当前目录。
用户已明确选择采集**所有本地Codex项目**，安装模板使用 `mode: all_projects`。
每个规范化绝对cwd生成独立scope：`codex:<目录末段slug>:<绝对路径SHA256前16位>`。
路径哈希避免同名项目串数据；`scope_overrides` 将 mem-project 映射回现有 `mem-project` scope。
同一会话发生turn_context.cwd切换时立即拆分批次，不把不同项目混在一个scope中。
所有项目的可见用户/assistant内容和工具活动会发送至云端Memory OS及所配置LLM复盘。
如需缩小范围，可使用 `mode: allowlist` 和 `allowed_cwds` 的规范化绝对路径**精确匹配**；
该模式不自动允许子目录或同一父目录下其他项目。库默认仍为allowlist以免已有配置悄悄扩大授权。

上传白名单：

- `response_item.message` 中 user / assistant 的 input_text / output_text / text。
- `response_item.function_call`、`custom_tool_call` 的工具名、call_id、参数。
- 对应 tool output 的 call_id 和返回内容。
- `event_msg.task_complete` / `turn_aborted` 的 turn_id、耗时和完成状态；不复制其中的 assistant 消息。

不上传 system / developer 消息、reasoning、world_state、turn_context 正文、
agent_message 子智能体内部消息、session_meta标记为subagent的整个子会话、token 统计、图像/音频内容和 `event_msg.item_completed`
镜像。`event_msg` 的 user/assistant 文本镜像也不重复采集。仅有这些被排除记录的旧版本
客户端不在当前覆盖范围，升级格式需要补适配后才能声称采集完整。

## 启用时间与持久性

第一次创建数据库时，对当时存在的 JSONL 文件建立 EOF 水位，不回灌历史。
当前已打开的会话也只采集此后追加的数据。新发现文件的事件时间必须不早于启用时间；
缺少或非法 timestamp 的记录会跳过并记录诊断。`dry-run` 也会建立这个启用水位。

每次完整行读取、source_id 去重和 batch 入队在同一 SQLite 事务提交；半行不推进水位。
网络错误保留队列和原 batch_id，下次重试原样发送，不能使用 HTTP 重试产生新 ID。
只有服务端返回匹配 batch_id、合法内部 session_id UUID 和范围正确的 accepted 数量后才确认出队；
重复批次可返回accepted=0。不要求每次都有job，尚未结束的轮次可能还没有复盘任务。这表示服务端接受批次，
**不表示异步记忆提取已经完成**。提取结果仍应在云端任务和记忆列表中检查。

文件 inode 改变、缩短、前缀或已读边界改变时记录 file_reset，重新读取但按稳定
session/item 身份去重。无 item ID 的记录使用完整记录的散列身份。文件原地覆盖且
未改变已保存前缀/边界的极端写法不能完整识别，诊断不等于文件系统审计。
已有文件删除、未落盘的客户端事件、Codex 没有记录的过程均无法凭空恢复。

## 脱敏与数据限制

递归隐藏 api_key、Authorization、密码、secret、token、cookie、credentials、private_key
字段，处理 JSON 工具参数、Bearer、常见 token 前缀、URL userinfo / 查询凭据、shell
环境赋值/凭据参数与 PEM 私钥块。检测到 `.env`、credentials、私钥、printenv、security
凭据读取、config.toml 读取等敏感工具操作时，整段参数与对应 call_id 的输出不上传；
关联状态也持久化到 SQLite，重启不丢失。服务端仍需独立二次清理。

每个文本字段上限 12000 字符，列表至多 100 项；内容带 `[TRUNCATED]`，事件 body.capture
记录 redactions / truncated。超过 2MiB 的源行直接丢弃并记录 oversized_line；这会损失
该行信息。规则脱敏不是任意秘密识别器，用户主动写入的未标记秘密或自定义工具格式仍
可能被采集，因此只能在明确授权的工作区启用，并在上传前检查业务数据边界。

配置与 SQLite 文件强制 600；锁文件也是 600。配置拒绝宽松权限与非 HTTPS 远端端点，
允许 localhost HTTP 用于 SSH tunnel。数据库通过哈希绑定端点、API key、scope、采集模式、工作区映射和
白名单，改变其中任一项需使用独立数据库，避免旧 outbox 发往另一目的地。当前权限
保护针对其他本机用户，不提供磁盘加密；磁盘备份同样包含已脱敏的业务内容。

## 在其他机器准备配置

模板见 `examples/codex-capture.json.example`。推荐通过交互脚本安全输入 API key：

```sh
cd /Users/xiaozhang/workspace/mem-project
.venv/bin/python examples/codex-install.py --base-url http://127.0.0.1:18082
```

此命令创建：

- `~/.config/memory-os/codex-capture.json`（600）
- `~/Library/LaunchAgents/com.memory-os.codex-capture.plist`（600）
- `~/.local/share/memory-os/`（新建目录权限 700）

默认**只准备、不启动**。脚本拒绝覆盖已有配置。示例 localhost:18082 需要已有 SSH
转发或本地 Memory OS 服务；使用远端反向代理时把 base URL 改成实际 HTTPS 地址，
带 `/memoryos` 前缀也可以。不要把 API key 写进命令参数、仓库模板或 launchd plist。

## dry-run / 状态 / 手动运行

```sh
.venv/bin/python -m memory_os.codex_collector dry-run
.venv/bin/python -m memory_os.codex_collector status
.venv/bin/python -m memory_os.codex_collector once
.venv/bin/python -m memory_os.codex_collector run
```

`--config /absolute/private/config.json` 可指定另一个配置文件。

- `dry-run`：扫描并将新事件放入本地队列，只显示数量、文件水位、覆盖范围和诊断，
  不显示正文、不发送网络请求。之后 once/run 会上传这些已排队批次。
- `status`：只读 SQLite，后台运行时也可执行；显示 pending 数量、文件水位、上传错误
  类型和最近 50 条诊断，不显示 key 或事件正文。
- `once`：扫描一次并尝试发送最多 100 个已排队批次。
- `run`：按 poll_seconds（至少 1 秒）循环，默认 3 秒。

单数据库只允许一个采集/上传实例，第二个实例拒绝启动。HTTP 错误仅保存异常类型，
不把可能含凭据或正文的 provider response 写入日志。首次接入应使用合成无敏感测试
消息确认云端 accepted/job/提取结果，然后再启用常驻采集。

## launchd 常驻与停止

检查配置和 dry-run 结果后再启用：

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.memory-os.codex-capture.plist
```

也可在准备脚本中显式传入 `--enable`，但不要在尚未确认上传范围时使用。
launchd 使用项目 `.venv/bin/python`、固定 WorkingDirectory，登录后自启，异常退出后
节流重启；源码或虚拟环境移位后需要更新配置。日志位于本地状态目录，不输出正文。

停止后台（保留配置、水位与队列）：

```sh
launchctl bootout gui/$(id -u)/com.memory-os.codex-capture
```

停止后再次启用会处理停机期间新增且仍保留的记录；不是重新建立启用时间。
若希望从另一个新的时间开始，需要由用户明确选择新的 db_path，不能静默重置水位。

## 验证边界

`tests/test_codex_collector.py` 是合成 JSONL 与离线 HTTP transport 测试，覆盖 EOF baseline、
partial line、重启幂等、HTTP 重试稳定身份、精确 cwd、镜像排除、文件覆盖/替换、异常
结束、敏感工具整段抑制、脱敏、锁和并发只读 status。它们不等于真实 Codex 自动上传
或云端提取验收；本次实际安装与真实端到端验收另见下文。

## 本机实际安装

已配置本机用户级 launchd：`com.memory-os.codex-tunnel` 将本机18082加密转发到云端18080；`com.memory-os.codex-capture` 常驻读取新增日志。只有采集器和SSH隧道在本机运行，本地Memory OS API、PG、Qdrant等容器继续停止。专用非管理员API凭据存于600权限的配置文件，未进入仓库或plist。

Codex MCP 已在 `~/.codex/config.toml` 注册memory-os（编辑前已保存600权限备份），地址 `http://127.0.0.1:18082/mcp/`；重启Codex/新建会话后加载。全局 `~/.codex/AGENTS.md` 追加明确保存规则，不要求模型重复上报工具轨迹。

控制台同样可从 `http://127.0.0.1:18082/` 访问；全局scope输入可选择已采集的项目，进入“轨迹复盘”查看会话、任务状态、证据、优化建议和候选记忆。候选经批准才进入语义记忆库。云端当前使用网页中实际保存的LLM，修改后仅影响后续复盘；自动复盘会产生该模型服务的调用用量。

停止采集与隧道（保留本地队列和所有数据）：

```sh
launchctl bootout gui/$(id -u)/com.memory-os.codex-capture
launchctl bootout gui/$(id -u)/com.memory-os.codex-tunnel
```

重新启用：

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.memory-os.codex-tunnel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.memory-os.codex-capture.plist
```

## 本次实际验收结果

2026-11-01已启用所有项目采集，本机32个既有JSONL建立EOF基线；父子共享sessionID的日志按文件身份分别处理。当前桌面会话新增事件已进入云端，独立真实Codex CLI执行只读命令的7条事件自动上传并完成deepseek-flash后台复盘；可在mem-project的“轨迹复盘”查看。最终完整测试91项通过，collector21项故障/性能用例通过。长中文/ASCII脱敏处理已修复无界正则回溯问题，40k字符最大约3.5ms（本地20次小样本基准）。

云端当前使用已保存的deepseek-flash与Gitee bge-m3（1024维），并非强制GPT；可继续在网页修改。原始脱敏轨迹存PostgreSQL，不进入向量索引；只有经批准的候选和明确保存的记忆会建立向量索引。复盘输出预算最多6000tokens，默认每轮完成/中止或100新增事件调度，长窗口再分批，故会产生LLM服务用量。

注意本机status的pending0仅说明采集批次上传完成；后台复盘是否完成应查看网页任务状态。网页会显示失败并可“重试复盘”；不要把上传成功当作模型分析成功。新开Codex会话后加载已添加的memory-os MCP；自动采集器已经独立运行，不需要每次手工调用保存。

最终补充验证：EOF基线恢复最近的turn_context.cwd；routing_metadata_v1仅修复已读边界内的路由元数据，不改变水位、不回灌历史。真实CLI复盘页面在1440px桌面和390px手机宽度均无横向溢出，证据链接、刷新重连通过。通过实际Codex配置完成MCP initialize、列出8个工具并真实调用inject成功。
