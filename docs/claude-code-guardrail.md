# code-guard: masking PII in Claude Code traffic without breaking the developer's tools

*2026-09-26 · LiteLLM v1.102.1 · GLiNER2 on RunPod (RTX A4500, EU-RO-1) · Claude Code 2.1.283*

## Executive summary

**Customer PII in exports and tool output is masked before it reaches Anthropic, and Claude Code
still edits code as well as it does without the gateway.**

| | Result |
|---|---|
| Customer exports (50 rows: CSV 12 KB, Markdown 18 KB, minified JSON 22 KB) | **100% masked** in all three formats (names 100/100, places 150/150, email/phone/IBAN/IP 200/200), 0 safe cells masked |
| Generated source files (24 files, 6 languages × FR/EN/ES/IT) | 0 code tokens masked, 24/24 still compile or parse |
| `claude -p` tasks through the gateway vs direct | Same success on code tasks (2/2 each); editing a CSV cell fails by design |
| Which parts of a `/v1/messages` request get scanned | Verified live, 8/8 cases (user text, CSV Read, MCP JSON scanned; code Read, Edit result, `tool_use`, system prompt, Claude's replies untouched) |
| Time per 12 KB export through the gateway | 1.7 s first time, 0.47 s once cached |

Key decisions, each backed by a measurement below:

- **One custom guardrail** ([`guardrail/code_guard.py`](../guardrail/code_guard.py)) replaces
  LiteLLM's built-in presidio and regex guardrails. The built-ins can't tell which tool produced a
  result, so they can't spare code files, Edit results or paths.
- **Code files reach Claude unmasked; data files stay masked.** Masking a file Claude later edits
  breaks the edit, or worse, writes `<PERSON>` into the file.
- **Detection:** GLiNER2 (names, places, cards) plus regexes (email, phone, IBAN, IP) in one pass
  over the original text.

**Known limits:**
- Editing PII cells of a data file (CSV, JSON, Markdown…) through the gateway doesn't work: Claude
  only sees the masked copy.
- PII inside code files (author lines, test fixtures) reaches Anthropic. That's the price of
  editable code. The same is now true of a notebook's own cells (`.ipynb` is treated as code, like
  the file NotebookEdit writes back to) — including any customer rows a notebook's output cells
  happen to hold, which reach Anthropic unmasked too.
- A `Bash` result can't be tied to a file (the command is arbitrary shell), so it stays masked even
  when it dumps a code file (`cat file.py`), unlike `Read`/`Grep`/`Glob` of that same file.
- Java Javadoc `@author` names (5 of 12) and one Terraform place (*Sheffield*) are missed.
- A slash no longer exempts a token, so `Lyon/Paris` in prose is masked. The price is
  `Europe/Paris`: it has the same shape, so a tz zone keeps its continent and loses its city. A
  *file* named after a place (`--config=Paris/app.yml`) stays readable, since breaking a path is
  the worse failure.
- Each Claude Code task takes about 1.5–2× longer through the gateway.

## What gets masked

The gateway sits between Claude Code on developer laptops and the Anthropic API.

| Part of the `/v1/messages` request | Masked? | Why |
|---|---|---|
| What the developer types (user text blocks) | ✅ | Prompts, pasted data |
| MCP tool results (JSON, Markdown tables, CSV) | ✅ | Customer data from other systems |
| Bash and other tool results (no file can be attributed to them) | ✅ | Command output; see Known limits |
| Grep / Glob lines attributed to a data file | ✅ | Search hits; a hit inside `customers.csv` is still an export |
| Read of a data file (`.csv`, `.tsv`, `.json`, `.jsonl`, `.md`, `.txt`, `.log`, `.xml`, `.sql`) or of an unknown type | ✅ | Exports and dumps. List: `data_extensions` in the config |
| Read of a code file (`.py`, `.ts`, `.go`, `.tf`, `.yaml`, `Dockerfile`…) or of a notebook (`.ipynb`) | ❌ | Claude quotes it exactly in its Edits |
| Read of a `file_path` a Write / Edit / MultiEdit / NotebookEdit targets anywhere in the same request | ❌ | The developer is editing that exact file right now, whatever its extension — a k8s manifest or a `.json` export being patched must stay readable (pii-guardian-qdu.5) |
| Grep / Glob lines attributed to a code file | ❌ | Same reason: Claude quotes the line back in its Edit |
| Write / Edit / MultiEdit / NotebookEdit results | ❌ | They echo the file being edited |
| `tool_use` input (paths, commands, `old_string`) | ❌ | Claude's tool calls must reach the tools unchanged |
| System prompt, Claude's own replies | ❌ | Written by Claude Code / Claude |
| Paths and URLs (`/Users/<name>/…`, `https://…`, `Europe/Paris`) | ❌ | A masked path breaks every tool call that reuses it |

