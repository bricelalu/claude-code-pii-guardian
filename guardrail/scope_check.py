#!/usr/bin/env python3
"""Black-box check of what code-guard scans in a real /v1/messages request (the path Claude Code
uses). A Luhn-valid test card is placed in one block at a time: code-guard blocks only when it
scans that block. Needs `task up` and a RunPod worker. Allowed requests reach Anthropic
(haiku, max_tokens 1), whatever it answers only proves the guardrail let the request through.

  python3 guardrail/scope_check.py [--gateway http://litellm.local:8080]
"""
import argparse
import json
import os
import urllib.error
import urllib.request

CARD = "4111 1111 1111 1111"
TOOLS = [{"name": n, "description": n, "input_schema": {"type": "object"}}
         for n in ("Read", "Edit", "Bash", "mcp__crm__list_customers")]


def request(system="You are terse.", user="Say ok", assistant_text="Checking.", tool_input="/tmp/notes",
            read_path="/exports/customers.csv", read_result="1\tid,name", edit_result="The file was updated.",
            mcp_result="[]"):
    return {
        "model": "claude-haiku-4-5-20251001", "max_tokens": 1, "system": system, "tools": TOOLS,
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
            ]},
        ],
    }


# (case, request, should code-guard scan it?)
CASES = [
    ("user text", request(user=f"My card is {CARD}"), True),
    ("Read .csv result", request(read_result=f"1\t42,{CARD}"), True),
    ("Read .py result", request(read_path="/src/billing.py", read_result=f"1\tCARD = '{CARD}'"), False),
    ("MCP JSON result", request(mcp_result=json.dumps({"content": f"| id | card |\n| 1 | {CARD} |"})), True),
    ("Edit tool_result", request(edit_result=f"1\tCARD = '{CARD}'"), False),
    ("tool_use input", request(tool_input=f"'{CARD}'"), False),
    ("system prompt", request(system=f"Card on file: {CARD}"), False),
    ("assistant text", request(assistant_text=f"The card is {CARD}."), False),
]


def blocked(gateway, key, body):
    req = urllib.request.Request(f"{gateway}/v1/messages", data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return False, resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if e.code == 404 or e.code >= 500:  # never reached the guardrail: no verdict possible
            raise SystemExit(f"HTTP {e.code} from {gateway}: {text[:200]}")
        return "Blocked by code-guard" in text, e.code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://litellm.local:8080"))
    args = ap.parse_args()
    key = os.environ["LITELLM_MASTER_KEY"]
    failures = 0
    for name, body, scanned in CASES:
        was_blocked, status = blocked(args.gateway, key, body)
        ok = was_blocked == scanned
        failures += not ok
        print(f"{'✅' if ok else '❌'} {name:<18} expected {'scanned' if scanned else 'untouched':<9} "
              f"got {'blocked' if was_blocked else 'passed'} (HTTP {status})")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
