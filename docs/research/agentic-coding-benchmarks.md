# Agentic coding benchmarks for testing the PII gateway

*2026-09-27 · research note, nothing run yet · Harbor @ `3c82380`, Terminal-Bench tasks @ `4def1f3`*

## Summary

- **Only one public harness can drive Claude Code through a proxy without extra work: [Harbor](https://github.com/laude-institute/harbor)**, the runner behind Terminal-Bench. Its `claude-code` agent passes `ANTHROPIC_BASE_URL` to the CLI inside the sandbox ([`claude_code.py` L127, L1807](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/src/harbor/agents/installed/claude_code.py#L127)). Harbor also runs SWE-bench Verified/Pro/Multilingual, Multi-SWE-bench, Aider polyglot, SWE-Lancer, τ³-bench and others as "adapters" ([registry](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/registry.json)).
- **Almost no public coding task contains personal data**, so most public benchmarks only measure overhead (latency, tokens, failed edits). I ran the gateway's own regexes over them (no NER): 14 of 500 SWE-bench Verified issue texts would be changed, all false positives, e.g. version strings like `pyerfa 2.0.0.1` read as IPv4 addresses.
- **Tasks that could reveal breakage:** Terminal-Bench 2.0 `mailman` (the instruction itself contains the list's email addresses, which would be masked), `multi-source-data-merger` (CSV/JSON of users with emails), Terminal-Bench 4.0 `telecom-entity-resolution` (about 46k emails and 19k phone numbers in CSVs), and τ³-bench (customer records served by an MCP server).
- **Recommendation:** first run **Terminal-Bench 2.0 through Harbor**: 10-task sample to check the plumbing, then 6 PII tasks + 6 controls. After that, run **SWE-bench Pro public**: 20 of the 68 tasks whose code touches emails/IPs plus 20 random controls. Both arms, 3 attempts each.
- **Main gap:** no benchmark edits data files, reads MCP output with real customer data inside a coding task, pastes customer data into the prompt, or reads `git log` for author emails. These have to stay custom scenarios (extend `guardrail/claude_ab.py`).
- **Precondition, not verified:** the agent runs inside a Docker container, so the gateway must be reachable from inside it. `litellm.local` in the laptop's `/etc/hosts` won't resolve in there.

## Comparison

"Can drive Claude Code" means an official or maintained runner starts the real `claude` CLI and lets you set its base URL. "PII exposure" comes from my regex scan (method in [Scan](#pii-scan-of-task-texts)) or from reading the task files.

| Benchmark | Tasks | Languages | Success check | Can drive Claude Code via proxy | PII exposure | Verdict |
|---|---|---|---|---|---|---|
| SWE-bench (full) | 2,294 test | Python, 12 repos | Hidden unit tests | Via Harbor (Verified only) | Issue texts: version-string false positives | Overhead |
| SWE-bench Lite | 300 test + 23 dev | Python, 11 repos | Same | No Harbor adapter | Same | Overhead |
| SWE-bench Verified | 500 | Python | Same | **Yes** (Harbor `swebench-verified`) | 14/500 issue texts hit, all false positives | Overhead + one false-positive class |
| SWE-bench Multimodal | 617 (102 dev / 510 test) | JavaScript, 17 repos | Tests; images in the issue | No | Not scanned | Not useful (images) |
| SWE-bench Multilingual | 300 | 9 languages, 42 repos | Tests | **Yes** (Harbor) | 19/300 issue texts hit (public IPs in Caddy/Terraform issues) | Overhead, a few probes |
| Multi-SWE-bench | 1,632 (mini 400, flash 300) | 7 languages | Tests | **Yes** (Harbor) | Not scanned | Overhead |
| SWE-bench-java | 91 | Java, 6 repos | Tests | No (folded into Multi-SWE-bench) | Not scanned | Superseded |
| SWE-PolyBench | 2,110 (PB500, Verified 382) | Java, JS, TS, Python | Tests | Predictions file only | Not scanned | Overhead |
| SWE-bench Pro (public) | 731 in the paper, 642 on HF now | Go, Python, JS, TS | Tests | **Yes** (Harbor `swebenchpro`, 731) | 68/642 tasks' patch/tests contain emails/IPs | **Overhead + probable breakage** |
| SWE-bench-Live | Python monthly; MultiLang 1,077; Windows 66 | 8+ languages | Tests | No | Not scanned | Overhead |
| SWE-rebench | 21k+ tasks | Python | Tests | No | Not scanned | Overhead |
| Terminal-Bench 2.0 | 89 | Shell, many | Per-task pytest | **Yes, native** | `mailman` instruction, `multi-source-data-merger` data | **Breakage probes + overhead** |
| Terminal-Bench 4.0 | 66 | Many; GPU tasks | Per-task tests | **Yes, native** | `telecom-entity-resolution` (46k emails) | Too heavy to start with |
| Aider polyglot | 225 | C++, Go, Java, JS, Python, Rust | Exercism unit tests | Via Harbor only (aider's harness runs aider) | None (no data files) | **Overhead only**, cheap |
| SWE-Lancer Diamond | 502 public (237 IC + 265 manager) | JS/TS (Expensify) | Playwright E2E / manager choice | Via Harbor (463) | Not checked | Poor fit: offline by design |
| τ³-bench | 375 (Harbor) | none (tool use) | Database end state | **Yes** (Harbor, MCP sidecar) | Customer names, addresses | MCP-masking probe |
| TheAgentCompany | 175 | Mixed office + SDE | Checkpoints | Via Harbor (37-task adapter) | Simulated colleagues | Maybe later |
| DABstep | 450 (Harbor), 460 (HF) | Python/pandas | Exact answer | **Yes** (Harbor, Claude Code parity run) | PII columns are tokenized | Overhead; possible NER hits on merchant names (unverified) |

## Per-benchmark notes

### SWE-bench (full, Lite, Verified, Multimodal, Multilingual)

- **What a task is:** "a codebase along with a description of an issue to be resolved". The model returns an edited codebase, judged by running tests ([paper](https://arxiv.org/abs/2310.06770)). Each instance has `FAIL_TO_PASS` and `PASS_TO_PASS` test lists ([dataset card](https://huggingface.co/datasets/princeton-nlp/SWE-bench)).
- **Full:** 2,294 test instances from 12 popular Python repositories ([paper](https://arxiv.org/abs/2310.06770)). There are also 225 dev and about 19k train rows ([HF](https://huggingface.co/datasets/princeton-nlp/SWE-bench)).
- **Lite:** 300 test + 23 dev from 11 of the 12 repos. Filtered to single-file patches with at most 3 hunks and no images or links in the issue ([swebench.com/lite](https://www.swebench.com/lite.html)).
- **Verified:** 500 human-validated tasks ([HF](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)).
  - 93 Python developers screened 1,699 samples. The "easy" subset has 196 tasks and the "hard" subset 45 ([OpenAI](https://openai.com/index/introducing-swe-bench-verified/)).
  - On 2026-02-23 OpenAI stopped reporting it: "SWE-bench Verified is increasingly contaminated", and "at least 59.4% of the audited problems have flawed test cases". OpenAI "recommends reporting results for SWE-bench Pro" ([OpenAI](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)).
  - For an A/B of the same model with and without the gateway, contamination and flawed tests hit both arms equally. It stays usable as a relative measure, not as a capability score.
- **Multimodal:** 617 tasks from 17 JavaScript libraries, each with at least one image in the issue or tests ([paper](https://arxiv.org/abs/2410.03859)); 102 dev / 510 test ([HF](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Multimodal)). Images are outside what the gateway masks, and there is no Harbor adapter. Skip it.
- **Multilingual:** 300 tasks, 42 repos, 9 languages (C, C++, Go, Java, JS, TS, PHP, Ruby, Rust) ([swebench.com/multilingual](https://www.swebench.com/multilingual.html)).
- **Harness and cost:**
  - MIT license. Evaluation is Docker-based: "at least 120GB of free storage", 16 GB RAM and 8 CPU cores are recommended, and x86_64 is preferred (arm64 is experimental) ([repo](https://github.com/SWE-bench/SWE-bench)). That rules out a quick local run on an Apple-silicon laptop.
  - The official harness scores a predictions file, so it doesn't run agents. To get Claude Code, use Harbor's `swebench-verified` (500) and `swebench_multilingual` (300) datasets ([registry](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/registry.json)).
- **PII:** GitHub issues and code, no data files. The regex scan found only false positives in the user-text channel ([Scan](#pii-scan-of-task-texts)). The repos are real git histories, so a `git log` in Bash would surface real committer emails. That output is masked, but no task needs it.
- **Verdict:** overhead only, apart from the version-string false positives, which are a real bug to fix.

### Multi-SWE-bench and SWE-bench-java

- **Multi-SWE-bench:** 1,632 instances in Java, TS, JS, Go, Rust, C and C++, annotated by 68 experts from 2,456 candidates ([paper](https://arxiv.org/abs/2504.02605)).
  - Repos include zstd, jq, fmt, nlohmann/json, cli/cli, grpc-go, mockito, axios, express, tokio, serde, ripgrep and vuejs/core. CC0 license ([HF](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench)).
  - The harness is Apache-2.0 and Docker-based. Subsets: **mini** (400 instances) and **flash** (300) ([repo](https://github.com/multi-swe-bench/multi-swe-bench)).
  - Harbor has an adapter, validated with Codex on 70 tasks ([parity](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/adapters/parity_summary.csv)).
- **SWE-bench-java:** 91 verified Java instances from 6 repos, Apache-2.0 ([HF](https://huggingface.co/datasets/Daoguang/Multi-SWE-bench)). The paper calls it "in progress" ([arXiv](https://arxiv.org/abs/2408.14354)). It became the Java part of Multi-SWE-bench.
- **Verdict:** overhead only. Useful to check that the code-file detection (pygments) doesn't mask Rust, C or Go sources.

### SWE-PolyBench

- 2,110 instances from 21 repos: Java 165, JS 1,017, TS 729, Python 199. Covers bug fixes, features and refactoring ([paper](https://arxiv.org/abs/2504.08703)).
- Subsets: PB500 (125 per language) and a Verified subset of 382. MIT license. Scores a `.jsonl` of patches with GHCR Docker images ([repo](https://github.com/amazon-science/SWE-PolyBench)).
- No Claude Code runner, so you would have to write one.
- **Verdict:** overhead only, and more glue work than Harbor-backed options.

### SWE-bench Pro

- **Tasks:** 1,865 problems from 41 repos, split into public (11 repos), held-out (12) and commercial (18 proprietary) sets ([paper](https://arxiv.org/abs/2509.16941)).
  - Public set: 731 tasks in Python, JS, TS and Go. Patches average 107.4 lines over 4.1 files. Graded by fail2pass/pass2pass tests. CC BY 4.0. Copyleft repos were chosen to reduce contamination ([paper HTML](https://arxiv.org/html/2509.16941)).
- **Current state:** the repo now ships "642 validated tasks in Harbor format" (V2), MIT license, with Docker images on GHCR/Docker Hub and Modal or local Docker ([repo](https://github.com/scaleapi/SWE-bench_Pro-os)). The HF dataset I downloaded has 642 rows: Go 256, Python 237, JS 145, TS 4 ([HF](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)).
- **Harbor:** `swebenchpro` has 731 tasks ([registry](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/registry.json)).
- **PII:** in 68 of 642 tasks the gold patch or test patch contains emails or IPs that the gateway would mask. Top repos: vuls 14, ansible 11, qutebrowser 11, protonmail/webclients 8, teleport 7, openlibrary 7.
  - Reading those code files stays unmasked.
  - Test runs, `grep` hits and failing assertions printed through Bash are masked. The agent would then see `[EMAIL_REDACTED]` where the real value caused the failure.
- **Verdict:** the best realistic repo benchmark for this gateway. Overhead on real multi-file work, and probable breakage on the 68-task slice. Not verified until run.

### SWE-bench-Live and SWE-rebench

- **SWE-bench-Live:** Python set updated monthly with 50 new verified issues, with frozen `lite` and `verified` splits. MultiLang has 1,077 instances from 431 repos in 8 languages, Windows has 66 instances. MIT license ([repo](https://github.com/microsoft/SWE-bench-Live), [paper](https://arxiv.org/abs/2505.23419)).
- **SWE-rebench:** a pipeline plus a dataset of more than 21,000 Python tasks, continuously refreshed to avoid contamination ([paper](https://arxiv.org/abs/2505.20411)).
- Neither has a Claude Code runner in Harbor's registry.
- **Verdict:** overhead only. Useful later if contamination matters, which it doesn't much for an A/B.

### Terminal-Bench (2.0 and 4.0) and Harbor

- **What a task is:** an English instruction, a test script, and a reference "oracle" solution ([legacy repo](https://github.com/laude-institute/terminal-bench)). Each task has its own Docker environment and resource limits in `task.toml`.
- **Terminal-Bench 2.0:** 89 tasks, "each task features a unique environment, human-written solution, and comprehensive tests". Frontier agents score below 65% ([paper](https://arxiv.org/abs/2601.11868)).
  - In Harbor as `terminal-bench@2.0` (89), plus a 10-task sample `terminal-bench-sample@2.0` ([registry](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/registry.json)).
  - The 2.0 tasks sit in the `archive/` folder of the [current repo](https://github.com/harbor-framework/terminal-bench/tree/4def1f367467b34b18e0dbdc086400ba71c3e037/archive). All 89 names match the registry.
  - Typical limits: 900 s agent timeout, 1 CPU, 2 GB RAM. 88 of the 90 archive folders set `allow_internet = true` (my count). The exceptions are `erp-procurement-planning` and `gpt2-codegolf`.
- **Terminal-Bench 4.0 (current leaderboard):** 66 tasks. The repo's `tasks/` folder and the [Harbor Hub listing](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench/4?tab=tasks) agree. Apache-2.0 ([repo](https://github.com/harbor-framework/terminal-bench)).
  - Some tasks need GPUs, so the official setup is `harbor run -d terminal-bench/terminal-bench@4.0.0 -e modal -a claude-code ...` ([tbench.ai/run](https://www.tbench.ai/run)).
  - Tasks are long. `telecom-entity-resolution` has `expert_time_estimate_hours = 16` and an agent timeout of 28,800 s ([task.toml](https://github.com/harbor-framework/terminal-bench/blob/4def1f367467b34b18e0dbdc086400ba71c3e037/tasks/telecom-entity-resolution/task.toml)).
  - The [leaderboard](https://www.tbench.ai/) lists many Claude Code entries. Their cost column runs from $2.7k to $9.6k per entry, and the page doesn't say how many attempts that covers. Too expensive for a first A/B.
- **Harbor and Claude Code:**
  - Harbor is Apache-2.0 and supports "Claude Code, OpenHands, Codex CLI" and more ([repo](https://github.com/laude-institute/harbor)).
  - Agent environment variables are passed with `--ae KEY=VALUE` ([`jobs.py`](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/src/harbor/cli/jobs.py#L653-L662)).
  - The `claude-code` agent reads `ANTHROPIC_BASE_URL` and forwards it to the CLI ([L127, L1807](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/src/harbor/agents/installed/claude_code.py#L1807)).
  - Two side effects of setting a base URL, read in the code but not tested:
    - Harbor then pins `ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` to the main model ([L1875-1880](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/src/harbor/agents/installed/claude_code.py#L1875)). **Set `ANTHROPIC_BASE_URL=https://api.anthropic.com` in the direct arm too**, or the two arms run different sub-models.
    - With a base URL, the model name keeps its provider prefix (`anthropic/claude-…`) ([L1751-1752](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/src/harbor/agents/installed/claude_code.py#L1751)). LiteLLM needs a `model_name` that matches it.
  - Each trial result records `agent_execution` timing plus input, cache and output tokens and cost ([`result.py`](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/src/harbor/models/trial/result.py#L66)). That gives latency overhead per task for free.
  - The claude-code agent also supports MCP servers (`mcp_servers=True`, L121).
- **PII in 2.0 (read from the task files):**
  - **`mailman`:** the [instruction](https://github.com/harbor-framework/terminal-bench/blob/4def1f367467b34b18e0dbdc086400ba71c3e037/archive/mailman/instruction.md) says to set up `reading-group@local.edu` with `-join@` and `-leave@` addresses. `local.edu` isn't a reserved domain, so the gateway's email regex masks all three in the user prompt. Predicted: **the agent can't know the list address → failure**.
  - **`multi-source-data-merger`:** merges `users.json`, `users.csv` and `users.parquet` with names and emails (`101,John Doe,john@b.com,…`). The CSV and JSON Reads are masked. The agent writes code that runs on the real files, so it may still pass. Placeholders written into code or output would show up as failures.
  - **`log-summary-date-ranges`, `nginx-request-logging`, `regex-log`:** logs and IP handling.
    - `regex-log` ships no log file. The public IPs are only in the hidden test ([test](https://github.com/harbor-framework/terminal-bench/blob/4def1f367467b34b18e0dbdc086400ba71c3e037/archive/regex-log/tests/test_outputs.py)).
    - It only probes the gateway if the agent tests its regex on sample lines it makes up with public or documentation-range IPs (e.g. `203.0.113.x`). Those would come back masked in Bash output. That's a hypothesis, not verified.
  - **`sanitize-git-repo`:** replace API keys with placeholders. Tests whether the gateway gets in the way of secret handling. The gateway doesn't mask API keys, so this is a control.
- **PII in 4.0:**
  - **`telecom-entity-resolution`:** four billing CSVs, ~93,000 records. The regex scan finds 46,142 emails and 19,405 international phones. The instruction names the adversarial cases ("Patrick"/"Pat", "Robert"/"Roberta"), which is user text that NER may mask ([instruction](https://github.com/harbor-framework/terminal-bench/blob/4def1f367467b34b18e0dbdc086400ba71c3e037/tasks/telecom-entity-resolution/instruction.md)).
  - **`data-anonymization`:** build an anonymizer over related CSVs.
  - **`react-lead-form`:** CRM lead export.
- **Verdict:** 2.0 is the best first run. It's cheap (900 s tasks, 1 CPU), drives the real Claude Code CLI, and has at least one near-certain breakage probe. 4.0 is the realistic stress test once the plumbing works.

### Aider polyglot

- 225 of Exercism's 697 exercises, "the most difficult", in C++, Go, Java, JavaScript, Python and Rust ([aider blog](https://aider.chat/2024/12/21/polyglot.html)).
  - Harbor's registry split: JS 49, Java 47, Go 39, Python 34, Rust 30, C++ 26 ([registry](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/registry.json)).
- Exercise content is © Exercism under its open-source licenses ([repo](https://github.com/Aider-AI/polyglot-benchmark)).
- Aider's own harness runs in Docker and only benchmarks aider ([README](https://github.com/Aider-AI/aider/blob/main/benchmark/README.md)).
- Harbor's adapter has been run with Claude Code: 225 tasks, `claude_code@v2.0.32` ([parity](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/adapters/parity_summary.csv)).
- **PII:** none. Single-file exercises, no data files.
- **Verdict:** a clean overhead-only arm (latency, tokens, failed Edits). Cheapest option if you only want the overhead number.

### SWE-Lancer

- 1,488 Upwork tasks worth $1M: 764 individual-contributor (IC) and 724 manager tasks.
  - The public Diamond split has 502 tasks worth $500,800 (237 IC + 265 manager).
  - IC tasks are graded by Playwright end-to-end tests on the Expensify JS/TS app ([paper](https://arxiv.org/html/2502.12115)).
- The OpenAI runner ships "198 tasks adjusted to run offline". Task images are ~14 GB each. It is "designed to run with internet disabled" and only takes OpenAI/OpenRouter keys ([README](https://github.com/openai/preparedness/blob/main/project/swelancer/README.md)).
- Harbor has `swe-lancer-diamond` (463: 198 IC + 265 manager).
- **Verdict:** poor fit. Heavy images, an offline design that clashes with a network gateway, and no data-file tasks.

### Adjacent benchmarks that exercise the gateway's other channels

- **τ³-bench / τ²-bench:** customer-service tool use in the airline, retail, telecom and banking_knowledge domains. MIT license, runs through LiteLLM ([repo](https://github.com/sierra-research/tau2-bench)).
  - The retail database has 500 users with first and last name, street address, email and payment methods. User IDs embed the name (`noah_brown_6181`) ([db.json](https://github.com/sierra-research/tau2-bench/blob/main/data/tau2/domains/retail/db.json)).
  - Emails use `example.com`, which the gateway exempts. Names and street addresses would go to NER.
  - Harbor's adapter (375 tasks) serves the environment through a `tau3-runtime` **MCP server** ([adapter README](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/adapters/tau3-bench/README.md)). That makes it the only public, ready-made test of **masking MCP tool results** under Claude Code.
  - Expect breakage by design: the agent has to authenticate a user by name and zip code.
- **TheAgentCompany:** 175 tasks in a simulated company (GitLab, OwnCloud, Plane, RocketChat) with simulated colleagues, graded by checkpoints ([paper](https://arxiv.org/html/2412.14161)). Harbor has a 37-task adapter.
- **DABstep:** 450 payment-data questions in Harbor (460 on HF, CC-BY-4.0, [HF](https://huggingface.co/datasets/adyen/DABstep)), with a Harbor parity run on `claude-code@2.1.39`.
  - `payments.csv` has `ip_address`, `email_address` and `card_number` columns, but the values are tokenized (`pKPYzJqqwB8TdpY0jiAeQw`), so the regexes won't fire.
  - Merchant names like `Crossfit_Hanna` could be tagged PERSON by NER (unverified).
- **SpreadsheetBench Verified (400 `.xlsx` tasks) and CRMArena (Salesforce org):** both in Harbor with Claude Code parity runs ([parity](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/adapters/parity_summary.csv)).
  - `.xlsx` reads go through Bash/Python output, which is masked.
  - CRMArena's privacy tasks are only in CRMArena-Pro ([adapter README](https://github.com/laude-institute/harbor/blob/3c82380859d187957cfd5cd64802b076d9779550/adapters/crmarena/README.md)).
- **MCPMark:** 127 MCP tasks with programmatic verification ([paper](https://arxiv.org/abs/2509.24002)). I couldn't confirm from the abstract which servers or data it uses (unverified).

## PII scan of task texts

The gateway's regex layer was applied as-is: `REGEXES`, `VALIDATORS` and `PROTECTED` imported from `guardrail/code_guard.py`. **NER (names, places) was not run**, so these counts are a lower bound. Data generated at Docker build time isn't included.

| Set | Tasks | User-text hits (issue / instruction → **masked**) | Hits in gold patch/tests (code Read unmasked; masked if printed by Bash/Grep) |
|---|---|---|---|
| SWE-bench Verified | 500 | 14 tasks: 12 IPV4, 1 PHONE_FR, 1 EMAIL, **all false positives** | 2 |
| SWE-bench Multilingual | 300 | 19 tasks: 54 IPV4, 3 IPV6, 2 EMAIL (real-looking public IPs in Caddy/Terraform/nushell issues) | 4 |
| SWE-bench Pro (HF, 642) | 642 | 2 tasks | **68 tasks**: 312 EMAIL, 146 IPV4, 19 IPV6, 19 PHONE_FR |
| Terminal-Bench 2.0 | 89 | **1 (`mailman`, 3 emails)** | Data files: 1 (`multi-source-data-merger`, 4 emails) |
| Terminal-Bench 4.0 | 66 | 0 by regex (names in `telecom-entity-resolution` need NER) | Data files: `telecom-entity-resolution` 46,142 EMAIL + 19,405 PHONE_INTERNATIONAL; `react-lead-form` 1 phone |

False positives found in SWE-bench Verified issue texts. These are bugs the gateway would ship today:

- **Four-part version strings read as IPv4:** `pyerfa 2.0.0.1` (7 astropy issues), `cftime 1.0.3.4` / `1.0.4.2` / `1.1.1.2` (4 xarray issues, 1 pytest issue). They come from the `show_versions()` / `pip freeze` blocks that bug reports paste. Masking them hides the dependency version the agent needs. Suggested fence: skip an IPv4 match that follows a package-name token on a `name version` line. Not implemented, since this note changes no code.
- **`git clone git@github.com:owner/repo`** (sphinx-doc 9281): `git@github.com` is masked as an email. `PROTECTED` only spares the part after the `:`, which breaks the command.
- **`strtotime("0123-04-05 06:07:00")`** (django 13670): masked as a French phone number.

## Gaps: what no public benchmark covers

These are the channels the gateway masks ([what gets masked](../claude-code-guardrail.md#what-gets-masked)) that no public task exercises in a coding workflow. Build them as custom scenarios in `guardrail/claude_ab.py`:

1. **Editing a data file with PII:** fix a CSV cell, update a JSON fixture, a SQL seed or a Markdown table. Already known to fail by design (0/2). No public benchmark includes it.
2. **Data files read as "unknown type":** `.env`, `.ipynb`, `.yml` fixtures that pygments doesn't know, extension-less exports. They are masked as data. Does Claude still edit the code around them?
3. **Pasted customer data in the prompt:** "customer Jean Dupont, IBAN FR76…, says the invoice is wrong, fix `billing.py`". The Terminal-Bench `mailman` case shows the instruction channel alone can break a task. The same goes for names used as examples, as in `telecom-entity-resolution` ("Patrick"/"Pat").
4. **MCP output inside a coding task:** a DB or ticketing MCP returns customer rows, and the agent must write a migration or a bug fix keyed on a real value. τ³-bench covers MCP masking only for customer service, not coding.
5. **`git log`, `git blame`, `git shortlog`:** author emails and names in Bash output. Tasks like "who changed this line, ask them" or "revert Alice's commit" can break.
6. **Logs and infrastructure output with public IPs:** nginx access logs, `dig`, `curl -v`, cloud CLI output (`aws ec2 describe-instances`), firewall rules. Private ranges are spared, but public addresses and documentation ranges are masked, so "block this attacker IP" can't work.
7. **Version and identifier look-alikes:** four-part versions (found above), OIDs, build numbers, phone-shaped order IDs, card-shaped test numbers (a Luhn-valid test card used to **block** the whole request; since 2026-09-27 it is masked as `<CREDIT_CARD>`).
8. **Test failures that print PII:** assertion diffs with emails or phones (`expected 'a@b.fr' got …`). This is the SWE-bench Pro 68-task slice, but it only matters when the value is printed, which no benchmark checks.
9. **Long sessions:** cache hit rate and cumulative latency over 50+ turns. Benchmarks start cold, so the measured overhead is the worst case.

## Recommendation

**Precondition (unverified; check before anything else).** Harbor runs Claude Code inside a Docker container, so the gateway must be reachable from inside it.

- On Docker Desktop that usually means `ANTHROPIC_BASE_URL=http://host.docker.internal:<port>` plus the matching LiteLLM `model_name`. `litellm.local` in the laptop's `/etc/hosts` won't resolve in the container.
- I didn't check whether Harbor's local Docker environment allows it. Modal and Daytona would need a public gateway, which you shouldn't expose.
- Prove the plumbing with the 10-task `terminal-bench-sample@2.0` and check that requests show up in LiteLLM's log.

```bash
harbor run -d terminal-bench-sample@2.0 -a claude-code -m anthropic/claude-haiku-4-5 \
  --ae ANTHROPIC_BASE_URL=http://host.docker.internal:4000 --ae ANTHROPIC_API_KEY=sk-xxx -k 1
```

1. **Terminal-Bench 2.0 via Harbor, about 12 tasks × 2 arms × 3 attempts (72 trials, 900 s cap each).**
   - PII probes: `mailman`, `multi-source-data-merger`, `log-summary-date-ranges`, `nginx-request-logging`, `regex-log`, `financial-document-processor`.
   - Six non-PII controls of similar difficulty, e.g. `build-cython-ext`, `fix-git`, `polyglot-c-py`, `sqlite-with-gcov`, `configure-git-webserver`, `cancel-async-tasks`.
   - What it shows: pass rate per arm, wall time per trial (`agent_execution`), tokens, and (from the trajectories) failed Edit calls and placeholders written to disk.
   - Expected: `mailman` fails through the gateway. If it doesn't, the instruction channel isn't being masked the way the scan predicts.
2. **SWE-bench Pro public via Harbor, 40 tasks × 2 arms × 2 attempts.**
   - 20 drawn from the 68 tasks whose patches or tests carry emails/IPs, 20 random controls, stratified across Go, Python and JS.
   - This is the realistic, multi-file repo workload that gives a trustworthy overhead number.
   - The 68-task slice shows whether masked test output hurts.
   - Needs x86_64 Docker and large per-task images, so run it on a Linux box, not the Mac.
   - If you only want overhead cheaply, swap in Harbor's `aider-polyglot`: 50 random exercises, no PII, small images.

After those, write the custom scenarios from the [gaps](#gaps-what-no-public-benchmark-covers) list, points 1, 3, 4 and 5 first. Add τ³-bench retail (about 20 tasks) as the MCP probe.
