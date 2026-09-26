#!/usr/bin/env python3
"""End-to-end benchmark through the running gateway: every file in a corpus (truth.json) goes
through LiteLLM's guardrail endpoint, code-guard (ingress -> LiteLLM ->
analyzer proxy -> RunPod GLiNER2), and the masked text that comes back is compared with the
ground truth.

  gateway_bench.py --corpus DIR --out DIR [--gateway URL] [--rescore]

Writes DIR/masked/<file> (what would be sent to Anthropic), DIR/report.md and
DIR/results.json. Needs LITELLM_MASTER_KEY (env) unless --rescore, which re-scores the
masked files saved by a previous run without calling the gateway. Stdlib only.

Scored: the PII the gateway is configured to mask (MASKED_ENTITIES). ZIP_CODE and DATE_TIME
cells are PII the gateway doesn't mask by design, so they are not scored.

Only masked values become placeholders, so a replaced region that doesn't overlap a true
PII span is a real over-mask (e.g. a code token, or a safe cell like a UUID).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import overlaps  # noqa: E402
from span_score import structure_error  # noqa: E402

# manifests/21-litellm-config.yaml: one guardrail (guardrail/code_guard.py), NER + regexes.
GUARDRAILS = ("code-guard",)
MASKED_ENTITIES = ("PERSON", "LOCATION", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "IP_ADDRESS")
# code-guard writes <ENTITY> for NER hits and [<PATTERN>_REDACTED] for regex hits.
PLACEHOLDER = re.compile(r"<(PERSON|LOCATION|DATE_TIME|CREDIT_CARD)>|\[([A-Z0-9_]+)_REDACTED\]")


def apply_guardrails(url, key, text):
    """Run the guardrails in gateway order, feeding each one the previous one's output."""
    t0 = time.perf_counter()
    for name in GUARDRAILS:
        req = urllib.request.Request(
            f"{url}/guardrails/apply_guardrail",
            data=json.dumps({"guardrail_name": name, "text": text}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            text = json.loads(urllib.request.urlopen(req, timeout=320).read())["response_text"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            status = "blocked" if "Blocked" in detail else f"http {e.code}"
            return status, f"{name}: {detail[:300]}", time.perf_counter() - t0
    return "ok", text, time.perf_counter() - t0


def replaced_regions(original, masked):
    """Map each placeholder in `masked` back to the original text it replaced."""
    # re.split with two groups yields literal, g1, g2, literal, ...; keep whichever group matched.
    raw = PLACEHOLDER.split(masked)
    literals = raw[0::3]
    entities = [a or b for a, b in zip(raw[1::3], raw[2::3])]
    if not original.startswith(literals[0]):
        raise ValueError("masked text does not start like the original")
    pos, regions, pending = len(literals[0]), [], []
    for i, (entity, literal) in enumerate(zip(entities, literals[1:])):
        pending.append(entity)
        last = i == len(entities) - 1
        if literal == "" and not last:
            continue  # adjacent placeholders: their boundary is unknowable, merge them
        end = len(original) if (last and literal == "") else original.find(literal, pos)
        if end == -1:
            raise ValueError(f"could not realign after <{entity}>")
        regions.append((pos, end, "/".join(pending)))
        pending = []
        pos = end + len(literal)
    return [(s, e, ent) for s, e, ent in regions if e > s]


def context(text, start):
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", start)
    return text[ls: le if le != -1 else len(text)].strip()[:110], text.count("\n", 0, start) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gateway", default="http://litellm.local:8080")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score the masked files from a previous run instead of calling the gateway")
    args = ap.parse_args()
    corpus, out = Path(args.corpus), Path(args.out)
    previous = {}
    if args.rescore:
        previous = {r["file"]: r for r in json.loads((out / "results.json").read_text(encoding="utf-8"))}
    else:
        key = os.environ.get("LITELLM_MASTER_KEY") or sys.exit("LITELLM_MASTER_KEY is not set")

    truth = json.loads((corpus / "truth.json").read_text(encoding="utf-8"))

    results = []
    for entry in truth:
        path = corpus / entry["file"]
        original = path.read_text(encoding="utf-8")
        ext = path.suffix.lstrip(".")
        dest = out / "masked" / entry["file"]
        if args.rescore:
            prev = previous[entry["file"]]
            status, secs = prev["status"], prev["seconds"]
            payload = dest.read_text(encoding="utf-8") if status == "ok" else prev.get("detail", "")
        else:
            status, payload, secs = apply_guardrails(args.gateway, key, original)
            print(f"  {entry['file']}: {status} ({secs:.1f}s)", file=sys.stderr)
        result = {"file": entry["file"], "ext": ext, "lang": entry["lang"], "status": status,
                  "seconds": round(secs, 2), "masked": [], "missed": [], "partial": [], "over_masked": []}
        if status != "ok":
            result["detail"] = payload
            results.append(result)
            continue
        masked = payload
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(masked, encoding="utf-8")
        try:
            regions = replaced_regions(original, masked)
        except ValueError as e:
            result.update(status="unaligned", detail=str(e))
            results.append(result)
            continue
        spans = [t for t in entry["spans"] if t["entity"] in MASKED_ENTITIES]
        for t in spans:
            hits = [r for r in regions if overlaps(t["start"], t["end"], r[0], r[1])]
            # A value counts as masked when every letter/digit is gone; separators like the
            # comma in "Paris, France" may legitimately survive between two placeholders.
            left = [c for i, c in enumerate(original[t["start"]:t["end"]], t["start"])
                    if c.isalnum() and not any(s <= i < e for s, e, _ in hits)]
            item = {"entity": t["entity"], "value": t["value"], "as": sorted({h[2] for h in hits})}
            key_ = "missed" if not hits else ("partial" if left else "masked")
            if key_ == "partial":
                item["left"] = "".join(left)
            if "column" in t:
                item["column"], item["row"] = t["column"], t["row"]
                col = result.setdefault("columns", {}).setdefault(t["column"], {"masked": 0, "total": 0})
                col["total"] += 1
                col["masked"] += key_ == "masked"
            result[key_].append(item)
        for s, e, ent in regions:
            if not any(overlaps(t["start"], t["end"], s, e) for t in entry["spans"]):
                ctx, line = context(original, s)
                result["over_masked"].append({"text": original[s:e], "as": ent, "line": line, "context": ctx})
        err = structure_error(original, masked, regions, ext, dest)
        result["structure"] = err or "ok"
        results.append(result)

    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(render_report(results, args.gateway), encoding="utf-8")
    print(f"report: {out / 'report.md'}", file=sys.stderr)


def render_report(results, gateway):
    lines = [
        "# GLiNER2 end-to-end benchmark (through the gateway)",
        "",
        f"Each file was sent to `{gateway}/guardrails/apply_guardrail`, through the guardrails",
        f"{' then '.join(f'`{g}`' for g in GUARDRAILS)} (gateway order): Presidio + GLiNER2 on the RunPod GPU",
        "for names and places, plus regexes for email/phone/IBAN/IP.",
        "The masked text is exactly what LiteLLM would forward to Anthropic; it is saved under `masked/`.",
        "",
        "Legend: ✅ masked · ❌ not masked · ⚠️ partly masked · 🔶 code/text masked that isn't PII · ⛔ request blocked",
        "",
        "- Scored: the PII the gateway is configured to mask: PERSON (≥ 0.85), LOCATION (≥ 0.96),",
        "  EMAIL_ADDRESS, PHONE_NUMBER, IBAN_CODE, IP_ADDRESS (regex). DATE_TIME is not masked",
        "  by design and isn't scored.",
        "- Exports: the 50-row `customers` table (export.sh) as CSV, Markdown table and minified JSON.",
        "  `zipcode` and `birth_date` are PII the gateway doesn't mask by design (not scored);",
        "  `customer_ref`, `created_at`, `status`, `plan` are safe cells (masking them = over-mask).",
        "- \"Still valid\": CSV/Markdown/JSON still parse with the same rows, code still compiles",
        "  (TypeScript: heuristic, no compiler available).",
        "",
        "## Summary by file type",
        "",
        "| Type | Files | Blocked | PERSON masked | LOCATION masked | EMAIL/PHONE/IBAN/IP masked | Over-masked tokens | Still valid | Avg time |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    by_ext = defaultdict(list)
    for r in results:
        by_ext[r["ext"]].append(r)

    def frac(rs, pred):
        items = [i for r in rs for k in ("masked", "missed", "partial") for i in r[k] if pred(i["entity"])]
        ok = sum(1 for r in rs for i in r["masked"] if pred(i["entity"]))
        return f"{ok}/{len(items)}" if items else "—"

    for ext in sorted(by_ext):
        rs = by_ext[ext]
        ok = [r for r in rs if r["status"] == "ok"]
        blocked = sum(1 for r in rs if r["status"] == "blocked")
        valid = sum(1 for r in ok if r.get("structure") == "ok")
        avg = sum(r["seconds"] for r in rs) / len(rs)
        lines.append(
            f"| {ext} | {len(rs)} | {blocked} | {frac(ok, lambda e: e == 'PERSON')} | "
            f"{frac(ok, lambda e: e == 'LOCATION')} | {frac(ok, lambda e: e not in ('PERSON', 'LOCATION'))} | "
            f"{sum(len(r['over_masked']) for r in ok)} | {valid}/{len(ok)} | {avg:.1f}s |")

    exports = [r for r in results if r["status"] == "ok" and r.get("columns")]
    if exports:
        columns = list(dict.fromkeys(c for r in exports for c in r["columns"]))
        lines += ["", "## Exports by column (masked / cells)", "",
                  "| Column | " + " | ".join(r["file"] for r in exports) + " |",
                  "|---|" + "---|" * len(exports)]
        for col in columns:
            lines.append(f"| `{col}` | " + " | ".join(
                "{masked}/{total}".format(**r["columns"][col]) if col in r["columns"] else "—"
                for r in exports) + " |")

    lines += ["", "## Per file", ""]
    for ext in sorted(by_ext):
        lines += [f"### .{ext}", ""]
        for r in sorted(by_ext[ext], key=lambda r: r["file"]):
            head = f"#### `{r['file']}` — {r['status']}, {r['seconds']}s"
            if r["status"] == "ok":
                head += f", structure: {r['structure']} — masked text: `masked/{r['file']}`"
            lines += [head, ""]
            if r["status"] != "ok":
                lines += [f"⛔ {r.get('detail', '')[:200]}", ""]
                continue
            where = lambda i: f" (row {i['row']}, `{i['column']}`)" if "column" in i else ""  # noqa: E731
            for i in r["masked"]:
                lines.append(f"- ✅ {i['entity']} `{i['value']}` → `<{'>/<'.join(i['as'])}>`{where(i)}")
            for i in r["partial"]:
                lines.append(f"- ⚠️ {i['entity']} `{i['value']}` only partly masked "
                             f"(as `<{'>/<'.join(i['as'])}>`; still visible: `{i['left']}`){where(i)}")
            for i in r["missed"]:
                lines.append(f"- ❌ {i['entity']} `{i['value']}` **not masked**{where(i)}")
            for o in r["over_masked"]:
                lines.append(f"- 🔶 `{o['text']}` masked as `<{o['as']}>` (line {o['line']}: `{o['context']}`)")
            lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
