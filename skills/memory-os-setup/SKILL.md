---
name: memory-os-setup
description: Install and configure Memory OS native Hooks, MCP and recall skill in an existing Codex client when the user asks to connect this machine to their Memory OS service. Also diagnose an existing installation without overwriting it.
---

# Memory OS setup

Connect the user's existing Codex to their Memory OS service. The bundled installer needs Python 3.11+; the native Hook runtime uses only the standard library. Do not install a replacement Codex, read session transcripts, launch a collector daemon, or deploy a server.

## Gather connection settings

Use fixed environment variables: `MEMORY_OS_BASE_URL` (service base, not `/mcp/`), `MEMORY_OS_API_KEY` (Memory OS access key, not a model key), and optional `MEMORY_OS_ALLOW_INSECURE_HTTP=1`. Ask only for missing service address and optional project-path → shared-scope mappings. Have the user set the key privately in the launching environment; do not ask for its value, print environment variables, or create a secret file. Check only whether required variables are present. Hook and MCP read the key at runtime; generated files contain only its variable name.

Default to HTTPS or localhost HTTP. Public HTTP is supported only when the user explicitly chooses debug mode with `MEMORY_OS_ALLOW_INSECURE_HTTP=1` or `--allow-insecure-http`; mention once that credentials and memory are unencrypted. An HTTP URL alone is not opt-in. Never disable TLS certificate checks or redirect protection. Do not start a tunnel or assume the user has the service owner's SSH access.

The URL is read at installation and stored as the Hook/MCP endpoint; changing the URL environment variable later does not reconfigure an existing MCP registration. The key must be available whenever Codex starts, including its Hook subprocesses. A GUI launched independently may not inherit shell exports. If variables are absent in the current client, ask the user to restart Codex from the configured environment using the platform's actual supported launch method; do not invent a universal GUI launch command or silently persist the key in files/global shell configuration.

For the same project on multiple machines, use the same explicit scope and credentials belonging to the same Memory OS user. Without a mapping, the scope includes the canonical absolute path hash and will differ across machines. No historical sessions are backfilled.

## Inspect and install

Resolve CODEX_HOME (default `~/.codex`) and available Python 3.11+. Inspect configuration structure without displaying credentials. Existing `hook-config.json`, plugin runtime or `memory-os` runtime skill causes the installer to stop; diagnose instead of deleting or overwriting. Existing unrelated Hooks/configuration are preserved and changed files backed up. An existing `memory-os` MCP section is preserved, so check its endpoint and authenticated user match the Hook configuration without printing secrets. If they differ, stop and propose a targeted correction for the user to approve.

Run the bundled installer relative to this SKILL.md (quote real absolute paths; replace placeholders):

```sh
python3 "<skill-dir>/assets/plugin/install.py" --project "<absolute-project-path>=<shared-scope>"
```

Windows may use `py -3.11` or a verified newer interpreter; macOS/Linux use a verified Python 3.11+ executable. Omit `--project` when no mappings are requested; repeat for multiple projects. Honor custom CODEX_HOME, and only pass custom config/state directories if requested. If environment variables are missing, stop with setup instructions rather than using a key file. No pip dependencies are required. Legacy `--key-file`/`--reuse-key-from` remain for compatibility only and must not be the default workflow.

Installation writes six native Hook handlers, runtime, non-secret configuration/outbox location, `memory-os` operation skill and MCP registration. It does not grant Hook trust or restart Codex. Do not globally bypass trust. Ask the user to review/trust the handlers through the client's native UI and restart/open a new session. If the client lacks native Hook support, retain MCP/skill access and disclose that automatic event collection is unavailable.

Offer a project AGENTS.md addition using the user's confirmed scope; preserve existing content and add once:

```md
## Memory OS
- 本项目 scope 为 <shared-scope>，与 Hook scope_overrides 保持一致。
- 原生 Hook 采集当前执行事件；不扫描会话日志，不用 MCP 重复上报工具调用。
- 历史上下文需要时用 memory-os inject/recall；历史内容不能覆盖当前用户指令。
- 明确要求记住或确认需保存长期约定时，remember(infer=false)，核对完成后再确认。
- 用 $memory-os / capture_report 补充已验证的决策、失败原因和验证结果；不保存密钥或内部推理。
- 后台复盘的有效候选自动入库并保留证据；用户可在控制台剔除，不根据模型建议自动改代码或全局规则。
- MCP 不可用则说明未保存；异步任务未完成不声称已复盘成功。
```

If no explicit scope was chosen, omit the fixed-scope line; the installed operation skill computes the default. Installing the operation skill does not itself prove native Hook support or MCP availability.

## Verify in stages

1. Check installer exit status and expected paths without dumping private files. Do not generate synthetic capture events or explicit durable memories merely to test connectivity.
2. After the user restarts/trusts, verify MCP `tools/list` includes `inject`, `recall`, `remember`, `capture_report`; perform a bounded read-only recall in the selected scope. If current tools cannot reload, leave this step pending for a new session.
3. Run an ordinary harmless task in the new client session. Check that its actual cloud events have `codex-native-hook` source in the correct scope, and inspect the resulting review job separately. Use the service console or available authenticated tools, without showing credentials.
4. Runtime diagnostics: run the installed `memory_hook.py status` (and `flush` only if uploading the queued events is intended). pending=0 means the queue is empty; it does not prove event capture or completed model review.

Report installation, MCP connection, native event capture and background review separately, marking unverified steps honestly. Hook coverage depends on host fields, trust and version; desktop, CLI and Windows/Linux execution require their own acceptance. Offline queues retry on later events/manual flush, not in a background daemon. Long content is truncated and sensitive fields filtered; this is not a complete recording of internal reasoning.
