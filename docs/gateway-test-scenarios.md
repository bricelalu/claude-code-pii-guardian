# Gateway test scenarios: what Claude Code does all day, and what code-guard could break

Scenario families for an end-to-end benchmark of Claude Code **directly vs through the gateway**.
Public benchmarks cover the pure-coding family; every other family has to be built as custom
scenarios in [`guardrail/claude_ab.py`](../guardrail/claude_ab.py). The public-benchmark part is
filled in from [research/agentic-coding-benchmarks.md](research/agentic-coding-benchmarks.md).

**Status** says how much is known today:
- **measured**: reproduced in this repo (see [claude-code-guardrail.md](claude-code-guardrail.md));
- **by design**: follows from the current masking rules, not yet run as a scenario;
- **hypothesis**: plausible, to be confirmed by the scenario.

## Scenario families

| Scenario family | Examples | What could break | Status |
|---|---|---|---|
| Pure coding | Fix a bug, add a feature, refactor, write tests (SWE-bench-style tasks) | Nothing should break: code files aren't masked. Measures **overhead** (latency, turns) only | measured on 2 small tasks: 2/2, ~1.5–2× wall time |
| Code with PII fixtures | Tests with customer fixtures, `@author` lines, seed data in `.py`/`.ts`/`.go` | Nothing breaks, but the PII **reaches Anthropic** (code files aren't masked, by design) | by design |
| Data files | Fix a cell in a CSV/JSON/Markdown export; edit a SQL seed, `package.json` (author), `README.md` | Failed Edits (`old_string` holds `<PERSON>`), or placeholders written into the file | measured: CSV edit 0/2; `package.json`/`README.md` by design |
| **Database schema and migrations** | Add a column to `customers` (`firstname`, `lastname`, `zipcode`, `city`), write a migration, rename an index, review DDL | `.sql` counts as a data file, so migrations are masked on Read: Edits of lines with seed values fail. Column names (`firstname`, `lastname`, `city`) could be scored as PERSON/LOCATION and masked, breaking the DDL Claude writes | hypothesis |
| **Database performance analysis** | `EXPLAIN (ANALYZE, BUFFERS)` on slow queries, `pg_stat_statements` top queries, `pg_stat_user_tables`/`pg_stat_user_indexes`, index bloat, vacuum and lock analysis, `pg_stat_activity` | See the breakdown below: measurements masked as PII, real values in plans masked so Claude can't reproduce the query, a Luhn-valid `queryid` masked as a card | hypothesis |
| MCP tools | Query a CRM or DB through an MCP server, then act on the result (reply, fix a record) | Masking works, but Claude reasons on `<PERSON>`/`[EMAIL_REDACTED]` and can't act on a specific record | masked: measured; impact on the task: hypothesis |
| Bash output | `git log`/`git blame` (author emails), `kubectl get pods -o wide`, application logs, test runs | Debugging hampered by masked emails/IPs; test card numbers masked as `<CREDIT_CARD>` (they used to block every later request until `/clear`: fixed) | infra IPs and card lock fixed (measured) |
| Pasted PII in prompts | "Answer this customer email", a stack trace with emails, a support ticket | Claude can't refer to the masked value, or asks which one is meant | measured once ("customer 2" as PERSON, fixed) |
| Long sessions | 50+ turns, `/compact`, resumed sessions, several LiteLLM replicas | History cache misses (per process) raise latency; cache reset leaked names (issue `pii-guardian-3rk.1`) | measured (review) |
| Subagents and web fetch | Task tool, WebFetch/WebSearch results | Each is a separate request, scanned separately: extra latency | by design |
| Not scanned at all | Screenshots and PDFs read as images, attached files | PII in images **leaks**: the guardrail only scans text | by design |
| Infrastructure failures | Cold start after idle, analyzer down, burst of parallel requests | 1–3 min waits, blocked requests (fails closed), 502s | measured |
| **Identifier and version look-alikes** (false positives) | Bug reports pasting `pip freeze` / `show_versions()` output, `git clone git@github.com:…`, dates and IDs in code or logs | Non-PII masked: versions read as IPv4, SSH remotes as emails, dates as phones. Claude loses the dependency version, or runs a broken `git` command | found by regex scan of SWE-bench Verified (see below) |

## Database work in detail

A developer tuning a customer database sends Claude Code a mix of **real customer values**
(which must stay masked), **identifiers** (table, column, index names) and **measurements**
(timings, counts, sizes, IDs). The gateway must tell them apart.

| Output | Example | Should be masked? | Risk |
|---|---|---|---|
| Column and table names | `customers(firstname, lastname, zipcode, city)` | No | NER scores `lastname` or `city` as PERSON/LOCATION → broken DDL and SQL in Claude's answer |
| Filter values in query plans | `Filter: (city = 'Lyon'::text)`, `Index Cond: (lastname = 'DUPONT')` | Yes (real values) | Once masked, Claude can't rerun or rewrite the exact query: it has to use placeholders or parameters |
| `pg_stats.most_common_vals` | `{Paris,Lyon,Marseille}`, `{MARTIN,BERNARD}` | Yes | Masked correctly, but the skew analysis (which value is hot) becomes opaque |
| Query text | `pg_stat_statements.query` is normalized (`WHERE lastname = $1`), but `pg_stat_activity.query` and slow-query logs (`log_min_duration_statement`) hold literal values | Yes when literal | Real PII leaks if not detected; wrong masking if SQL keywords or identifiers are taken for names |
| Timings and counts | `actual time=0.012..1843.502 rows=1204981 loops=1`, `Buffers: shared hit=48213` | No | Regexes built for phones or IPs match number runs → measurements masked, analysis wrong |
| Large IDs | `queryid = 4523719831203651234`, `xid 1203481`, OIDs, LSN `0/16B3748` | No | If Presidio's card recognizer accepts a Luhn-valid 16–19 digit `queryid` as CREDIT_CARD, it is masked as `<CREDIT_CARD>` and Claude loses the ID it needs to look the query up (cards used to block the request; now masked); to verify |
| Sizes and versions | `pg_size_pretty` → `12 GB`, `PostgreSQL 16.4`, `2.1.3.4` extension versions | No | Four-part versions matched as IPv4 (fenced for OIDs, not for every version string) |
| Client addresses | `pg_stat_activity.client_addr` → `10.0.4.17`, or a public IP | Private: no; public: yes | Private IPs are exempt (measured); public client IPs masked as intended |
| Migration files (`.sql`) | `ALTER TABLE customers ADD COLUMN ...`, seed `INSERT` rows | Seed values yes, DDL no | `.sql` is a data file, so the whole Read is masked: Edits of seed lines fail |

Scenarios to build, on a PostgreSQL fixture with a realistic `customers` table (the 50-row
open-data table from `scripts/pii-score/customers.py`, scaled up):

1. "Find why this query is slow": Claude runs `EXPLAIN ANALYZE`, proposes an index, verifies the plan.
2. "Top 5 slowest queries": `pg_stat_statements` (seeded so that at least one `queryid` is Luhn-valid: is it masked as a card?).
3. "Add a `phone_country` column and backfill it": migration file + `UPDATE` + verification query.
4. "Why is this table bloated?": `pg_stat_user_tables`, vacuum stats, sizes.
5. "Which city has the most customers?": aggregates over real values, answer must be correct yet masked.

What to check in each: task success (plan improved, migration applied), measurements unchanged in
what Anthropic receives, real values masked, no blocked request, no placeholder written into SQL.

## False positives found in public task texts

The research note ran code-guard's regexes (no NER) over the 500 SWE-bench Verified issue texts:
14 of them would be changed, **all false positives** ([details](research/agentic-coding-benchmarks.md#pii-scan-of-task-texts)).
Each becomes a scenario: the prompt below must reach Anthropic unchanged, and the task must succeed.

| Scenario | Prompt or tool output (from a real issue) | Current behaviour | Expected |
|---|---|---|---|
| Four-part versions in a bug report | `show_versions()` / `pip freeze` block with `pyerfa 2.0.0.1`, `cftime 1.0.3.4` (7 astropy, 4 xarray, 1 pytest issue) | Masked as `[IPV4_REDACTED]`: Claude can't see which dependency version is installed | Unchanged. Suggested fence: skip an IPv4 match that follows a package-name token on a `name version` line |
| SSH git remote | `git clone git@github.com:owner/repo` (sphinx-doc 9281) | `git@github.com` masked as `[EMAIL_REDACTED]`: the command Claude repeats is broken | Unchanged: `git@host:` is a remote, not a person |
| Date in code | `strtotime("0123-04-05 06:07:00")` (django 13670) | Masked as `[PHONE_FR_REDACTED]` | Unchanged |

Also worth a scenario, from the same scan: SWE-bench Multilingual issues quote real public IPs
(Caddy, Terraform, nushell configs, 19 of 300 issues). Those are masked as intended, but a task like
"why does this proxy config fail for 203.0.113.9" may then be unsolvable.

## What to measure in every scenario

Direct vs gateway, same model, several runs each: task success, failed Edits, placeholders written
into files, blocked requests, wall time, turns, tokens. `guardrail/claude_ab.py` already records all of them.

## Public benchmarks

From [research/agentic-coding-benchmarks.md](research/agentic-coding-benchmarks.md) (sources cited
there; nothing has been run against the gateway yet).

**Almost no public coding task contains personal data**, so public benchmarks mostly measure the
**pure coding** family: overhead (wall time, tokens, turns) and non-regression. A few tasks do probe
the other families:

| Benchmark | Size | Covers | Why it's useful here |
|---|---|---|---|
| **Terminal-Bench 2.0** | 89 tasks | Pure coding, Bash output, pasted PII | `mailman` has emails in the instruction itself (should fail through the gateway); `multi-source-data-merger` has user CSV/JSON with emails |
| **SWE-bench Pro (public)** | 731 tasks (642 on Hugging Face) | Pure coding; test output with PII | 68 tasks' patches or tests contain emails/IPs: does masked test output hurt? Realistic multi-file repos, Go/Python/JS/TS |
| SWE-bench Verified | 500 tasks, Python | Pure coding; look-alikes | 14 false positives (above). OpenAI stopped reporting it on 2026-02-23 (contaminated), but it still works for a direct-vs-gateway comparison |
| SWE-bench Multilingual | 300 tasks, 9 languages | Pure coding; public IPs in issues | 19 issues quote real public IPs |
| Aider polyglot | 225 exercises, 6 languages | Pure coding only | Cheapest overhead number: no personal data, small images |
| τ³-bench (retail) | 375 tasks | MCP tools | The only public benchmark serving customer records through an MCP server (customer service, not coding) |
| Terminal-Bench 4.0 | 66 tasks | Data files at scale | `telecom-entity-resolution`: ~46k emails and ~19k phones in CSVs. Too heavy to start with (GPU tasks, up to 8 h) |

Ruled out: SWE-Lancer (offline by design, ~14 GB images), SWE-bench Multimodal (image-based
tasks), DABstep (its PII columns are already tokenized), and aider's own harness (it runs aider, not
Claude Code).

**How to run them: [Harbor](https://github.com/laude-institute/harbor)**, the Terminal-Bench runner,
is the only public harness that drives the real `claude` CLI with a custom `ANTHROPIC_BASE_URL`, and
it has adapters for most benchmarks above.
- *Precondition, not verified:* Harbor runs Claude Code inside Docker, so the gateway must be
  reachable from the container (probably `http://host.docker.internal:<port>`); `litellm.local`
  won't resolve there.
- Set `ANTHROPIC_BASE_URL=https://api.anthropic.com` explicitly in the direct arm too, so both arms
  get the same Claude Code configuration from Harbor.

**Run plan:**
1. Plumbing: the 10-task `terminal-bench-sample@2.0` through the gateway; check requests reach LiteLLM.
2. Terminal-Bench 2.0: 6 PII tasks (`mailman`, `multi-source-data-merger`, `log-summary-date-ranges`,
   `nginx-request-logging`, `regex-log`, `financial-document-processor`) + 6 non-PII controls,
   × 2 arms × 3 attempts (72 trials, 900 s cap each). Expected: `mailman` fails through the gateway.
3. SWE-bench Pro public: 20 of the 68 PII tasks + 20 random controls, × 2 arms × 2 attempts, on an
   x86_64 Linux box (large images). Cheap alternative for overhead only: 50 Aider polyglot exercises.
4. τ³-bench retail (~20 tasks) as the MCP probe.
5. Then the custom families no benchmark covers, in `guardrail/claude_ab.py`: data files, pasted
   customer data, MCP output inside a coding task, `git log`/`blame`, database work, look-alikes.
