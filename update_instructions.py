#!/usr/bin/env python3
"""Put Kārya's tracking instructions into ~/.claude/CLAUDE.md (Claude Code)
and ~/.codex/AGENTS.md (Codex), from CLAUDE_SNIPPET.md.

    python3 update_instructions.py             # add or refresh the section
    python3 update_instructions.py --remove    # take it out again

The section runs from "# Task tracking (Kārya)" to the next top-level "# "
heading (or the end of the file); it is replaced in place or appended.
Everything else in those files is left exactly as it is. A file is only
created when that tool is installed (its ~/.claude or ~/.codex folder
exists). Safe to re-run.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from store import ROOT, load_config

TARGETS = [Path.home() / ".claude/CLAUDE.md", Path.home() / ".codex/AGENTS.md"]
SECTION = re.compile(r"^# Task tracking \(Kārya\)\n.*?(?=^# |\Z)", re.M | re.S)


def snippet() -> str:
    config = load_config()
    text = (ROOT / "CLAUDE_SNIPPET.md").read_text().strip() + "\n"
    return (text.replace("{name}", config.get("name") or "The user")
                .replace("{root}", str(ROOT).replace(str(Path.home()), "~", 1))
                .replace("{port}", str(config["port"])))


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Kārya's instructions for Claude Code and Codex")
    parser.add_argument("--remove", action="store_true", help="remove the Kārya section instead")
    args = parser.parse_args()
    block = snippet()
    for path in TARGETS:
        if not path.parent.exists():
            print(f"skip {path} ({path.parent.name} is not installed)")
            continue
        text = path.read_text() if path.exists() else ""
        if args.remove:
            new = SECTION.sub("", text).rstrip() + ("\n" if text.strip() else "")
            action = "removed"
        elif SECTION.search(text):
            new = SECTION.sub(lambda _: block + "\n", text, count=1).rstrip() + "\n"
            action = "updated"
        else:
            new = (text.rstrip() + "\n\n" if text.strip() else "") + block
            action = "added"
        if new == text:
            print(f"{path}: already current")
            continue
        path.write_text(new)
        print(f"{path}: section {action}")


if __name__ == "__main__":
    main()
