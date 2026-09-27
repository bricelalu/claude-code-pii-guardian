#!/usr/bin/env python3
"""Replay real Claude Code sessions (~/.claude/projects/*/*.jsonl) through code-guard's regexes,
offline: the exact blocks code-guard would scan (user text, tool results except Write/Edit) are
rebuilt from each transcript and every change is reported. Nothing leaves this machine; the
report holds your own data, so it goes to .pii-score-out/ (gitignored).

  python3 guardrail/replay_sessions.py [--projects ~/.claude/projects] [--out .pii-score-out/replay.md]
"""
import argparse
import asyncio
import collections
import json
import pathlib

from code_guard import REGEXES, Masker

SKIP_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]


async def no_ner(_text):
    return []


def session_messages(path):
    messages = []
    for line in path.open(errors="replace"):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message") if entry.get("type") in ("user", "assistant") else None
        if isinstance(message, dict) and message.get("role") in ("user", "assistant"):
            messages.append(message)
    return messages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=pathlib.Path.home() / ".claude" / "projects", type=pathlib.Path)
    ap.add_argument("--out", default=".pii-score-out/replay.md", type=pathlib.Path)
    args = ap.parse_args()
    masker = Masker(no_ner, REGEXES, {}, [], SKIP_TOOLS)
    blocks = {}  # text -> session name (deduplicated: history repeats across turns)
    sessions = sorted(args.projects.glob("*/*.jsonl"))
    for path in sessions:
        for container, key, _tool_use in masker._targets(session_messages(path)):
            blocks.setdefault(container[key], path.parent.name)
    hits = collections.defaultdict(collections.Counter)
    lines = []
    for text, session in blocks.items():
        for start, end, label in masker._spans(text, []):
            hits[label][text[start:end]] += 1
            context = text[max(0, start - 60):end + 60].replace("\n", "⏎")
            lines.append(f"- `{label}` `{text[start:end]}` ({session}): `{context}`")
    changed = sum(1 for t in blocks if masker._spans(t, []))
    summary = [f"{len(sessions)} sessions, {len(blocks)} distinct scanned blocks "
               f"({sum(map(len, blocks)) / 1e6:.1f} MB), {changed} would change (regex only, no NER)"]
    for label, counter in sorted(hits.items(), key=lambda kv: -sum(kv[1].values())):
        summary.append(f"{label}: {sum(counter.values())} matches, {len(counter)} distinct")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(["# Session replay", "", *summary, "", *lines, ""]))
    print("\n".join(summary))
    print(f"Every match with context: {args.out}")


if __name__ == "__main__":
    main()
