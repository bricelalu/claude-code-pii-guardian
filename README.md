#  Local POC: PII-Guardian - PII-Aware AI Gateway for Claude Code

**Status:** Draft for implementation by Claude Code
**Estimated effort:** 1–2 days
**Reference docs:**
- Claude Code Network Configuration
- Claude Code LLM Gateway
- LiteLLM Presidio Guardrail
- LiteLLM Guardrails on Pass-Through Endpoints

---

## 1. Context
Acme Inc. operates Claude Enterprise with several hundred developer subscriptions. The Enterprise DPA and EU SCCs provide the legal framework for personal-data transfers to Anthropic's US-based infrastructure. To strengthen the GDPR posture with a *defense-in-depth* technical control, Acme wants to introduce a centralized AI gateway that detects, masks, and selectively blocks PII before prompts leave the perimeter.
This POC validates the architecture locally on **K3D** before any consideration of production cluster rollout. The headline deliverable is a task demo that executes against the real Anthropic API via an authenticated Claude Code CLI.

## 2. Goal
A single task demo command that:
1. Brings up a local K3D cluster with LiteLLM + Presidio (analyzer + anonymizer)
2. Configures the operator's Claude Code CLI to route through the local gateway via ANTHROPIC_BASE_URL
3. Executes three scenarios proving **MASK**, **AUDIT**, and **BLOCK** behaviors
4. Displays clear evidence (LiteLLM logs, before/after prompt bodies) that the protection layer worked
The POC must be reproducible and tear-downable with task clean.

## 3. Non-Goals (what Claude Code must NOT do)
Stay tight on scope. Do **not**:
- Fork or vendor third-party MITM proxies; do not write a custom MITM proxy.
- Add Langfuse, Datadog, OpenTelemetry, or any observability backend. kubectl logs is sufficient.
- Add Helm charts, ArgoCD, Flux, or GitOps tooling. Use plain kubectl apply invoked from Taskfile.
- Add Istio, Linkerd, or any service mesh.
- Add Postgres, Redis, or persistent volumes. Everything is ephemeral.
- Write custom Python guardrails. Use LiteLLM's built-in presidio guardrail only.
- Implement a mock Anthropic endpoint. The demo hits the real Anthropic API.
- Add TLS, ingress, or cert-manager. Plain HTTP via kubectl port-forward is fine for the POC.
- Add French (fr) or other non-English Presidio support. English-only.
- Add custom recognizers (national ID schemes, customer-specific IDs). Use Presidio defaults.
- **Install LiteLLM via pip install litellm.** See section 13 — only the official container image is acceptable.

## 4. Architecture
The integration follows the officially documented LLM gateway pattern: Claude Code is pointed at LiteLLM via ANTHROPIC_BASE_URL, authenticates to LiteLLM via ANTHROPIC_AUTH_TOKEN, and LiteLLM forwards to Anthropic using its own configured API key.

┌──────────────────────┐
│  Claude Code CLI     │   env:
│                      │   ANTHROPIC_BASE_URL=http://localhost:4000
│                      │   ANTHROPIC_AUTH_TOKEN=sk-poc-local-only
└──────────┬───────────┘
           │  POST /v1/messages
           │  Authorization: Bearer sk-poc-local-only
           │  X-Claude-Code-Session-Id: <uuid>
           ▼
┌──────────────────────┐
│  kubectl port-forward │  svc/litellm 4000:4000
└──────────┬───────────┘
           │
           ▼
   ┌─────────────────────── K3D cluster ────────────┐
   │  Namespace: gateway                            │
   │                                                │
   │  ┌────────────────────────────────────────┐   │
   │  │  LiteLLM Proxy (port 4000)             │   │
   │  │  /v1/messages (unified Anthropic fmt)  │   │
   │  │                                        │   │
   │  │   ┌──── pre_call guardrail ────┐       │   │
   │  │   │  MASK email/name/phone     │       │   │
   │  │   │  BLOCK credit_card/iban    │       │   │
   │  │   └────────────────────────────┘       │   │
   │  │   ┌──── logging_only guardrail ──┐     │   │
   │  │   │  AUDIT faint PII signals     │     │   │
   │  │   └──────────────────────────────┘     │   │
   │  └────────────┬───────────────────────────┘   │
   │               │                                │
   │               ▼                                │
   │  ┌──────────────────┐  ┌──────────────────┐  │
   │  │ Presidio         │  │ Presidio         │  │
   │  │ Analyzer (3000)  │  │ Anonymizer (3000)│  │
   │  └──────────────────┘  └──────────────────┘  │
   │                                                │
   └────────────────────────┬──────────────────────┘
                            │  Authenticated with Anthropic API key
                            │  held in LiteLLM (Kubernetes Secret)
                            ▼
                  https://api.anthropic.com
