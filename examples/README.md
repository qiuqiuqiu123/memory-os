# Agent 接入示例

## Claude Code hooks

在 Claude Code 所运行的机器上安装本项目 Python 包的依赖，并确保 hook 命令能导入 `memory_os`。将 `claude-settings.json` 的 `hooks` 合并到项目 `.claude/settings.json`，把示例中的 `python` 替换成虚拟环境解释器的绝对路径；项目未安装为包时，还需在命令前设置 `PYTHONPATH=/本项目绝对路径`。不要覆盖已有 hooks。

在启动 Claude Code 的环境中配置：

```bash
export MEMORY_OS_URL=http://127.0.0.1:8000
export MEMORY_OS_API_KEY='填入事件核心 API_KEY'
export MEMORY_OS_SCOPE='my-project'
# 可选：有 Langfuse 时直接采集原始 span；否则走核心降级事件。
export LANGFUSE_BASE_URL=http://127.0.0.1:3000
export LANGFUSE_PUBLIC_KEY='填入项目公钥'
export LANGFUSE_SECRET_KEY='填入项目私钥'
```

SessionStart 注入偏好和项目记忆，UserPromptSubmit 按任务主题检索，PreToolUse 的 Skill 工具按 skill 名称注入。普通工具执行记录 span；PostToolUseFailure 会记录错误。命令 hook 输出 Claude Code 的 `hookSpecificOutput.additionalContext`，不会阻止工具执行。

默认不采集工具原始参数、返回值、最终回答或文件内容，仅保留工具名、时间、状态和 skill 名称。任务检索会把当前 prompt 的前 4000 字符发送到自己配置的记忆服务作为查询。工具 token 用量无法从这些 hooks 完整取得，记录为 0；需要详细 LLM token 用量时，请在自研 Agent 中接入 Langfuse/OTel。

Langfuse 不可达时，事件存入本机 SQLite，随后尝试发送到事件核心。默认缓存为 `~/.memory-os/client-<身份哈希>.sqlite3`，按服务地址、API 凭据和 scope 隔离；可用 `MEMORY_OS_CACHE` 指定独立路径。同一路径不能混用不同身份。只使用 MCP 不会自动安装这些 hooks。

## Python SDK 与离线同步

```python
import os
from memory_os.client import MemoryClient

client = MemoryClient(
    'http://127.0.0.1:8000', os.environ['MEMORY_OS_API_KEY'],
    scope='my-project', db_path='./my-project-cache.sqlite3',
)
try:
    # 在线时返回提取任务，断网时返回 queued_offline，并保存原 request_id。
    result = client.remember('用户偏好中文回复', tags=['preference'])
    print(result)
    print(client.sync())
    print(client.local_memories())
    context = client.inject('session.start')
    # 自研 Agent：把 context['content'] 加入下一次模型调用的上下文。
finally:
    client.close()
```

`remember()` 的离线输入是待提取请求，未完成提取前不会成为本地事实。需要显式离线创建已确认事实时，可 `enqueue()` 一个 `memory.created` 事件；它会乐观更新本地投影，恢复联网后由服务端校验并接收。

推送确认和拉取游标分别保存；推送自己的新事件不会跳过其他设备的旧事件。本地投影支持查看已同步或显式创建的记忆；离线语义检索没有伪造实现，`recall()` 仍需要服务和真实 embedding 可用。

### 在模型调用前自动注入

自研 Agent 可把原本的 `llm.chat.completions.create(...)` 改为 `client.chat(llm, ...)`，复用自己配置的同步 OpenAI 兼容客户端：

```python
from openai import OpenAI

llm = OpenAI(
    api_key=os.environ['LLM_API_KEY'],
    base_url=os.environ['LLM_BASE_URL'],
)
try:
    response = client.chat(
        llm,
        model=os.environ['LLM_MODEL'],
        messages=[{'role': 'user', 'content': '帮我检查这次部署的注意事项'}],
        token_budget=500,
    )
finally:
    llm.close()
```

请在上一个示例的 `client.close()` 之前调用。SDK 默认从最后一条字符串 user 消息获取检索 query，先调用注入接口，再复制消息列表并插入独立 system 参考上下文，明确历史记忆不得覆盖应用规则或用户指令。原消息列表不会被修改。预算包括参考包装文字，按 `cl100k_base` 计算，不含模型协议自身的消息封装开销。

只需要准备上下文时使用 `client.prepare_messages(messages, ...)`。skill 注入可指定 `trigger='skill.invoked', context={'skill_name': 'deploy', 'query': '当前任务'}`。注入服务或模型调用失败会显式抛出异常，由应用决定是否降级，不会悄悄绕过记忆后继续调用模型。

## Langfuse 语义适配约定

服务端适配器使用 Langfuse Public API `/api/public/traces` 和 `/api/public/traces/{id}`，每轮分页全扫描，包含旧 trace 的后续变化。它适合单用户调试数据量，尚未针对大量历史 trace 优化扫描成本。

- `trace.metadata.memory_os_scope` 指定项目；租户固定由服务端 `LANGFUSE_USER_ID` 绑定，原始 trace 不能声明其他用户。
- observation 的 `metadata.skill_name` 或 `metadata.skill` 标识 skill 调用。
- observation 的 `metadata.error_type` 标识失败类别；没有时为 `execution_error`。
- `metadata.retry_attempt` / `retry_delay_ms` 标识重试。
- `trace.metadata.trace_finished=true` 明确表示 trace 已结束，`metadata.status` 指定最终状态。不会仅凭某个 span 结束就推断整个 trace 已结束。

事件使用确定性 UUID 去重；skill 统计按 observation ID 更新最终状态，避免活跃 trace 后补耗时与 token 时重复计数。自建控制台显示语义摘要；原始输入、输出与完整 span 树查看 Langfuse。
