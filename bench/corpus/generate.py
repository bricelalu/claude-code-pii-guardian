#!/usr/bin/env python3
"""Render bench/corpus/templates/* once per natural language, recording exact PII spans.

Usage: generate.py --out DIR [--langs fr,en,es,it] [--check]

Writes DIR/<lang>/<template> and DIR/truth.json:
  [{"file": "fr/main.tf", "lang": "fr", "spans": [{"start", "end", "entity", "value"}]}]
Stdlib only.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
FIXTURES = HERE / "fixtures"

PLACEHOLDER = re.compile(r"\{\{(?:T:([a-z0-9_]+)|([A-Z]+)_(\d+))\}\}")
ENTITY_OF_POOL = {
    "PERSON": "PERSON",
    "CITY": "LOCATION",
    "ADDRESS": "LOCATION",
    "COUNTRY": "LOCATION",
    "EMAIL": "EMAIL_ADDRESS",
}
FORBIDDEN = ('"', "{{", "}}", "\n")


def render(template, fixture, counters):
    pools, texts = fixture["pools"], fixture["text"]
    assigned = {}
    out, spans, pos = [], [], 0
    length = 0
    for m in PLACEHOLDER.finditer(template):
        literal = template[pos:m.start()]
        out.append(literal)
        length += len(literal)
        text_key, pool, _ = m.groups()
        if text_key:
            value = texts[text_key]
        else:
            key = m.group(0)
            if key not in assigned:
                values = pools[pool]
                assigned[key] = values[counters.get(pool, 0) % len(values)]
                counters[pool] = counters.get(pool, 0) + 1
            value = assigned[key]
            spans.append({"start": length, "end": length + len(value),
                          "entity": ENTITY_OF_POOL[pool], "value": value})
        if any(f in value for f in FORBIDDEN):
            raise SystemExit(f"fixture value would break code syntax: {value!r}")
        out.append(value)
        length += len(value)
        pos = m.end()
    out.append(template[pos:])
    text = "".join(out)
    for s in spans:
        assert text[s["start"]:s["end"]] == s["value"], s
    return text, spans


def syntax_check(path):
    """Return None if OK/skipped-with-notice, else an error string."""
    ext = path.suffix
    with tempfile.TemporaryDirectory() as tmp:
        if ext == ".py":
            cmd = [sys.executable, "-m", "py_compile", str(path)]
        elif ext == ".go" and shutil.which("gofmt"):
            cmd = ["gofmt", "-e", "-l", str(path)]
        elif ext == ".java" and shutil.which("javac"):
            cmd = ["javac", "-encoding", "UTF-8", "-d", tmp, str(path)]
        elif ext == ".rs" and shutil.which("rustc"):
            cmd = ["rustc", "--edition", "2021", "--crate-type", "lib", "--emit=metadata",
                   "-A", "warnings", "-o", f"{tmp}/out.rmeta", str(path)]
        elif ext == ".tf" and shutil.which("terraform"):
            with open(path, "rb") as f:
                r = subprocess.run(["terraform", "fmt", "-"], stdin=f, capture_output=True)
            return None if r.returncode == 0 else r.stderr.decode(errors="replace")
        else:
            print(f"  - {path.name}: no syntax checker installed, skipped")
            return None
        r = subprocess.run(cmd, capture_output=True)
        return None if r.returncode == 0 else (r.stderr or r.stdout).decode(errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--langs", default=",".join(sorted(p.stem for p in FIXTURES.glob("*.json"))))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    templates = sorted(p for p in TEMPLATES.iterdir() if p.is_file())
    truth, failures = [], []
    for lang in args.langs.split(","):
        fixture = json.loads((FIXTURES / f"{lang}.json").read_text(encoding="utf-8"))
        counters = {}
        (out_dir / lang).mkdir(parents=True, exist_ok=True)
        for tpl in templates:
            text, spans = render(tpl.read_text(encoding="utf-8"), fixture, counters)
            dest = out_dir / lang / tpl.name
            dest.write_text(text, encoding="utf-8")
            truth.append({"file": f"{lang}/{tpl.name}", "lang": lang, "spans": spans})
            if args.check:
                err = syntax_check(dest)
                if err:
                    failures.append(f"{dest}: {err.strip()}")
    (out_dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    n_spans = sum(len(t["spans"]) for t in truth)
    print(f"generated {len(truth)} files, {n_spans} PII spans -> {out_dir}")
    if failures:
        raise SystemExit("syntax check failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