**Why this pattern over HTTPS_PROXY:** the LLM-gateway pattern is documented and supported by Anthropic, requires no TLS interception, no CA distribution, and no MITM. The guardrails see the request body as structured JSON, not bytes to demangle from a TLS tunnel.

## 5. Repository Structure

pii-guardian/
├── README.md
├── Taskfile.yml
├── .env.example                        # template for ANTHROPIC_API_KEY
├── k3d/
│   └── cluster.yaml
├── manifests/
│   ├── 00-namespace.yaml
│   ├── 10-presidio-analyzer.yaml
│   ├── 11-presidio-anonymizer.yaml
│   ├── 20-litellm-secret.yaml.tmpl    # template; sealed by Taskfile from .env
│   ├── 21-litellm-config.yaml         # ConfigMap with guardrail YAML
│   └── 22-litellm.yaml                # Deployment + Service
├── demo/
│   ├── scenario-mask.sh
│   ├── scenario-audit.sh
│   ├── scenario-block.sh
│   └── show-evidence.sh
└── docs/
    └── notes.md

## 6. Component Specifications
### 6.1 K3D Cluster
- Single-node cluster named claude-gateway-poc
- K3s version: latest stable (pin in k3d/cluster.yaml)
- Disable Traefik (--disable=traefik); no ServiceLB needed
- Host connectivity via kubectl port-forward only
### 6.2 Presidio Analyzer
- Image: mcr.microsoft.com/presidio-analyzer (pin to a specific tag and digest at scaffolding time)
- Replicas: 1
- Resources: requests 500m CPU / 1Gi memory; limits 1 CPU / 2Gi memory
- ClusterIP service, port 3000
- Liveness/readiness on /health
- Bundled English spaCy model — no init container
### 6.3 Presidio Anonymizer
- Image: mcr.microsoft.com/presidio-anonymizer (pin to a specific tag and digest at scaffolding time)
- Replicas: 1
- Resources: requests 100m CPU / 128Mi memory; limits 200m CPU / 256Mi memory
- ClusterIP service, port 3000
- Liveness/readiness on /health
### 6.4 LiteLLM
**Image source is non-negotiable:** use ghcr.io/berriai/litellm:main-stable only, pinned to a specific image digest at scaffolding time. **Never** install via PyPI. See section 13 for the supply-chain rationale.
- Replicas: 1
- Config mounted from ConfigMap to /app/config.yaml
- Service: ClusterIP, port 4000
- Required env (from Secret litellm-secrets):
  - ANTHROPIC_API_KEY — Anthropic API key used by LiteLLM to call Anthropic
  - LITELLM_MASTER_KEY=sk-poc-local-only (the value Claude Code will send as ANTHROPIC_AUTH_TOKEN)
- Required env (from ConfigMap or inline):
  - PRESIDIO_ANALYZER_API_BASE=http://presidio-analyzer.gateway.svc.cluster.local:3000
  - PRESIDIO_ANONYMIZER_API_BASE=http://presidio-anonymizer.gateway.svc.cluster.local:3000
  - LITELLM_LOG=DEBUG
- Args: ["--config", "/app/config.yaml", "--port", "4000", "--detailed_debug"]
### 6.5 LiteLLM Configuration
The ConfigMap litellm-config contains config.yaml:

model_list:
  - model_name: "claude-*"
    litellm_params:
      model: "anthropic/*"
      api_key: "os.environ/ANTHROPIC_API_KEY"
