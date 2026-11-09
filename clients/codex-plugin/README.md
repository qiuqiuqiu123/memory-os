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
- 原始轨迹不进入向量库；后台结论/优化点是待核验材料，证据校验通过的候选自动入库，用户可在控制台事后剔除。

## 安装

先将压缩包解压到普通用户的固定目录。无需安装Python依赖或Docker。
可使用 HTTPS，或通过 SSH 隧道访问本机 HTTP（OpenSSH 在 Windows 也可使用）：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18082:127.0.0.1:18080 root@139.199.231.195
```

保持隧道连接；也可以显式选择下文的公网 HTTP 调试方式。在安装包目录执行前，先设置服务地址和访问密钥（项目路径替换为本机实际路径）：

macOS/Linux：

```sh
export MEMORY_OS_BASE_URL="http://127.0.0.1:18082"
# 在自己的终端中隐藏输入，不将真实密钥写入命令历史
read -r -s MEMORY_OS_API_KEY  # 输入后按回车；适用于 Bash/Zsh
export MEMORY_OS_API_KEY
python3 install.py --project "$HOME/workspace/mem-project=mem-project"
```

Windows PowerShell：

```powershell
$env:MEMORY_OS_BASE_URL = "http://127.0.0.1:18082"
$secret = Read-Host "Memory OS API key" -AsSecureString
$env:MEMORY_OS_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
py -3 install.py --project "C:\work\mem-project=mem-project"
```

安装器读取 `MEMORY_OS_BASE_URL` 和 `MEMORY_OS_API_KEY`，不要求另建密钥文件；密钥不是 LLM/embedding key。不要把真实密钥写入聊天或命令参数。
不同机器使用同user_id下的凭据和相同scope才能共享；可重复传 --project PATH=SCOPE。
没有映射的项目按本机规范化绝对路径生成scope，因此同名目录不会自动跨机器合并。

脚本写入：

- ~/.codex/hooks.json：保留既有Hook，追加本插件事件处理器；已有文件先备份。
- ~/.codex/memory-os/memory_hook.py：独立事件处理脚本。
- ~/.codex/skills/memory-os：记忆操作Skill。
- ~/.config/memory-os/hook-config.json：endpoint、环境变量名和scope映射（环境变量模式不保存key）。
- ~/.local/share/memory-os/hooks.sqlite3：首次Hook运行时创建队列。
- ~/.codex/config.toml：如果没有memory-os MCP才追加，使用 `bearer_token_env_var = "MEMORY_OS_API_KEY"`；已有节原样保留，需确认endpoint/用户一致。

**公网 HTTP 调试（默认关闭）**：在安装前额外设置：

```sh
export MEMORY_OS_BASE_URL="http://139.199.231.195:18080"
export MEMORY_OS_ALLOW_INSECURE_HTTP=1
```

PowerShell 对应 `$env:MEMORY_OS_BASE_URL = "http://139.199.231.195:18080"` 和 `$env:MEMORY_OS_ALLOW_INSECURE_HTTP = "1"`。也可在安装命令添加 `--allow-insecure-http`。HTTP 地址本身不代表同意开启；未开启时安装器和运行时都拒绝公网 HTTP。调试模式会明文传输密钥和记忆，不改变 HTTPS 校验或重定向保护。

URL 是安装时输入，随后写入 Hook/MCP 配置；后续单独更改 URL 环境变量不会自动更新已有配置。密钥由 Hook/MCP 每次从 Codex 环境读取，不写入配置。每次启动 Codex 都需让其继承 `MEMORY_OS_API_KEY`；桌面图标启动的应用可能不继承终端变量。若当前客户端读不到变量，先从已配置的环境重新启动对应客户端。不要让安装助手打印变量值，也不要自动写入 shell 配置文件。

旧 `--key-file` / `--reuse-key-from` 方式只保留兼容；显式使用它们时仍会采用旧的密钥持久化方式。

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
- 自动复盘的有效候选由服务端自动入库；用户可在控制台剔除。不根据模型建议自动修改代码或全局规则。
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

## 在其他 Codex 中用一句提示词开始安装

仓库现已公开，无需私仓授权；连接记忆服务仍需要访问密钥。在 Codex 输入：

```text
使用 $skill-installer 从 GitHub 仓库 qiuqiuqiu123/memory-os 安装 skills/memory-os-setup，安装完成后直接读取已安装目录的 SKILL.md，按其流程引导我接入 Memory OS。使用 MEMORY_OS_BASE_URL 和 MEMORY_OS_API_KEY 环境变量，询问需要共享的项目 scope；公网 HTTP 调试仅在明确选择后启用 MEMORY_OS_ALLOW_INSECURE_HTTP=1。不要另建密钥文件或让我把密钥发在聊天里。
```

如果当前会话尚未发现新 skill，在下一轮输入 `使用 $memory-os-setup 帮我配置记忆接入`；必要时重开会话。安装助手本身包含插件资产，安装 skill 后无需再克隆完整项目或下载模型。

用户设置上述三个环境变量即可；默认 HTTPS/localhost HTTP，公网 HTTP 调试需显式启用。密钥由 Hook 与 MCP 从运行环境读取，不通过文件配置，也不在聊天中传递。服务地址由部署方提供。

完成安装后仍需用户按原生界面信任 Hook 并重启/新开会话；文件安装、MCP 查询、实际 Hook 上传及后台复盘分开验收。现有安装不自动覆盖。普通用户不需要服务管理员的 root SSH 权限；服务地址由部署方提供。

维护者：修改 `clients/codex-plugin/` 后运行 `python3 scripts/build_setup_skill.py` 同步可独立分发资产；CI/提交前用 `--check` 检查一致性。
