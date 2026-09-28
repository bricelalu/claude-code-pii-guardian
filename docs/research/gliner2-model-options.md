# GLiNER2 model options: is there a better PERSON/LOCATION model than the one we run?

*2026-09-28 · research note, nothing run · deployed model `fastino/gliner2-privacy-filter-PII-multi`
@ [`36126f6`](https://huggingface.co/fastino/gliner2-privacy-filter-PII-multi/commit/36126f612f1f9e376dc2c25b297d827912effef4)
(per [`bench/analyzers/gliner2/Dockerfile`](../../bench/analyzers/gliner2/Dockerfile)),
package pinned to `gliner2==2.0.0`
(per [`bench/analyzers/gliner2/requirements.txt`](../../bench/analyzers/gliner2/requirements.txt)),
GitHub tag [`v2.0.0`](https://github.com/fastino-ai/GLiNER2/releases/tag/v2.0.0)*

## Summary

- **Fastino publishes 14 models** as of 2026-09-28
  ([HF API listing](https://huggingface.co/api/models?author=fastino&limit=500)). **None of them
  is a GLiNER2.5 model fine-tuned for PII/privacy.** The only two PII-taxonomy models in the org
  are both on the older "GLiNER2" span architecture: our current model and
  `fastino/GLiNER2-Guardrails-PII-Multi`.
- **`GLiNER2-Guardrails-PII-Multi` is the one model worth a second look, but only claims parity,
  not improvement.** It's fine-tuned from `fastino/gliner2-base-v1`, is the identical size (307M
  parameters, same as our current model), carries the *same* 42-label PII taxonomy as our current
  model (including every label in our `ENTITY_MAPPING`: `person`, `full_name`, `first_name`,
  `last_name`, `address`, `street_address`, `city`, `state_or_region`, `country`), and adds LLM
  guardrail/moderation labels in the same weights. Its card claims "no regression" against the PII
  model we already run, but gives no numeric side-by-side table, and the benchmark behind that
  claim (SPY, legal/medical prose) isn't ours.
- **`fastino/gliner2.5-multi-v1`** is the only multilingual model on the newer GLiNER2.5 (boundary)
  architecture, but it is a *general-purpose* extractor with no fixed PII taxonomy and no published
  NER/PII benchmark numbers, and its own model metadata doesn't confirm French/Spanish/Italian
  support beyond the tags `"multilingual"` and `"en"`.
- **Nothing published by Fastino measures what our benchmark measures** (code false positives,
  file-validity-after-masking, PERSON/LOCATION precision/recall on FR/EN/ES/IT source code). Every
  benchmark number below is Fastino's own, on their own data, and is marked as such.
- **Our currently pinned revision (`36126f6`) is one commit behind the repo's current HEAD
  (`1cb4166`), but the model weights are byte-identical** — same `model.safetensors` LFS object ID
  at both revisions. The one intervening commit only repoints a README banner link. There is
  nothing newer to pull for the model we already run.
- **`extract_entities_long`, the method our recognizer calls, is confirmed to exist** in
  `gliner2==2.0.0` and is shared by both architectures: it's defined once in
  `ExtractorRuntimeMixin` and mixed into both `SpanExtractor` (legacy/"GLiNER2") and
  `BoundaryExtractor` (GLiNER2.5), so both architecture families expose an identical call
  signature for this method in the version we already have pinned.

## Comparison

| Model | Params | Languages | Entities | License | Loadable via our API? | Source |
|---|---|---|---|---|---|---|
| **`fastino/gliner2-privacy-filter-PII-multi`** *(deployed)* | 307,098,645 (~307M / "0.3B") | en, fr, es, de, it, pt, nl | 42 PII labels incl. our full `ENTITY_MAPPING` | Apache 2.0 | Yes — this is what we run | [model card](https://huggingface.co/fastino/gliner2-privacy-filter-PII-multi/blob/1cb4166094dc58fa8d836429f060d6c95f62b495/README.md), [API](https://huggingface.co/api/models/fastino/gliner2-privacy-filter-PII-multi) |
| `fastino/GLiNER2-Guardrails-PII-Multi` | 307,098,645 (~307M, identical size to current) | en, fr, es, de, it, pt, nl (cardData) | Same 42 PII labels as above, plus 6 moderation tasks (`prompt_safety`, `prompt_toxicity`, `jailbreak_detection`, `response_safety`, `response_toxicity`, `response_refusal`) | Apache 2.0 | Same span (legacy "GLiNER2") architecture and label taxonomy as our model — label-compatible with `AutoExtractor.from_pretrained` + `extract_entities_long` (confirmed shared mixin, see Summary) | [model card](https://huggingface.co/fastino/GLiNER2-Guardrails-PII-Multi/blob/aad696b2f6815e3dfc2d95908129eea5ed598562/README.md), [API](https://huggingface.co/api/models/fastino/GLiNER2-Guardrails-PII-Multi) |
| `fastino/gliner2.5-multi-v1` | 287,355,159 (~287M) | Card tags: `multilingual`, `en` only — FR/ES/IT **not confirmed** in metadata | General-purpose, user-defined labels; no fixed PII taxonomy, no `address`/`city`/`person` schema shipped | Apache 2.0 | Boundary architecture, introduced in `gliner2==2.0.0` itself (GitHub tag [`v2.0.0`](https://github.com/fastino-ai/GLiNER2/releases/tag/v2.0.0): "gliner2.5 release"). `AutoExtractor.from_pretrained` dispatches to `BoundaryExtractor`, which also mixes in `ExtractorRuntimeMixin`, so `extract_entities_long` is present with the same signature — architecturally loadable, but has no PII labels to load in the first place | [model card](https://huggingface.co/fastino/gliner2.5-multi-v1/blob/2ca71aafb3446d9014e1c55c7ff51c9bc7209c47/README.md), [API](https://huggingface.co/api/models/fastino/gliner2.5-multi-v1) |
| `fastino/gliner2.5-base-v1` | 194M | English only | General-purpose, user-defined labels; its own model card calls it "the everyday English model for schema-driven NER, classification, records, and relations" / "Default English multi-task checkpoint" | Apache 2.0 | Excluded — no FR/ES/IT | [model card](https://huggingface.co/fastino/gliner2.5-base-v1) |
| `fastino/gliner2.5-small-v1` | 73.9M | English only | General-purpose, user-defined labels | Apache 2.0 | Excluded — no FR/ES/IT | [model card](https://huggingface.co/fastino/gliner2.5-small-v1) |
| `fastino/GLiNER2.5-Decide`, `-multi-Decide`, `-Decide-1B` | 0.3B–1B | EN (multi variant: multilingual) | **Not entity extraction** — classifiers for intent/sentiment/moderation routing; card states "does not reason, explain, or answer open questions" | Apache 2.0 | Out of scope — wrong task type | [model card](https://huggingface.co/fastino/GLiNER2.5-Decide) |
| `fastino/gliner2-base-v1`, `gliner2-multi-v1`, `gliner2-large-v1` | 0.2B–0.5B | Base/multilingual general NER, no PII fine-tune | Generic entity types via schema | Apache 2.0 | Superseded for our purpose by the two PII-specific fine-tunes above | [org listing](https://huggingface.co/api/models?author=fastino&limit=500) |
| `fastino/gliguard-LLMGuardrails-300M`, `Fastino-Nemotron-3.5-Lightning-{Finance,Healthcare}` | — | — | Inferred from name only, not verified here: an LLM-guardrail classifier and two domain-specific LLMs — not GLiNER NER models | — | Out of scope | [org listing](https://huggingface.co/api/models?author=fastino&limit=500) |

Notes on the table:
- Param counts for the three candidates actually compared (current model, `GLiNER2-Guardrails-PII-Multi`,
  `gliner2.5-multi-v1`) are exact `safetensors` totals from the HF API
  (`?expand[]=safetensors` on each model's `/api/models/<id>` endpoint), not the rounded "0.3B"
  bucket shown on the org's model-list page. The excluded/out-of-scope rows' params, languages and
  license are as stated on each model's own card or the org listing, not independently re-verified
  against `safetensors`.
- Fastino's own SPY-benchmark number for the current model: average F1 0.477 across domains,
  with legal-domain precision/recall 0.346/0.750 and medical-domain 0.369/0.686
  ([model card](https://huggingface.co/fastino/gliner2-privacy-filter-PII-multi/blob/1cb4166094dc58fa8d836429f060d6c95f62b495/README.md)).
  SPY tests legal and medical prose, not source code; it's not comparable to our
  [`docs/ner-model-benchmark.md`](../ner-model-benchmark.md) numbers (100% / 94% PERSON, 100% / 93%
  LOCATION at our tuned cutoffs).
- `GLiNER2-Guardrails-PII-Multi`'s own card gives no numeric side-by-side table for its "no
  regression" claim, only the prose claim itself
  ([model card](https://huggingface.co/fastino/GLiNER2-Guardrails-PII-Multi/blob/aad696b2f6815e3dfc2d95908129eea5ed598562/README.md)).
- Weight-identity check: `model.safetensors` has LFS object id
  `0280f6f39f6012da50b6640bad438d9b7e763a1b0102094115d1b710c4dd79b6` at both our pinned revision
  ([`36126f6`](https://huggingface.co/api/models/fastino/gliner2-privacy-filter-PII-multi/tree/36126f612f1f9e376dc2c25b297d827912effef4))
  and current HEAD
  ([`1cb4166`](https://huggingface.co/api/models/fastino/gliner2-privacy-filter-PII-multi/tree/main)) —
  confirmed identical, not inferred from commit messages.
- `extract_entities_long` and its shared-mixin status were confirmed by reading the `gliner2==2.0.0`
  source directly (downloaded from GitHub tag `v2.0.0`): defined at
  [`gliner2/inference/runtime.py#L1299`](https://github.com/fastino-ai/GLiNER2/blob/v2.0.0/gliner2/inference/runtime.py#L1299)
  in `ExtractorRuntimeMixin`; mixed in by
  [`SpanExtractor(ExtractorRuntimeMixin, SpanExtractorModel)`](https://github.com/fastino-ai/GLiNER2/blob/v2.0.0/gliner2/inference/engine.py#L35)
  and by
  [`BoundaryExtractor(ExtractorRuntimeMixin, BoundaryExtractorModel)`](https://github.com/fastino-ai/GLiNER2/blob/v2.0.0/gliner2/models/boundary/engine.py#L48).
- Guardrails' `base_model` field (per its
  [API metadata](https://huggingface.co/api/models/fastino/GLiNER2-Guardrails-PII-Multi)) points to
  `fastino/gliner2-base-v1`, listed elsewhere in the org catalog at 0.2B params — smaller than
  Guardrails' own measured 307M, so the fine-tune added parameters or the base model's own listed
  size is approximate. Our currently deployed model's `base_model` field is not set at all (`null`),
  so "same base checkpoint" between the two PII models is not something the metadata confirms either
  way.
- The one commit between our pinned revision and current HEAD was confirmed directly against the
  [commits API](https://huggingface.co/api/models/fastino/gliner2-privacy-filter-PII-multi/commits/main),
  not just a summarized page: `1cb4166 "Point the fine-tune banner to agent.fastino.ai"` sits
  immediately above `36126f6 "Update the Fastino GLiNER skill"` (our pin) in the commit list.

## Recommendation

**The current model stays. Nothing here warrants a PII re-benchmark right now.**

`GLiNER2-Guardrails-PII-Multi` is the only candidate that's actually comparable to what we run —
same size, same label taxonomy, confirmed loadable through the exact `AutoExtractor` +
`extract_entities_long` call our recognizer already uses. But it only claims to
*match* our current model's PII accuracy, on a benchmark (SPY, legal/medical prose) that doesn't
test what ours does: code false positives and file-validity-after-masking. A model that "doesn't
regress" on someone else's prose benchmark is not evidence it holds our 100%-precision PERSON/LOCATION
cutoffs on code, and re-tuning those cutoffs (`docs/ner-model-benchmark.md#recommended-gliner2-cutoffs`)
for a different checkpoint is a full `task pii-bench` run, not a threshold copy — LOCATION's cutoff
already sits 0.01 above cloud-region-ID false positives on the *current* checkpoint's score
distribution, which is fine-tune-specific.

**The trigger that would justify running the benchmark against it:** if the gateway ever needs
prompt/response moderation (`prompt_safety`, `jailbreak_detection`, etc.) as a requirement,
`GLiNER2-Guardrails-PII-Multi` becomes worth a `task pii-bench` run plus a full cutoff re-tune,
since it would let one model replace two. Absent that requirement, its only advantage over the
current model is unused capability, which isn't a reason to re-benchmark on its own.

`fastino/gliner2.5-multi-v1` is not worth benchmarking. It's architecturally loadable, but ships
with no PII taxonomy, no published NER numbers, and unconfirmed FR/ES/IT support — using it means
building and tuning a PERSON/LOCATION schema from scratch with zero prior evidence it avoids the
`fullName`/`city` code-token false positives that eliminated GLiNER v1, not a drop-in swap.
