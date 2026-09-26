#!/usr/bin/env python3
"""Offline false-positive sweep of code-guard's regexes over real source files (no NER, no GPU).
Every match on code is printed with its context, grouped by pattern, most frequent first.

  python3 guardrail/regex_sweep.py <dir>
  # or over the LiteLLM pod's site-packages (240 MB of real code):
  kubectl -n gateway exec -i deploy/litellm -- python - /app/.venv/lib < guardrail/regex_sweep.py
"""
import asyncio
import collections
import pathlib
import sys

sys.path[:0] = ["/app", str(pathlib.Path(__file__).resolve().parent) if "__file__" in globals() else "."]
from code_guard import REGEXES, Masker  # noqa: E402

EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".tf", ".sh", ".yaml", ".yml", ".toml", ".json", ".md", ".cfg"}


async def no_ner(_text):
    return []


def main(root):
    masker = Masker(no_ner, REGEXES, {}, [], [])
    hits = collections.defaultdict(collections.Counter)
    examples = {}
    files = size = 0
    for path in pathlib.Path(root).rglob("*"):
        if path.suffix not in EXTS or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        files += 1
        size += len(text)
        for start, end, label in masker._spans(text, []):
            value = text[start:end]
            hits[label][value] += 1
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            examples.setdefault((label, value), f"{path.name}: {text[line_start:line_end if line_end != -1 else None].strip()[:140]}")
    print(f"{files} files, {size / 1e6:.1f} MB scanned")
    for label, counter in sorted(hits.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"\n{label}: {sum(counter.values())} matches, {len(counter)} distinct")
        for value, n in counter.most_common(12):
            print(f"  {n:>5} × {value!r:<40} {examples[(label, value)]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