**How a file counts as code.** First, the guardrail checks whether any `Write`/`Edit`/`MultiEdit`/
`NotebookEdit` `tool_use` anywhere in the same request targets the exact same `file_path` string as
the Read (no basename or path-normalisation matching, since a loose match is a leak surface). If
so, the Read is code, whatever its extension: the request itself shows the developer is editing
that file right now. Otherwise, the guardrail finds the `tool_use` that produced the result. For
`Read`/`NotebookRead` that's the whole result, exempted by `file_path`/`notebook_path`: `pygments`
is asked whether it knows the language (`find_lexer_class_for_filename`), and a `.ipynb` counts as
code even without a lexer for it, since `NotebookEdit` is what edits it back. `pygments` ships with
LiteLLM's proxy image (`litellm[proxy]` → `rich` → `pygments`) and knows 500+ formats. Data
extensions (`data_extensions`) are masked even though `pygments` knows them; the `file_path` match
above is what lets an actually-edited file of one of those extensions through, without widening the
list. If `pygments` is missing, every Read not covered by the `file_path` match is treated as data
and masked (a `.ipynb` still isn't, since that check needs no lexer).

For `Grep`/`Glob`, one result can mix hits from many files, so the exemption is per line: each line
is checked for a leading `path:line:text` (or a bare `path`, `files_with_matches`/`Glob`) and the
extracted path goes through the same `is_code_file` check as `Read`. A line whose shape doesn't
name a file stays masked — the fail-safe default.

LiteLLM's own `block_code_execution` guardrail only detects fenced code blocks inside text, and
its `model_armor` file scanning maps MIME types of attachments. Neither identifies code files.

## Detection

- **Names, places, cards:** Presidio + GLiNER2 on the RunPod GPU. Cutoffs PERSON ≥ 0.85,
  LOCATION ≥ 0.96 ([NER benchmark](ner-model-benchmark.md)). A Luhn-valid card is masked as
  `<CREDIT_CARD>`. It used to **block** the request; that was dropped because Claude Code resends
  the whole conversation every turn, so one blocked test card in a tool result failed every later
  request until `/clear`. Blocking is still available per entity (`block_entities`), empty by default.
- **Email, phones, IBAN, IPv4/IPv6:** regexes in `REGEXES` (`code_guard.py`), in the same pass.
  Placeholders: `<PERSON>`, `<LOCATION>`, `[EMAIL_REDACTED]`, `[PHONE_FR_REDACTED]`…

Rules added after measuring real failures:

| Rule | Failure it fixes |
|---|---|
| NER and regexes both see the original text | With the regex guardrail running first, `Maintainer: Rosalind Kerr <[EMAIL_REDACTED]>` lowered the name's score: Terraform fell to 11/12 names and 6/8 places, CSV to 95/100, Markdown to 99/100 |
| A NER hit inside a regex hit defers to it | 3 of 50 generated IBANs contain a Luhn-valid 16-digit run: Presidio called it a card and blocked the whole export |
| A PERSON hit made only of role words and numbers is dropped | GLiNER2 scores "customer 2" 0.84, "user 42" 0.89, "The customer" 0.96, "reviewer 2" 0.98 as PERSON. The prompt "customer 2 should be on pro" reached Claude as "`<PERSON>` should be on pro", and Claude asked which customer (0/2 task success). Real names, lowercase ones included, are still masked |
| JSON tool results: escaped strings are decoded and masked as documents of their own, then re-serialized | In MCP-style JSON (a CSV or table inside a `"content"` string), a phone right after an escaped `\n` was **not masked**, an email right after `\n` was masked **with** the `n` (`\[EMAIL_REDACTED]`, invalid JSON), and `Lucía` was unreadable to the model. JSON without escaped strings keeps its exact bytes |
| Private, loopback, link-local and CGNAT IPs, and public DNS resolvers, are not masked | Replaying real sessions: `kubectl get pods -o wide`, cluster IPs and log lines had their `10.x`/`172.18.x` addresses masked, which blocks debugging. Documentation ranges stay masked |
| Regex fences from a sweep over real code (below) | OIDs read as IPv4, `+3.142` and signed years as phones, PKCE verifiers as IBANs |
| IBANs are checksum-verified (mod 97) | Random uppercase tokens matched the IBAN shape |

## Checks

All in [`guardrail/`](../guardrail/). The live checks need `task up` and a RunPod worker.

| Check | What it proves | Result |
|---|---|---|
| `test_code_guard.py` (offline) | Masking rules, request scope, JSON decoding, code-file detection, cache | 43/43 (1 expected failure, see below; 37 + 6 skipped without `pygments`) |
| `scope_check.py` (live) | Which blocks of a real `/v1/messages` request reach Anthropic masked: a unique test email in one block at a time, and Haiku is asked to list every email it can see | 8/8 |
| `regex_sweep.py` (offline) | Regex false positives on real code | 14,594 files (240 MB, the LiteLLM image's site-packages): 5,152 matches before fencing, 2,227 after, mostly real emails/IPs in package metadata and docs |
| `replay_sessions.py` (local only) | Regexes on your own `~/.claude/projects` transcripts | 26 sessions, 914 distinct scanned blocks (1.2 MB), 63 would change; found the infrastructure-IP issue. Report in `.pii-score-out/` (gitignored) |
| `claude_ab.py` (live) | Can Claude Code still do its job through the gateway? | See below |
| `task pii-score`, `task pii-bench-gateway` (live) | Masking ratio and false positives on exports and generated code | See the [README](../README.md#pii-detection-scoring-task-pii-score) |

#### The 24-file corpus, through the request path

`docs/ner-model-benchmark.md` reports 0 code tokens masked over `bench/corpus`, but it measures
those files as raw text through `/guardrails/apply_guardrail`, which never reaches the scope
decision. `CodeCorpusRequestTest` measures the same 24 files the way a developer meets them: each
one echoed back inside a `tool_result`, once per tool that returns one, through `mask_request` —
the path `/v1/messages` takes.

| Tool that echoes the file | Files changed by masking |
|---|---|
| `Read` | 0/24 |
| `Grep` | 0/24 |
| `Bash` | 24/24 |

**pii-guardian-qdu.4** (this fix): a `Grep` hit is now attributed per line (`path:line:text`) to
the file it came from, the same `is_code_file` check `Read` uses, so it matches `Read`'s row. `Bash`
stays masked: its result can't be tied to a file (an arbitrary command, not a path), so the same
`# Maintainer:` email trips it on every file — a known limit, not a bug, recorded above. Offline,
the figures come from the real regexes against the fake analyzer, and every corpus file carries a
real email, so the gap in the `Bash` row is the missing attribution and not the analyzer. With the
live model the names and cities go too.

### Claude Code A/B (`claude_ab.py`)

The same `claude -p` tasks (Haiku 4.5) on a small CRM repo full of customer PII
(`crm/customers.py`, `data/customers.csv`, a test file), directly and through the gateway, 2 runs
each. Each run starts from a fresh copy and is checked for task success, placeholders written
into files, and failed Edit calls.

| Task | Direct | Gateway, every Read masked | Gateway, current rules |
|---|---|---|---|
| Fix a bug on a line without PII | 2/2 | 2/2 | 2/2 |
| Change one customer's plan in `customers.py` | 2/2 | 1/2, 4 failed Edits | **2/2** |
| Add a function and its test | 2/2 | **0/2, `<PERSON>`/`<LOCATION>` written into `tests/test_crm.py`** | **2/2** |
| Fix one cell in `customers.csv` | 2/2 | 1/2, 13 failed Edits | 0/2 (by design) |

Why masked files can't be edited: Claude quotes the lines it saw in `old_string`, so an Edit on a
masked line carries `<PERSON>`, which isn't on disk ("String to replace not found"). After a few
failures Claude may rewrite the whole file from its masked view.

The only way to edit data files through the gateway too is **reversible masking**: stable tokens
(`<PERSON_a3f9>`) swapped back to the real values in Claude's replies, including inside Edit
calls. It is not built. Claude's replies stream in small pieces, so a token can be split across
two of them.

## Latency

Measured on the 12 KB CSV export, warm GPU:

| Path | Time |
|---|---|
| Tiny text, direct to RunPod from the laptop (fixed round trip) | 0.30 s |
| Tiny text through the gateway | 0.39–0.41 s |
| 12 KB, direct to RunPod (≈ 0.9 s of GLiNER2 inference) | 1.2–1.5 s |
| 12 KB through the gateway, first time | 1.7 s (2.0 s before the proxy kept connections alive) |
| 12 KB through the gateway, same text again (cached) | 0.47 s |

What was changed, and what was tried and dropped:

| Lever | Effect | Status |
|---|---|---|
| `::1 litellm.local` in `/etc/hosts` | **5.6–7.8 s → 1.9–2.5 s per export.** On macOS, the IPv6 lookup of a `.local` name goes to mDNS and times out after 5 s; Claude Code (Node) doesn't cache DNS, so every request paid it | Operator setup (README) |
| Cache NER results per text block | Claude Code resends the whole conversation every turn; only new blocks reach the GPU | Done |
| Keepalive from the nginx proxy to RunPod | 12 KB: 2.0 s → 1.7 s. `proxy_pass` with a variable opened a new TCP + TLS connection per call; now an `upstream` with `keepalive` and `resolve` | Done |
| At most 4 analyzer calls in flight | 26 blocks at once: 5.1 s and 2 × HTTP 502; 4 in flight: 2.8 s, 0 errors (the worker runs 4 gunicorn threads) | Done |
| Wait out cold starts | The RunPod load balancer answers 502/503/504 while a worker starts; the guardrail retries for up to about 3 minutes instead of failing the request | Done |
| Split documents into chunks | 4 KB chunks, 4 in flight: −20–25% on CSV/Markdown, 0 on JSON; 1–2 KB chunks were slower (one round trip each). GLiNER2 already chunks internally | Dropped |
| Keep 1 worker warm during working hours | Removes the 80–100 s cold start, the delay developers would feel most | Not done: about $0.58/h, ~$100/month for 8 h × 22 days |
| 16-bit model or faster GPU tier | Cuts part of the 0.9 s inference | Not done: needs an image rebuild |

Per-task wall time in the A/B (second run, cache warm): the plan change took 17.7 s through the
gateway vs 10 s direct; fixing the bug 50 s vs 25 s.

## How to run

```bash
python3 guardrail/test_code_guard.py                     # offline
python3 guardrail/regex_sweep.py <dir>                   # offline, any source tree
python3 guardrail/replay_sessions.py                     # offline, your own sessions
# live: task up, RunPod endpoint workers.max >= 1, LITELLM_MASTER_KEY in the environment
python3 guardrail/scope_check.py
python3 guardrail/claude_ab.py --reps 2                  # calls Anthropic (Haiku), both arms
```

Deploying a change to `code_guard.py`: `task reload-config` recreates the `litellm-guardrail`
ConfigMap from the file and restarts LiteLLM.
