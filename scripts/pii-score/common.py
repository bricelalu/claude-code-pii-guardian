"""Shared helpers for the PII benchmark scripts. Stdlib only."""
import json
import urllib.request

# Cutoffs of the original single-guardrail config (presidio-mask + presidio-audit). The NER
# benchmark (span_score.py, docs/ner-model-benchmark.md) reports its "gateway thresholds"
# regime against these; the live config is manifests/21-litellm-config.yaml.
MASK_TIER = {
    "PERSON": 0.75,
    "EMAIL_ADDRESS": 0.6,
    "PHONE_NUMBER": 0.6,
    "CREDIT_CARD": 0.6,
    "IBAN_CODE": 0.6,
}
AUDIT_TIER = {
    "LOCATION": 0.35,
    "DATE_TIME": 0.35,
}
ANALYZE_FLOOR = 0.35  # lowest threshold across both guardrails


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end
