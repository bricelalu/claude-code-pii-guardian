# Out-of-Scope — Future PRDs

Items deliberately deferred from this POC. See README §15 for context.

## Custom recognizers
Organization-specific entity types: national IDs, customer IDs, internal hostnames, project codes.
Requires Presidio custom recognizer plugins and a recognizer registry. (One custom recognizer now
exists, for GLiNER2: `bench/analyzers/gliner2/`.)

## Non-English language support
Done for names and places: GLiNER2 detects French, English, Spanish and Italian while Presidio is
still called with `language: en` ([NER benchmark](ner-model-benchmark.md)). Presidio's own pattern
recognizers are unused; `code-guard` has its own regexes, including French phone numbers.

## Production manifests
- Secret management integration (HashiCorp Vault / External Secrets Operator)
- Egress NetworkPolicy restricting outbound to api.anthropic.com only
- Sticky sessions / consistent hashing for placeholder restoration across LiteLLM replicas
- HorizontalPodAutoscaler for Presidio Analyzer (CPU-bound at inference time)

## Observability integration
- Datadog forwarder sidecar for LiteLLM JSON logs
- Langfuse self-hosted for prompt analytics
- OpenTelemetry trace propagation linking Claude Code session IDs to Datadog APM spans

## Audit layer
The `presidio-audit` logging_only guardrail (low-score detections, DATE_TIME) is commented out in
`manifests/21-litellm-config.yaml`; `task demo`'s AUDIT scenario shows no audit event until it's
back.

## Always-warm analyzer
One RunPod worker kept warm during working hours removes the 80–100 s cold start (about $0.58/h,
~$100/month for 8 h × 22 days).

## Multi-tenant policies
Per-team guardrail profiles with different score thresholds and entity configs.
Requires LiteLLM virtual key per team with team-level guardrail routing.

## Token restoration
Masked tokens (`<PERSON_a3f9>`) in Claude's response would be substituted back with the original
value before reaching Claude Code. **Measured need:** without it, Claude Code can't edit masked
lines of a data file through the gateway (0/2 on "fix one cell in `customers.csv`",
[claude-code-guardrail.md](claude-code-guardrail.md)). Stable tokens can be rebuilt from each
request, because Claude Code resends the unmasked history every turn, so no mapping store is
needed. The hard part is streaming: the reply arrives in small pieces and a token can be split
across two of them, including inside `tool_use` input.

## CI/CD pipeline
Automated digest pinning on gateway image updates, policy-as-code tests for guardrail behavior,
canary promotion for LiteLLM config changes.

## Production authentication (see README §14)
- Option A: Negotiate Enterprise API key allocation with Anthropic CSM (recommended first step)
- Option B: Custom forward proxy preserving subscription OAuth bearer (2–3 weeks engineering)
- Option C: Client-side UserPromptSubmit hooks via MDM (defense in depth only)

## mTLS between Claude Code and the gateway
CLAUDE_CODE_CLIENT_CERT / CLAUDE_CODE_CLIENT_KEY for per-developer gateway authentication.
Irrelevant for local POC; useful in production to prevent unauthorized gateway access.
