# Out-of-Scope — Future PRDs

Items deliberately deferred from this POC. See README §15 for context.

## Custom recognizers
Organization-specific entity types: national IDs, customer IDs, internal hostnames, project codes.
Requires Presidio custom recognizer plugins and a recognizer registry.

## Non-English language support
Presidio supports multiple languages via additional spaCy models. Adding French (fr) or other locales
requires init containers to download language models and updated `presidio_language` config per guardrail.

## Production manifests
- Secret management integration (HashiCorp Vault / External Secrets Operator)
- Egress NetworkPolicy restricting outbound to api.anthropic.com only
- Sticky sessions / consistent hashing for placeholder restoration across LiteLLM replicas
- HorizontalPodAutoscaler for Presidio Analyzer (CPU-bound at inference time)

## Observability integration
- Datadog forwarder sidecar for LiteLLM JSON logs
- Langfuse self-hosted for prompt analytics
- OpenTelemetry trace propagation linking Claude Code session IDs to Datadog APM spans

## Multi-tenant policies
Per-team guardrail profiles with different score thresholds and entity configs.
Requires LiteLLM virtual key per team with team-level guardrail routing.

## Token restoration
Presidio's replace operation produces a reverse-mapping so masked tokens ([PERSON_1]) in Claude's
response can be substituted back with the original value before returning to the user.
Requires a stateful mapping store (Redis or in-process cache) and a post-call hook.

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
