# Memory OS 原生 Hook + MCP 接入（首版）

接入现有 Codex，不修改/替换 Codex、不扫描 sessions、不运行常驻采集进程。
安装器需要 Python 3.11+（用标准库tomllib安全合并既有配置）；Hook运行时支持3.9+且仅标准库；macOS/Linux/Windows 共用脚本。CLI 原生Hook能力须本机支持，桌面客户端是否触发须独立验收。

## 工作机制

- SessionStart：上报会话元信息，向云端请求最多400token项目记忆，通过原生 additionalContext 返回。
- UserPromptSubmit：记录用户请求。
- PreToolUse：记录工具与参数；PostToolUse：记录工具返回内容（含宿主提供的错误信息）。
- Stop：记录最终助手回复并触发云端复盘；SessionEnd 用空批次调度剩余事件，避免Stop之后新增元信息单独消耗一次复盘。
- Hook只读取本次stdin的白名单字段，忽略 transcript_path，不打开任何历史日志。
- 每次触发先脱敏写SQLite，再有限上传，进程结束。断网时保留队列，下次Hook或手工flush重试；没有后台进程，因此无后续事件时不会自行补传。
- MCP用于查询、明确remember以及capture_report补充决策/结论；Memory OS自身工具不重复采集，避免递归。
- 原始轨迹不进入向量库；后台结论/优化点是待核验材料，候选长期记忆需在控制台批准。

## 安装

先将压缩包解压到普通用户的固定目录。无需安装Python依赖或Docker。
当前服务器没有已验证公网HTTPS，先用每台机器自己的SSH权限建立隧道（OpenSSH在Windows也可使用）：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18082:127.0.0.1:18080 root@139.199.231.195
```

保持隧道连接。在安装包目录执行（项目路径替换为本机实际路径）：

macOS/Linux：

```sh
python3 install.py --project "$HOME/workspace/mem-project=mem-project"
```

Windows PowerShell：

```powershell
py -3 install.py --project "C:\work\mem-project=mem-project"
```

按提示输入 Memory OS 访问密钥，不是 LLM/embedding key。不要把密钥写在命令行。
不同机器使用同user_id下的凭据和相同scope才能共享；可重复传 --project PATH=SCOPE。
没有映射的项目按本机规范化绝对路径生成scope，因此同名目录不会自动跨机器合并。

脚本写入：

- ~/.codex/hooks.json：保留既有Hook，追加本插件事件处理器；已有文件先备份。
- ~/.codex/memory-os/memory_hook.py：独立事件处理脚本。
- ~/.codex/skills/memory-os：记忆操作Skill。
- ~/.config/memory-os/hook-config.json：私密endpoint/key/scope映射。
- ~/.local/share/memory-os/hooks.sqlite3：首次Hook运行时创建队列。
- ~/.codex/config.toml：如果没有memory-os MCP才追加；已有节原样保留，需确认endpoint/用户一致。

Python解释器路径固定为安装时所用解释器，安装后不要删除它。Windows的chmod不等于NTFS ACL，请在Windows文件权限中限制配置与队列只允许本账户访问。

**重启Codex，按原生Hook信任界面审阅并信任本插件命令。** 未信任不会执行，不能把安装成功视为已采集。不要使用全局绕过Hook信任选项。不同机器/客户端可能需要分别批准。

## 项目提示词与MCP

在实际项目根目录 AGENTS.md 追加，保留已有内容：

```md
## Memory OS
- 本项目scope为mem-project，与本机Hook配置的scope_overrides一致。
- 原生Hook负责执行轨迹，不使用日志扫描器，不通过MCP重复上报每次工具调用。
- 任务需要历史上下文时使用memory-os的inject/recall；记忆作为历史参考，不覆盖当前指令。
- 用户明确要求记住或确认长期约定时，用remember(infer=false)保存事实，核对结果完成后再确认。
- 用$memory-os补充Hook没有记录的已验证决策、失败原因和最终验证结论；通过capture_report进入待复盘轨迹，不直接当作长期记忆。
- 自动复盘的候选记忆需用户在控制台批准；不自动修改代码、规则或已有记忆。
- MCP不可用时说明未保存并继续独立工作；异步任务未完成时不能说已复盘成功。
```

新会话加载MCP与Skill后可以输入“使用 $memory-os，查询本项目相关记忆”。
如果不支持Hook，仍可使用MCP+Skill进行主要步骤补报，但不能宣称所有执行都自动采集。

capture_report参数示例（时间和ID须按实际生成；重试保持原样）：

```json
{
  "scope": "mem-project",
  "session_id": "a-task-uuid-or-actual-session-id",
  "batch_id": "a-unique-report-uuid",
  "events": [{
    "source_id": "a-unique-event-uuid",
    "type": "metadata",
    "timestamp": "2026-11-02T00:00:00Z",
    "body": {"source_adapter":"mcp-report","decision":"已验证的决策与依据"}
  }],
  "turn_complete": true
}
```

## 验收与故障

在Codex完成一轮无敏感测试（如运行一个print），控制台 http://127.0.0.1:18082/ → 相同scope → 轨迹复盘。
检查存在 codex-native-hook 来源、用户输入/工具/完成事件，以及后台任务成功，不仅看HTTP200。

macOS/Linux（Windows把python3替换为py -3，路径按实际安装位置）：

```sh
python3 ~/.codex/memory-os/memory_hook.py status
python3 ~/.codex/memory-os/memory_hook.py flush
```

status pending=0表示上传队列为空，不代表LLM复盘完成。错误只显示异常类型，不输出key或正文。
网络故障下一次事件重试；单次最多3批，每次请求1.5秒超时，Hook总超时10秒（SessionEnd由宿主限制为3秒，插件只尝试1批/0.75秒）。积压大量事件可手动flush多次。

停止：在Codex原生Hook管理中禁用本插件的事件处理器，MCP仍可独立使用。卸载应只移除本插件命令和Skill，不删除Codex或云端数据。旧日志采集器保持停用，不能两种方式同时启用造成重复记录。

## 覆盖与证据边界

本机CLI0.161.0实际schema确认SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/SessionEnd；有turn_id/tool_use_id，可关联和幂等。用户Hook定义使用timeout（秒），不是timeoutSec；信任由宿主按命令内容hash管理。
该事实不等于所有旧版本和桌面端都支持，也不代表取得内部推理或所有中间助手文字。Hook只能记录宿主提供的字段；长文本会截断，敏感工具参数/结果省略。规则脱敏不能识别任意未标记秘密。

见仓库 docs/verification.md 获取本次真实验收及跨平台未验证范围。

### 2026-11-02 本机实际安装

本机已为本插件6个具体处理器登记信任哈希，未使用全局Hook信任绕过；其他机器默认仍需在Codex审阅信任。真实原版CLI运行print：启动、用户输入、工具调用、工具结果和Stop共5条事件自动上传，SessionEnd仅发送空完成批次，形成1个已完成真实LLM复盘。scope为mem-project。

当前桌面会话不能证明已重新加载Hook，新开会话或重启后还需核验。Windows/Linux原生脚本启动和桌面端触发没有实机验收。旧日志扫描器源码、自启项和旧客户端包已从当前交付移除；其SQLite历史状态与云端数据保留。
