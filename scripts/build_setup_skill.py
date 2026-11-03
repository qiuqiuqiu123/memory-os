"""Synchronize standalone setup-skill assets from canonical plugin sources."""

import argparse
from pathlib import Path

FILES = (
    "install.py",
    "memory_hook.py",
    "memory-os/SKILL.md",
    "memory-os/agents/openai.yaml",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "clients/codex-plugin"
    target = root / "skills/memory-os-setup/assets/plugin"
    stale = []
    for name in FILES:
        dest = target / name
        content = (source / name).read_bytes()
        if args.check:
            if not dest.exists() or dest.read_bytes() != content:
                stale.append(name)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
    if stale:
        parser.exit(1, "Stale setup assets; run scripts/build_setup_skill.py: " + ", ".join(stale) + "\n")
    print("Setup skill assets verified." if args.check else "Setup skill assets synchronized.")


if __name__ == "__main__":
    main()