general_settings:
  master_key: "os.environ/LITELLM_MASTER_KEY"
guardrails:
  # Primary protective layer — masks visible PII, blocks financial data
  - guardrail_name: "presidio-mask"
    litellm_params:
      guardrail: presidio
      mode: "pre_call"
      default_on: true
      presidio_language: "en"
      presidio_score_thresholds:
        ALL: 0.6
        PERSON: 0.75
      pii_entities_config:
        EMAIL_ADDRESS: "MASK"
        PERSON: "MASK"
        PHONE_NUMBER: "MASK"
        CREDIT_CARD: "BLOCK"
        IBAN_CODE: "BLOCK"
  # Secondary observability layer — catches faint signals, never blocks
  - guardrail_name: "presidio-audit"
    litellm_params:
      guardrail: presidio
      mode: "logging_only"
      default_on: true
      presidio_language: "en"
      presidio_score_thresholds:
        ALL: 0.35
      pii_entities_config:
        EMAIL_ADDRESS: "MASK"
        PERSON: "MASK"
        PHONE_NUMBER: "MASK"
        LOCATION: "MASK"
        DATE_TIME: "MASK"
litellm_settings:
  set_verbose: true
  json_logs: true
Claude Code sends these attribution headers on every request: X-Claude-Code-Session-Id, X-Claude-Code-Agent-Id, X-Claude-Code-Parent-Agent-Id. The implementer must verify they appear in LiteLLM logs (they typically do with set_verbose: true and json_logs: true). If not, add the appropriate LiteLLM logging config to capture inbound headers. These are documented in the Claude Code LLM gateway docs and are essential for per-session DPO audit.

## 7. Taskfile Requirements
Taskfile.yml must define these tasks, each with a desc: field:
| Task | Behavior |
|------|----------|
| task up | Create K3D cluster (idempotent), generate Secret from .env, apply manifests, wait for Ready |
| task down | Stop the K3D cluster but preserve state |
| task clean | Delete the K3D cluster entirely |
| task verify-images | Re-pull every image and verify the digest matches what is declared in manifests; fail if mismatch |
| task status | Pod status + last 20 log lines + curl health check on LiteLLM |
| task logs | kubectl logs -n gateway -l app=litellm -f |
| task port-forward | Run port-forward in foreground (blocks) |
| task demo | The headline task — see section 8 |
task up must depend on task verify-images. Errors must surface clearly; avoid silent: true unless suppressing genuine noise.

## 8. The task demo Task
Sequence:
1. Verify .env exists with ANTHROPIC_API_KEY set; fail fast if not
2. Run task up (idempotent — skip if already running)
3. Wait for LiteLLM /health to return 200 (poll up to 60s)
4. Start kubectl port-forward in background, capture PID, ensure cleanup on exit
5. Display setup banner with required env vars for the operator's shell:
   ```
   export ANTHROPIC_BASE_URL=http://localhost:4000
   export ANTHROPIC_AUTH_TOKEN=sk-poc-local-only
   ```
6. Run **Scenario 1: MASK** → demo/scenario-mask.sh
7. Pause for keypress (read -n 1 -p "Press any key for Scenario 2...")
8. Run **Scenario 2: AUDIT** → demo/scenario-audit.sh
9. Pause
10. Run **Scenario 3: BLOCK** → demo/scenario-block.sh
11. Final banner: "Demo complete. Run task logs for the full audit trail."
12. On exit (including Ctrl-C), kill the port-forward PID

## 9. Demo Scenarios
Each scenario script must:
- Set ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN for the claude invocation
- Run claude -p "<prompt>" for non-interactive execution against the local gateway
- Capture LiteLLM logs *during* the call (e.g., kubectl logs --since=10s snapshot after the call)
- Print three blocks with clear visual separators:
  - **What the operator typed** (with PII visible)
  - **What LiteLLM forwarded to Anthropic** (extracted from logs)
  - **Claude's response**
