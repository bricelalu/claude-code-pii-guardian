#!/usr/bin/env python3
"""Span-level benchmark of Presidio analyzer variants on the generated code corpus.

  span_score.py add-exports --exports DIR --corpus DIR
  span_score.py collect --variant NAME --analyzer URL --corpus DIR [--per-file-language] --out RAW.json
  span_score.py report RAW.json [RAW.json ...] --corpus DIR --masked-dir DIR [--out RESULTS.json]

Scores PERSON and LOCATION only: the NER-driven entities that are hardest to mask.
`report` also masks each file with each model's PERSON/LOCATION detections and
checks the file still parses (CSV/Markdown/JSON) or compiles (code).

`collect` calls /analyze once per corpus file at the lowest configured floor and
stores every raw detection, so `report` can re-threshold offline.

Matching rule (same as score.py's classify): a detection is a true positive if it
overlaps a true span of the same entity type; any other detection is a false
positive; a true span with no same-type detection is a false negative, even if a
detection of another type covers it. Several detections on one true span count
as one true positive.
Stdlib only.
"""
import argparse
import csv
import io
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bench" / "corpus"))
from generate import syntax_check  # noqa: E402
from score import ANALYZE_FLOOR, AUDIT_TIER, MASK_TIER, http_post_json, locate_spans, overlaps  # noqa: E402

GATEWAY_THRESHOLDS = {**MASK_TIER, **AUDIT_TIER}
FOCUS = ["PERSON", "LOCATION"]


def context_line(text, start):
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line = text[line_start: line_end if line_end != -1 else len(text)].strip()
    return line[:110], text.count("\n", 0, start) + 1


