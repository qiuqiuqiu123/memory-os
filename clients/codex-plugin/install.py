"""Install Memory OS hooks + MCP on the existing Codex; no daemon or transcript scan."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    if os.name != "nt":
        path.chmod(0o600)


def backup(path):
    if path.exists():
        dst = path.with_name(
            path.name + ".memory-os-" + datetime.now().strftime("%Y%m%d%H%M%S%f") + ".bak"
        )
        private_write(dst, path.read_text(encoding="utf-8"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:18082")
    p.add_argument("--project", action="append", default=[], metavar="PATH=SCOPE")
    p.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", "~/.codex"))
    p.add_argument("--config-dir", default="~/.config/memory-os")
    p.add_argument("--state-dir", default="~/.local/share/memory-os")
    p.add_argument(
        "--reuse-key-from",
        help="Read an existing private Memory OS configuration; never prints key",
    )
    a = p.parse_args()
    try:
        import tomllib
    except ImportError:
        p.error(
            "The installer requires Python 3.11+ to safely merge existing TOML; hook runtime supports 3.9+."
        )
    source = Path(__file__).resolve().parent
    sys.path.insert(0, str(source))
    from memory_hook import config_read

    codex = Path(a.codex_home).expanduser().resolve()
    cfg_dir = Path(a.config_dir).expanduser().resolve()
    state = Path(a.state_dir).expanduser().resolve()
    config_path = cfg_dir / "hook-config.json"
    if config_path.exists():
        p.error("Hook configuration already exists; inspect it before updating. No files changed.")
    overrides = {}
    for value in a.project:
        path, sep, scope = value.rpartition("=")
        if not sep or not path or not scope.strip():
            p.error("--project requires PATH=SCOPE")
        overrides[str(Path(path).expanduser().resolve())] = scope.strip()
    hooks_path = codex / "hooks.json"
    hooks = (
        json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {"hooks": {}}
    )
    if not isinstance(hooks.get("hooks"), dict):
        p.error("Existing hooks.json has unexpected structure; no files changed.")
    mcp_path = codex / "config.toml"
    mcp_content = mcp_path.read_text(encoding="utf-8") if mcp_path.exists() else ""
    try:
        mcp_parsed = tomllib.loads(mcp_content)
    except tomllib.TOMLDecodeError:
        p.error("Existing config.toml is invalid; no files changed.")
    mcp_servers = mcp_parsed.get("mcp_servers", {})
    if not isinstance(mcp_servers, dict):
        p.error("Unexpected mcp_servers configuration; no files changed.")
    if "memory-os" not in mcp_servers and re.search(
        r"^\s*[\"\']?mcp_servers[\"\']?\s*=", mcp_content, re.M
    ):
        p.error("Existing inline mcp_servers requires manual merge; no files changed.")
    key = (
        config_read(a.reuse_key_from)["api_key"]
        if a.reuse_key_from
        else getpass.getpass("Memory OS API key (hidden): ").strip()
    )
    if not key:
        p.error("API key required")
    # Validate the endpoint before writing any persistent configuration.
    from urllib.parse import urlparse

    parsed = urlparse(a.base_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        p.error("Use HTTPS or localhost through SSH")
    target = codex / "memory-os" / "memory_hook.py"
    if target.exists():
        p.error("Existing plugin runtime found; refusing to overwrite.")
    skill_target = codex / "skills" / "memory-os"
    if skill_target.exists():
        p.error("Existing memory-os skill found; refusing to overwrite.")
    config = dict(
        base_url=a.base_url.rstrip("/"),
        api_key=key,
        scope_overrides=overrides,
        db_path=str(state / "hooks.sqlite3"),
        recall_on_start=True,
    )
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(config_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    private_write(target, (source / "memory_hook.py").read_text(encoding="utf-8"))
    for file in (source / "memory-os").rglob("*"):
        if file.is_file():
            dest = skill_target / file.relative_to(source / "memory-os")
            private_write(dest, file.read_text(encoding="utf-8"))
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    ):
        argv = [sys.executable, str(target), event, "--config", str(config_path)]
        # Codex uses the native shell; commandWindows is the documented schema override.
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        entry = {
            "type": "command",
            "command": command,
            "timeout": 3 if event == "SessionEnd" else 10,
            "async": False,
        }
        hooks["hooks"].setdefault(event, []).append({"hooks": [entry]})
    backup(hooks_path)
    private_write(hooks_path, json.dumps(hooks, ensure_ascii=False, indent=2) + "\n")
    if "memory-os" in mcp_servers:
        print("Existing memory-os MCP section preserved; verify it uses this endpoint and user.")
    else:
        backup(mcp_path)
        stanza = (
            "\n[mcp_servers.memory-os]\nurl = "
            + json.dumps(a.base_url.rstrip("/") + "/mcp/")
            + "\nhttp_headers = { Authorization = "
            + json.dumps("Bearer " + key)
            + " }\n"
        )
        private_write(mcp_path, mcp_content + stanza)
    print("Installed event-driven hooks, private configuration, skill, and MCP registration.")
    print(
        "No Hook trust bypass applied. Restart Codex, review and trust the installed hooks using its normal trust prompt."
    )
    print(
        "No resident collector, transcript reads, history backfill, or model service required on this machine."
    )
    print("Cross-machine shared scopes:", json.dumps(overrides, ensure_ascii=False))
    if os.name == "nt":
        print(
            "Restrict hook-config.json and config.toml to your Windows account via NTFS permissions; chmod alone is not an ACL boundary."
        )


if __name__ == "__main__":
    main()