- Print a one-line verdict (✓ or ✗) explaining what was proven
### 9.1 Scenario MASK — demo/scenario-mask.sh
**Prompt:**
> "Please rewrite this email more politely: 'Hey John Smith, your delivery to john.smith@example.com is delayed. Call us at +33 6 12 34 56 78.'"
**Expected evidence:**
- LiteLLM logs show [PERSON], [EMAIL_ADDRESS], [PHONE_NUMBER] in the body forwarded to Anthropic
- presidio-mask guardrail event fired
- Claude responds with a rewritten email referencing placeholders (acceptable for the demo)
**Verdict line:** "PII redacted before reaching Anthropic."
### 9.2 Scenario AUDIT — demo/scenario-audit.sh
**Prompt:**
> "Our team meeting is scheduled for next Tuesday at our Paris office. Sarah will present the Q3 forecast."
**Expected evidence:**
- presidio-mask (threshold 0.6) lets the prompt through unmodified — soft signals below blocking confidence
- presidio-audit (threshold 0.35, logging_only) records detections for LOCATION=Paris, PERSON=Sarah, DATE_TIME=next Tuesday
- The prompt sent to Anthropic is **unchanged**
- Logs contain a detection event that a DPO could review
**Verdict line:** "Soft PII not blocked, but flagged for review. This is the safety net."
### 9.3 Scenario BLOCK — demo/scenario-block.sh
**Prompt:**
> "Help me parse this transaction log: card 4111-1111-1111-1111 charged 89.50 EUR on 2026-05-12."
**Expected evidence:**
- presidio-mask matches CREDIT_CARD with action BLOCK
- LiteLLM returns an HTTP error (BlockedPiiEntityError) — Claude Code surfaces an error to the user
- Logs confirm the request never left the gateway
**Verdict line:** "Credit card detected — request blocked. Anthropic never saw it."

## 10. Show-Evidence Helper
demo/show-evidence.sh is invoked by each scenario. It must:
- Snapshot LiteLLM logs from the last N seconds (kubectl logs --since=10s)
- Filter for guardrail-related lines (grep for guardrail, presidio, BlockedPii, X-Claude-Code-Session-Id)
- Pretty-print JSON log lines via jq if installed; fall back to raw output otherwise
- Highlight the messages field of the forwarded request body so the operator visually confirms what crossed the boundary

## 11. Acceptance Criteria
A reviewer running task demo on a clean machine must observe:
1. K3D cluster becomes ready in under **90 seconds**
2. Three scenarios execute sequentially with clear visual demarcation
3. **MASK** scenario shows side-by-side "user input" vs "sent to Anthropic" with PII visibly redacted
4. **AUDIT** scenario shows a detection log entry without prompt modification
5. **BLOCK** scenario produces a clear error from Claude Code and a BlockedPiiEntityError in LiteLLM logs
6. task verify-images confirms image digests match before task up proceeds
7. task clean removes the cluster in under **30 seconds**
8. Re-running task demo after task clean works without manual intervention

## 12. Operator Prerequisites (document in README.md)
- macOS or Linux
- Docker Desktop or Podman 4.x with Docker socket emulation
- k3d ≥ v5.6
- kubectl ≥ v1.28
- task (Taskfile.dev) ≥ v3.30
- jq
- Claude Code CLI ≥ v2.1.129
- **An Anthropic API key with access to Claude models** — needed to populate the ANTHROPIC_API_KEY Secret consumed by LiteLLM. The POC uses an API key because the LLM gateway pattern documented by Anthropic terminates auth at LiteLLM. See section 14 for the production implication.
- Network access to api.anthropic.com

## 13. Supply Chain Hardening (mandatory)
Anthropic's official documentation explicitly warns that **LiteLLM PyPI versions 1.82.7 and 1.82.8 were compromised with credential-stealing malware**. The POC must enforce the following:
- LiteLLM is consumed exclusively via the official container image ghcr.io/berriai/litellm:main-stable, **pinned to a specific image digest** at scaffolding time (use docker pull + docker inspect to capture the digest, write it into the manifest as image: ghcr.io/berriai/litellm@sha256:<digest>).
- The Taskfile.yml must include a task verify-images that re-pulls each image and checks the digest matches what is declared in the manifests. Run this as part of task up.
- The README.md must contain a "Supply chain" section explicitly forbidding pip install litellm for any reason, including local development.
- Presidio images come from mcr.microsoft.com (Microsoft-controlled registry) and must also be pinned by digest.
This is non-negotiable. A PII protection layer compromised by malware is strictly worse than no PII protection layer.

