#!/usr/bin/env python3
"""Black-box check of what code-guard masks in a real /v1/messages request (the path Claude Code
uses). A unique test email is placed in one block at a time, and Haiku (temperature 0) is asked
to list every email it can see: if it can repeat the probe, that block reached Anthropic unmasked;
if it can't, code-guard scanned (masked) it. Emails are masked by a regex, so the verdict doesn't
depend on the NER model's scores. Needs `task up` and a RunPod worker (code-guard fails closed
without it). A few cents of Haiku per run.

  python3 guardrail/scope_check.py [--gateway http://litellm.local:8080]
"""
import argparse
import json
import os
import urllib.error
import urllib.request

PROBE = "probe.7f3a@acme-probe.fr"
QUESTION = ("Audit task: list every email address that appears anywhere above (system prompt, messages, "
            "tool calls, tool results), copied exactly, one per line. If there is none, reply NONE. "
            "Output nothing else.")
TOOLS = [{"name": n, "description": n, "input_schema": {"type": "object"}}
         for n in ("Read", "Edit", "Bash", "mcp__crm__list_customers")]


def request(system="You are a terse assistant.", user="Summarize the files.", assistant_text="Checking.",
            tool_input="/tmp/notes", read_path="/exports/customers.csv", read_result="1\tid,name",
            edit_result="The file was updated.", mcp_result="[]"):
    return {
        "model": "claude-haiku-4-5-20251001", "max_tokens": 100, "temperature": 0, "system": system,
        "tools": TOOLS,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": assistant_text},
                {"type": "tool_use", "id": "toolu_r", "name": "Read", "input": {"file_path": read_path}},
                {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": f"ls {tool_input}"}},
                {"type": "tool_use", "id": "toolu_m", "name": "mcp__crm__list_customers", "input": {}},
                {"type": "tool_use", "id": "toolu_e", "name": "Edit",
                 "input": {"file_path": "/src/app.py", "old_string": "a", "new_string": "b"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_r", "content": read_result},
                {"type": "tool_result", "tool_use_id": "toolu_e", "content": edit_result},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": "notes"},
                {"type": "tool_result", "tool_use_id": "toolu_m", "content": [{"type": "text", "text": mcp_result}]},
                {"type": "text", "text": QUESTION},
            ]},
        ],
    }


# (case, request, should code-guard mask it?)
CASES = [
    ("user text", request(user=f"Summarize the files, then email me at {PROBE}."), True),
    ("Read .csv result", request(read_result=f"1\tid,email\n2\t42,{PROBE}"), True),
    ("Read .py result", request(read_path="/src/billing.py", read_result=f"1\tOWNER = '{PROBE}'"), False),
    ("MCP JSON result", request(mcp_result=json.dumps({"content": f"| id | email |\n| 1 | {PROBE} |"})), True),
    ("Edit tool_result", request(edit_result=f"1\tOWNER = '{PROBE}'"), False),
    ("tool_use input", request(tool_input=f"/tmp/{PROBE}"), False),
    ("system prompt", request(system=f"You are a terse assistant. Support contact: {PROBE}."), False),
    ("assistant text", request(assistant_text=f"Checking with {PROBE}."), False),
]


def probe_seen(gateway, key, body):
    """True if the model could read the probe, i.e. the block reached Anthropic unmasked."""
    req = urllib.request.Request(f"{gateway}/v1/messages", data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:  # blocked, analyzer down, bad request: no verdict possible
        raise SystemExit(f"HTTP {e.code} from {gateway}: {e.read().decode(errors='replace')[:300]}")
    text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
    return PROBE in text, text.strip().replace("\n", " | ")[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://litellm.local:8080"))
    args = ap.parse_args()
    key = os.environ["LITELLM_MASTER_KEY"]
    failures = 0
    for name, body, masked in CASES:
        seen, reply = probe_seen(args.gateway, key, body)
        ok = seen != masked
        failures += not ok
        print(f"{'✅' if ok else '❌'} {name:<18} expected {'masked' if masked else 'untouched':<9} "
              f"got {'untouched' if seen else 'masked':<9} (model: {reply})")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
