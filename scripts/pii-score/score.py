#!/usr/bin/env python3
"""Score Presidio's detection of one rendered export against its answer key.

Talks directly to Presidio Analyzer/Anonymizer (not through LiteLLM or a real
Anthropic call) — see the header comment in run.sh for why. Stdlib only, no
pip installs, consistent with the project's supply-chain stance.

Usage: score.py <export-file> <truth.json> <analyzer-url> <anonymizer-url> <masked-out-file>
"""
import json
import sys
import urllib.request

# Mirrors manifests/21-litellm-config.yaml's "presidio-mask" guardrail
# (mode: pre_call — this is the one that actually mutates the request).
MASK_TIER = {
    "PERSON": 0.75,
    "EMAIL_ADDRESS": 0.6,
    "PHONE_NUMBER": 0.6,
    "CREDIT_CARD": 0.6,
    "IBAN_CODE": 0.6,
}
# "presidio-audit" guardrail (mode: logging_only — detected and logged, but
# the request body is never changed).
AUDIT_TIER = {
    "LOCATION": 0.35,
    "DATE_TIME": 0.35,
}
ANALYZE_FLOOR = 0.35  # lowest threshold configured across both guardrails


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def locate_spans(text, truth_rows):
    """Find each seeded value's exact offset in this rendering. Raises if the
    dataset invariant (unique, non-substring values) doesn't hold here."""
    spans = []
    for row in truth_rows:
        value = row["value"]
        first = text.find(value)
        if first == -1:
            raise SystemExit(f"seed value not found in export: {value!r}")
        if text.find(value, first + 1) != -1:
            raise SystemExit(f"seed value is not unique in export — invariant violated: {value!r}")
        spans.append({**row, "start": first, "end": first + len(value)})
    return spans


def classify(spans, analyzer_results):
    buckets = {
        "true_positive": [], "miss_under_threshold": [], "miss_type_confusion": [],
        "miss_not_detected": [], "false_positive": [], "true_negative": [],
        "decoy_audit_logged": [], "audit_detected": [], "audit_missed": [],
    }

    for span in spans:
        hits = [r for r in analyzer_results if overlaps(span["start"], span["end"], r["start"], r["end"])]
        hits_above_floor = [r for r in hits if r["score"] >= ANALYZE_FLOOR]

        if span["category"] == "decoy":
            mask_hits = [r for r in hits if r["entity_type"] in MASK_TIER and r["score"] >= MASK_TIER[r["entity_type"]]]
            audit_hits = [r for r in hits if r["entity_type"] in AUDIT_TIER and r["score"] >= AUDIT_TIER[r["entity_type"]]]
            bucket = "false_positive" if mask_hits else "true_negative"
            buckets[bucket].append({**span, "hits": mask_hits})
            if audit_hits:
                buckets["decoy_audit_logged"].append({**span, "hits": audit_hits})
            continue

        if span["category"] == "audit":
            threshold = AUDIT_TIER[span["expected_entity"]]
            detected = [r for r in hits if r["entity_type"] == span["expected_entity"] and r["score"] >= threshold]
            bucket = "audit_detected" if detected else "audit_missed"
            buckets[bucket].append({**span, "hits": detected})
            continue

        # category in (mask, block): must be masked/blocked to be "caught".
        threshold = MASK_TIER[span["expected_entity"]]
        correct_type_hits = [r for r in hits_above_floor if r["entity_type"] == span["expected_entity"]]
        # Only a hit on an entity type the acting guardrail actually knows
        # about (MASK_TIER) would ever cause a real redaction. A hit on some
        # other type (URL, NRP, ...) is not "detected under a different
        # label" — the gateway would forward this PII untouched, same as no
        # detection at all — so it belongs in miss_not_detected, not
        # miss_type_confusion.
        other_configured_hits = [
            r for r in hits_above_floor
            if r["entity_type"] != span["expected_entity"] and r["entity_type"] in MASK_TIER
        ]
        if any(r["score"] >= threshold for r in correct_type_hits):
            buckets["true_positive"].append({**span, "hits": correct_type_hits})
        elif correct_type_hits:
            buckets["miss_under_threshold"].append({**span, "hits": correct_type_hits})
        elif other_configured_hits:
            buckets["miss_type_confusion"].append({**span, "hits": other_configured_hits})
        else:
            buckets["miss_not_detected"].append({**span, "hits": []})

    return buckets


def fmt_hits(hits):
    return ", ".join(f"{h['entity_type']}@{h['score']:.2f}" for h in hits) or "(none)"


def report(buckets):
    tp = len(buckets["true_positive"])
    misses = buckets["miss_under_threshold"] + buckets["miss_type_confusion"] + buckets["miss_not_detected"]
    fp, tn = len(buckets["false_positive"]), len(buckets["true_negative"])
    ad, am = len(buckets["audit_detected"]), len(buckets["audit_missed"])

    masking_ratio = tp / (tp + len(misses)) if (tp + len(misses)) else float("nan")
    fp_ratio = fp / (fp + tn) if (fp + tn) else float("nan")
    audit_ratio = ad / (ad + am) if (ad + am) else float("nan")

    print(f"masking_ratio         = {masking_ratio:.0%}  "
          f"(masked={tp}, missed={len(misses)} "
          f"[under_threshold={len(buckets['miss_under_threshold'])} "
          f"type_confusion={len(buckets['miss_type_confusion'])} "
          f"not_detected={len(buckets['miss_not_detected'])}])")
    print(f"false_positive_ratio  = {fp_ratio:.0%}  (flagged={fp}, correctly ignored={tn})")
    print(f"audit_detection_ratio = {audit_ratio:.0%}  (detected={ad}, missed={am})  "
          f"[logging_only — never actually masked]")
    if buckets["decoy_audit_logged"]:
        print(f"decoys also audit-logged (not masked, informational): {len(buckets['decoy_audit_logged'])}")

    for label, key in [
        ("type confusion (detected as a different entity type — counts as a miss)", "miss_type_confusion"),
        ("under threshold (right entity type, score too low to trigger the guardrail)", "miss_under_threshold"),
        ("not detected at all", "miss_not_detected"),
        ("false positives (safe decoys incorrectly flagged)", "false_positive"),
    ]:
        rows = buckets[key]
        if not rows:
            continue
        print(f"\n{label}:")
        for row in rows:
            tag = row.get("expected_entity") or "decoy"
            print(f"  [{tag}] {row['value']!r} -> {fmt_hits(row['hits'])}")


def main():
    export_path, truth_path, analyzer_url, anonymizer_url, masked_out_path = sys.argv[1:6]

    with open(export_path, "r", encoding="utf-8") as f:
        text = f.read()
    with open(truth_path, "r", encoding="utf-8") as f:
        truth_rows = json.load(f)

    spans = locate_spans(text, truth_rows)

    analyzer_results = http_post_json(f"{analyzer_url}/analyze", {
        "text": text,
        "language": "en",
        "score_threshold": ANALYZE_FLOOR,
    })

    buckets = classify(spans, analyzer_results)
    report(buckets)

    anonymized = http_post_json(f"{anonymizer_url}/anonymize", {
        "text": text,
        "analyzer_results": [
            {"start": r["start"], "end": r["end"], "score": r["score"], "entity_type": r["entity_type"]}
            for r in analyzer_results
        ],
    })
    with open(masked_out_path, "w", encoding="utf-8") as f:
        f.write(anonymized["text"])


if __name__ == "__main__":
    main()