def add_exports(args):
    """Copy the pii-score CSV/Markdown/JSON exports into the corpus, with their PERSON/LOCATION spans."""
    src, corpus = Path(args.exports), Path(args.corpus)
    rows = [r for r in json.loads((src / "truth.json").read_text(encoding="utf-8"))
            if r["expected_entity"] in FOCUS]
    truth = json.loads((corpus / "truth.json").read_text(encoding="utf-8"))
    (corpus / "exports").mkdir(exist_ok=True)
    for fmt in ("csv", "md", "json"):
        text = (src / f"export.{fmt}").read_text(encoding="utf-8")
        (corpus / "exports" / f"export.{fmt}").write_text(text, encoding="utf-8")
        truth.append({"file": f"exports/export.{fmt}", "lang": "exports", "spans": [
            {"start": s["start"], "end": s["end"], "entity": s["expected_entity"], "value": s["value"]}
            for s in locate_spans(text, rows)]})
    (corpus / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")


def collect(args):
    corpus = Path(args.corpus)
    truth = json.loads((corpus / "truth.json").read_text(encoding="utf-8"))
    files = []
    for entry in truth:
        text = (corpus / entry["file"]).read_text(encoding="utf-8")
        per_file = args.per_file_language and entry["lang"] != "exports"
        language = entry["lang"] if per_file else "en"
        t0 = time.perf_counter()
        dets = http_post_json(f"{args.analyzer}/analyze", {
            "text": text, "language": language, "score_threshold": ANALYZE_FLOOR,
        })
        latency_ms = (time.perf_counter() - t0) * 1000
        detections = []
        for d in dets:
            ctx, line = context_line(text, d["start"])
            detections.append({"start": d["start"], "end": d["end"], "entity": d["entity_type"],
                               "score": d["score"], "text": text[d["start"]:d["end"]],
                               "line": line, "context": ctx})
        files.append({"file": entry["file"], "lang": entry["lang"],
                      "ext": Path(entry["file"]).suffix.lstrip("."),
                      "truth": entry["spans"], "detections": detections, "latency_ms": latency_ms})
        print(f"  {entry['file']}: {len(detections)} detections, {latency_ms:.0f} ms", file=sys.stderr)
    Path(args.out).write_text(json.dumps({
        "variant": args.variant,
        "language_mode": "per-file (oracle)" if args.per_file_language else "en",
        "files": files,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def score_files(files, entity, threshold):
    tp = fp = fn = 0
    for f in files:
        truths = [t for t in f["truth"] if t["entity"] == entity]
        dets = [d for d in f["detections"] if d["entity"] == entity and d["score"] >= threshold]
        for t in truths:
            if any(overlaps(t["start"], t["end"], d["start"], d["end"]) for d in dets):
                tp += 1
            else:
                fn += 1
        for d in dets:
            if not any(overlaps(t["start"], t["end"], d["start"], d["end"]) for t in truths):
                fp += 1
    return tp, fp, fn


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


def best_threshold(files, entity):
    candidates = sorted({d["score"] for f in files for d in f["detections"]
                         if d["entity"] == entity and d["score"] >= ANALYZE_FLOOR} | {ANALYZE_FLOOR})
    best = (ANALYZE_FLOOR, -1.0)
    for t in candidates:
        f1 = prf(*score_files(files, entity, t))[2]
        if f1 >= best[1]:  # ties go to the higher threshold (fewer false positives)
            best = (t, f1)
    return best[0]


def false_positive_tokens(files, entity, threshold):
    counts, examples = Counter(), {}
    for f in files:
        truths = [t for t in f["truth"] if t["entity"] == entity]
        for d in f["detections"]:
            if d["entity"] != entity or d["score"] < threshold:
                continue
            if any(overlaps(t["start"], t["end"], d["start"], d["end"]) for t in truths):
                continue
            counts[d["text"]] += 1
            examples.setdefault(d["text"], f"{f['file']}:{d['line']}  {d['context']}")
    return counts, examples


def mask(text, dets):
    """Replace each detection with <ENTITY>, merging overlaps (Presidio's default 'replace')."""
    merged = []
    for s, e, ent in sorted((d["start"], d["end"], d["entity"]) for d in dets):
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2])
        else:
            merged.append((s, e, ent))
    for s, e, ent in reversed(merged):
        text = text[:s] + f"<{ent}>" + text[e:]
    return text, merged


def _csv_error(original, masked):
    a, b = list(csv.reader(io.StringIO(original))), list(csv.reader(io.StringIO(masked)))
    if len(a) != len(b):
        return f"{len(a) - 1} rows became {len(b) - 1}"
    for ra, rb in zip(a, b):
        if len(ra) != len(rb) or ra[0] != rb[0]:
            return f"row {ra[0]} became {rb!r}"
    return None


def _md_error(original, masked):
    a, b = original.splitlines(), masked.splitlines()
    if len(a) != len(b):
        return f"{len(a)} lines became {len(b)}"
    for la, lb in zip(a, b):
        if la.count("|") != lb.count("|") or la.split("|")[1] != lb.split("|")[1]:
            return f"line {la.split('|')[1].strip()!r} became {lb.strip()[:60]!r}"
    return None


def _json_error(original, masked):
    try:
        b = json.loads(masked)
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e.msg}"
    a = json.loads(original)
    ids_a, ids_b = [r["id"] for r in a], [r.get("id") for r in b]
    if ids_a != ids_b:
        return f"records lost: ids {sorted(set(ids_a) - set(ids_b))}"
    return None


def _ts_error(original, merged):
    """No TypeScript compiler here: a masked span is safe only inside a string literal or comment."""
    for s, e, _ in merged:
        line_start = original.rfind("\n", 0, s) + 1
        before = original[line_start:s]
        in_comment = re.match(r"\s*(//|/?\*)", before) is not None
        in_string = before.count('"') % 2 == 1 and "\n" not in original[s:e] and '"' not in original[s:e]
        if not (in_comment or in_string):
            return f"(heuristic) code token masked: {original[s:e]!r}"
    return None


def structure_error(original, masked, merged, ext, out_path):
    if ext == "csv":
        return _csv_error(original, masked)
    if ext == "md":
        return _md_error(original, masked)
    if ext == "json":
        return _json_error(original, masked)
    if ext == "ts":
        return _ts_error(original, merged)
    err = syntax_check(out_path)
    if not err:
        return None
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace(str(out_path), out_path.name).strip()
             for ln in err.splitlines()]
    lines = [ln for ln in lines if ln]
    first = next((ln for ln in lines if "rror" in ln), lines[0] if lines else "compile error")
    return f"does not compile: {first[:100]}"


def pct(x):
    return f"{x * 100:5.1f}%"


def table(headers, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    print(line(headers))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))


