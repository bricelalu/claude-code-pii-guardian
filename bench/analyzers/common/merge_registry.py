"""Append a variant's recognizer entries to Presidio's own default registry YAML.

Keeps every stock pattern recognizer (email, card, IBAN, phone, ...) exactly as
shipped, so a variant only differs from the baseline in its NER component.

Usage: python merge_registry.py <default.yaml> <extra.yaml> <out.yaml>
"""
import sys

import yaml

default_path, extra_path, out_path = sys.argv[1:4]
with open(default_path) as f:
    conf = yaml.safe_load(f)
with open(extra_path) as f:
    extra = yaml.safe_load(f)

added = extra.pop("recognizers", [])
names = {r["name"] for r in added}
conf["recognizers"] = [r for r in conf["recognizers"] if r["name"] not in names] + added
conf.update(extra)
with open(out_path, "w") as f:
    yaml.safe_dump(conf, f, sort_keys=False)