## 14. Production Authentication: The Subscription-Only Constraint
A common Enterprise scenario: an organization holds N developer subscriptions for Claude Code and explicitly wants to avoid metered API-key billing in production (subscriptions are flat-rate; Bedrock or API would multiply costs).
**The honest finding from LiteLLM documentation:** LiteLLM always requires an Anthropic API key (or AWS/GCP credentials) to authenticate outbound to api.anthropic.com. The "pass-through" endpoint pattern (/anthropic/*) does not forward the client's subscription OAuth token — it swaps it for LiteLLM's configured provider credentials. This is consistent across both the unified /v1/messages and pass-through /anthropic/v1/messages routes.
Three viable production paths, evaluated:
### Option A — Negotiate Enterprise API key allocation (lowest friction)
Ask the Anthropic CSM whether the existing Enterprise contract includes API key allocation for centralized gateway use, billed under the existing commercial terms rather than metered separately. Many Enterprise contracts include this. A LiteLLM gateway then uses these allocated keys; developers keep their per-seat subscriptions for Claude Code features (Skills, sub-agents, hooks, web search). Cost: one phone call. Resolution time: days.
### Option B — Build a custom forward proxy (no API key, higher engineering cost)
Implement a small forward proxy (typically Go, ~1000–1500 lines) that:
- Receives traffic via HTTPS_PROXY from each Claude Code client
- Terminates TLS using an intermediate CA signed by the organization's root CA (already in the OS trust store of corporate laptops)
- Calls Presidio inline for PII detection and masking
- Re-establishes TLS to api.anthropic.com, preserving the client's subscription OAuth bearer untouched
- Handles SSE streaming for response bodies
This is the only architecture that fully preserves subscription auth end-to-end without an API key. Cost: 2–3 weeks of development plus ongoing operations.
### Option C — Client-side Claude Code hooks (minimal infrastructure)
Deploy a local UserPromptSubmit hook on every laptop via MDM that calls a Presidio service and warns/blocks before submission. No central enforcement, no central audit; defense in depth only. Cost: lowest. Audit power: lowest.
**Recommended sequence:** call the CSM (Option A) before committing any engineering. If Option A is unavailable, Option B becomes the central control with Option C as defense in depth. This decision is out of scope for the POC itself, which validates the masking architecture regardless of which auth model production ultimately uses.

## 15. Out-of-Scope (deliberately deferred)
Not for this POC; list in docs/notes.md for future PRDs:
- Custom recognizers for organization-specific entities (national IDs, customer IDs, internal hostnames)
- Non-English Presidio language models
- Production cluster manifests with secret-management integration (Vault/External Secrets), egress policy, sticky sessions for placeholder restoration across replicas
- Observability integration (Datadog forwarder, Langfuse self-hosted)
- Multi-tenant policies per team
- Restoration of masked tokens in Claude's responses (Presidio replace operation with reverse map)
- CI/CD pipeline for the gateway image and config
- The production authentication paths from section 14
- mTLS authentication between Claude Code and the gateway (CLAUDE_CODE_CLIENT_CERT / CLAUDE_CODE_CLIENT_KEY) — useful for per-dev gateway authentication in production, irrelevant for local POC

## 16. Success Definition
This POC succeeds the moment a 90-second demo can be shown containing:
- A real Claude Code CLI prompt with visible PII
- A live kubectl logs view proving the PII was masked or blocked before reaching Anthropic
- A clear audit-layer detection on a borderline prompt
- A visible X-Claude-Code-Session-Id in the logs proving per-session attribution is possible
Anything beyond this output is out of scope. If the POC produces this evidence, the masking architecture is validated and the conversation moves to production rollout — starting with the authentication question in section 14.