def report(args):
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.raw]
    langs = sorted({f["lang"] for r in runs for f in r["files"]})
    exts = sorted({f["ext"] for r in runs for f in r["files"]})
    label = {r["variant"]: r["variant"] + (" *" if r["language_mode"] != "en" else "") for r in runs}
    results = {}

    print("\n* = told each file's language up front: an upper bound the gateway (always 'en') can't reach.")
    for regime in ("gateway", "best-F1"):
        print(f"\n=== Precision / recall / F1 by natural language (exports = CSV/MD/JSON seed exports) — thresholds: {regime} ===")
        for entity in FOCUS:
            rows = []
            for r in runs:
                th = GATEWAY_THRESHOLDS[entity] if regime == "gateway" else best_threshold(r["files"], entity)
                row = [label[r["variant"]], f"{th:.2f}"]
                for lang in langs + ["all"]:
                    files = [f for f in r["files"] if lang == "all" or f["lang"] == lang]
                    p, rc, f1 = prf(*score_files(files, entity, th))
                    row.append(f"{pct(p)}/{pct(rc)}/{f1:.2f}")
                    results.setdefault(r["variant"], {}).setdefault(regime, {}).setdefault(entity, {})[lang] = {
                        "threshold": th, "precision": p, "recall": rc, "f1": f1}
                rows.append(row)
            print(f"\n{entity}  (P / R / F1)")
            table(["model", "thr"] + langs + ["all"], rows)

    print("\n=== False positives (PERSON + LOCATION) and recall by file type — gateway thresholds ===")
    rows = []
    for r in runs:
        row = [label[r["variant"]]]
        for ext in exts:
            files = [f for f in r["files"] if f["ext"] == ext]
            fp = tp = fn = 0
            for entity in FOCUS:
                a, b, c = score_files(files, entity, GATEWAY_THRESHOLDS[entity])
                tp, fp, fn = tp + a, fp + b, fn + c
            row.append(f"FP {fp:>2} · R {pct(tp / (tp + fn) if tp + fn else 0)}")
        rows.append(row)
    table(["model"] + exts, rows)

    print("\n=== Latency per /analyze call (CPU) ===")
    table(["model", "p50 ms", "max ms"], [
        [label[r["variant"]], f"{statistics.median(f['latency_ms'] for f in r['files']):.0f}",
         f"{max(f['latency_ms'] for f in r['files']):.0f}"] for r in runs])

    print("\n=== False-positive tokens — gateway thresholds (what each model mis-flags) ===")
    for r in runs:
        print(f"\n[{label[r['variant']]}]")
        any_fp = False
        for entity in FOCUS:
            counts, examples = false_positive_tokens(r["files"], entity, GATEWAY_THRESHOLDS[entity])
            if not counts:
                continue
            any_fp = True
            print(f"  {entity}: " + ", ".join(f"{tok!r} x{n}" for tok, n in counts.most_common(12)))
            for tok, _ in counts.most_common(5):
                print(f"      e.g. {examples[tok]}")
            results.setdefault(r["variant"], {}).setdefault("false_positive_tokens", {})[entity] = dict(counts)
        if not any_fp:
            print("  (none)")

    corpus, masked_dir = Path(args.corpus), Path(args.masked_dir)
    for regime in ("gateway", "best-F1"):
        print(f"\n=== Structure after masking PERSON + LOCATION — thresholds: {regime} ===")
        print("broken files / files, per format (CSV/MD/JSON must still parse with the same rows;"
              " code must still compile; ts = heuristic)")
        rows, failures = [], []
        for r in runs:
            th = {e: GATEWAY_THRESHOLDS[e] if regime == "gateway" else best_threshold(r["files"], e) for e in FOCUS}
            broken = Counter()
            for f in r["files"]:
                original = (corpus / f["file"]).read_text(encoding="utf-8")
                dets = [d for d in f["detections"] if d["entity"] in FOCUS and d["score"] >= th[d["entity"]]]
                masked, merged = mask(original, dets)
                out_path = masked_dir / regime / r["variant"] / f["file"]
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(masked, encoding="utf-8")
                err = structure_error(original, masked, merged, f["ext"], out_path)
                if err:
                    broken[f["ext"]] += 1
                    failures.append(f"  [{label[r['variant']]}] {f['file']}: {err}")
                results.setdefault(r["variant"], {}).setdefault("structure", {}).setdefault(regime, {})[f["file"]] = err
            total = Counter(f["ext"] for f in r["files"])
            rows.append([label[r["variant"]]] + [
                f"{broken[x]}/{total[x]}" + (" ok" if not broken[x] else "") for x in exts])
        table(["model"] + exts, rows)
        if failures:
            print("\n".join(failures))
        print(f"masked files: {masked_dir / regime}")

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--variant", required=True)
    c.add_argument("--analyzer", required=True)
    c.add_argument("--corpus", required=True)
    c.add_argument("--per-file-language", action="store_true")
    c.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("raw", nargs="+")
    r.add_argument("--corpus", required=True)
    r.add_argument("--masked-dir", required=True)
    r.add_argument("--out")
    e = sub.add_parser("add-exports")
    e.add_argument("--exports", required=True)
    e.add_argument("--corpus", required=True)
    args = ap.parse_args()
    {"collect": collect, "report": report, "add-exports": add_exports}[args.cmd](args)


if __name__ == "__main__":
    main()
