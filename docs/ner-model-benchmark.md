# NER model benchmark: PERSON and LOCATION masking in coding workflows

*2026-09-25 · Presidio analyzer variants compared with `task pii-bench`*

## Executive summary

**GLiNER2 is the only model that masks names and places without breaking the files they sit in,
but it can't run on the gateway's hot path on CPU as-is.**

| | spaCy EN *(gateway today)* | spaCy multilingual* | GLiNER v1 | **GLiNER2** |
|---|---|---|---|---|
| PERSON: precision / recall | 60% / 75% | 58% / 85% | 99% / 95% | **100% / 94%** |
| LOCATION: precision / recall | 54% / 46% | 28% / 69% | 42% / 98% | **100% / 93%** |
| Files still valid after masking (of 27) | 8 | 5 | 8 | **27** |
| Code tokens masked by mistake | many (`s.Carrier`, `jsonencode`, `s.City`) | worst (`nil`, `None`, `String`, `Optional`) | many (`city`, `Customer`, `fullName`) | **none** |
| Time per `/analyze` call (CPU, ~2 KB file) | ~35 ms | ~40 ms | ~3.6 s | ~1.5 s |

Scores use each model's best cutoffs, except GLiNER2, which uses the recommended cutoffs below.
\*spaCy multilingual was told each file's language in advance, which the gateway can't do, so its
numbers are an upper bound.

- **spaCy (current gateway)** misses 1 in 4 names and more than half of the places. It also
  masks code identifiers, which breaks 19 of 27 files: code no longer compiles, a CSV row loses
  its id, and **two JSON records silently disappear**.
- **GLiNER v1** finds almost everything, but masks any identifier that *means* "name" or "city".
  No cutoff fixes LOCATION (best precision 42%).
- **GLiNER2** at PERSON ≥ 0.85 / LOCATION ≥ 0.96 masked no code token and kept all 27 files
  valid, in French, English, Spanish and Italian. Its misses are consistent and explainable (see
  below).
- **Blocker:** GLiNER2 on CPU takes about 5 s per 2 KB and ~26 s per 10 KB, and a 40 KB request
  failed. Claude Code re-sends tens of KB on every request, so deploying it in place of spaCy
  would make most gateway requests fail or time out. It needs a GPU, a faster runtime, or a
  narrower scan scope before it can go on the request path (see
  [Deployment options](#deployment-options)).

## What was measured

- **Entities:** PERSON and LOCATION only, the NER-driven entities that are hardest to mask.
  Emails, phones, cards and IBANs come from Presidio's pattern recognizers, which every variant
  keeps unchanged.
- **Variants:** the same pinned Presidio analyzer image. Only the NER component differs.

  | Variant | NER |
  |---|---|
  | spaCy EN | `en_core_web_lg` |
  | spaCy multilingual | + `fr/es/it_core_news_md` |
  | GLiNER v1 | `urchade/gliner_multi_pii-v1` |
  | GLiNER2 | `fastino/gliner2-privacy-filter-PII-multi` |

- **Data:** 27 files, 148 PII values.
  - 24 realistic source files (Terraform, TypeScript, Python, Go, Java, Rust) × 4 natural
    languages (FR/EN/ES/IT). PII sits only where it shows up in real code: author comments, test
    fixtures, seed data. It's surrounded by identifiers that look like names or places
    (`dallasRetryPolicy`, `paris_gateway`, `s.City`, `nil`).
  - The `task pii-score` dataset exported as CSV, Markdown and minified JSON.
- **Scoring:** a detection counts only if it overlaps a real value of the *same* type.
  Everything else in a file is a false positive, so masking a code token counts against the
  model.
- **Structure check:** each file is masked with the model's detections (`<PERSON>`,
  `<LOCATION>`), then CSV/Markdown/JSON must parse with the same rows and ids, and code must
  compile (`terraform fmt`, `py_compile`, `gofmt`, `javac`, `rustc`). No TypeScript compiler was
  available, so TypeScript uses a heuristic: masked text must sit inside a string or comment.
  Masking only the true values keeps all 27 files valid, so every failure comes from the model.

## Results

### Precision / recall by language (best cutoff per model; GLiNER2 at the recommended 0.85 / 0.96)

| Model | PERSON FR | PERSON EN | PERSON ES | PERSON IT | LOCATION FR | LOCATION EN | LOCATION ES | LOCATION IT |
|---|---|---|---|---|---|---|---|---|
| spaCy EN | 44 / 44 | 63 / 83 | 54 / 83 | 57 / 89 | 40 / 31 | 67 / 62 | 55 / 46 | 25 / 23 |
| spaCy multi* | 43 / 94 | 63 / 83 | 56 / 83 | 62 / 89 | 15 / 77 | 67 / 62 | 32 / 69 | 24 / 62 |
| GLiNER v1 | 94 / 94 | 100 / 89 | 100 / 100 | 100 / 100 | 42 / 100 | 36 / 100 | 37 / 100 | 41 / 100 |
| GLiNER2 | 100 / 94 | 100 / 89 | 100 / 94 | 100 / 94 | 100 / 100 | 100 / 92 | 100 / 100 | 100 / 100 |

Values are precision / recall, in %. spaCy is weakest in French and Italian. GLiNER2 is stable
across all four languages.

### Files broken after masking (27 files)

| Model | CSV | JSON | MD | Terraform | Go | Java | Python | Rust | TS |
|---|---|---|---|---|---|---|---|---|---|
| spaCy EN | ✗ | ✗ (records 3 and 24 lost) | ✓ | 4/4 ✗ | 4/4 ✗ | 4/4 ✗ | 4/4 ✗ | ✓ | 1/4 ✗ |
| spaCy multi* | ✗ | ✗ | ✓ | 4/4 ✗ | 4/4 ✗ | 4/4 ✗ | 4/4 ✗ | 3/4 ✗ | 1/4 ✗ |
| GLiNER v1 | ✓ | ✓ | ✓ | ✓ | 4/4 ✗ | 4/4 ✗ | 3/4 ✗ | 4/4 ✗ | 4/4 ✗ |
| **GLiNER2** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

How each model breaks things:
- **spaCy** masks spans that cross delimiters, so a CSV row loses its `5,"` prefix and JSON
  objects get merged. It also masks code tokens:

  | Original | Masked |
  |---|---|
  | `if s.Carrier == nil` | `if s.<PERSON> == nil` |
  | `jsonencode(` | `<PERSON>(` |

- **GLiNER v1** masks identifiers: `private final String <LOCATION>;`.
- **GLiNER2 at the gateway's current LOCATION cutoff (0.35)** also masks `city` in Java, which
  breaks all four Java files. At 0.96 it doesn't.

### Code tokens mis-flagged (gateway cutoffs)

| Model | Examples |
|---|---|
| spaCy EN | PERSON: `s.Recipient`, `Carrier`, `jsonencode`, `jordan-ci-admin` · LOCATION: `s.City`, `String city`, `paris`, and the name *Aurélien Dubois* |
| spaCy multi* | PERSON: `Maintainer`, `Carrier`, `None` · LOCATION: `nil`, `String`, `Shipment`, `Optional`, `.findFirst` |
| GLiNER v1 | PERSON: `customer`, `Customer`, `fullName`, `User` · LOCATION: `city` (×61), `country`, `Address`, `street` |
| GLiNER2 | PERSON: `fullName` (0.74–0.81) · LOCATION: `city`, `us-east-1a` (0.95), `eu-west-3a` (0.94), `eu-west-3` (0.93) |

## Recommended GLiNER2 cutoffs

| Entity | Cutoff | Why |
|---|---|---|
| PERSON | **0.85** | Highest code false positive is `fullName` at 0.81. Tuning on two languages and testing on the other two picks 0.83–0.88, always with 100% precision (recall 83–94%). |
| LOCATION | **0.96** | Cloud regions and availability zones score up to 0.95. Held-out tuning picks 0.96–0.98 with 100% precision (recall 92–100%). |

With these cutoffs, over all 27 files: PERSON **100% precision / 94% recall**, LOCATION **100% /
93%**, and **0 files broken**.

What it misses, and why:

| Entity | Missed values | Why |
|---|---|---|
| PERSON | The Javadoc `@author` name in `CustomerRepository.java`, in all four languages, plus one English Java fixture name (*Harriet Whitfield*) | The `@author` names score 0.48–0.73, so the model sees them but with low confidence. Harriet Whitfield scored 0.83, just under the cutoff. |
| LOCATION | *Sheffield* (0.96, at the edge); a long street address and "Shibuya, Tokyo" in the CSV/MD/JSON exports | Scored below 0.96. |

**Main risk:** cloud region IDs sit only 0.01 below the LOCATION cutoff. The follow-on "safe code
context" feature should exclude patterns like `^[a-z]{2}-[a-z]+-\d[a-z]?$` explicitly, rather than
rely on the cutoff.

**Caveats:**
- Small corpus (148 values). One value moves recall by about 1–5 points.
- The cutoffs were chosen on this corpus; held-out-language checks back them up but aren't a
  substitute for real traffic.
- Precision/recall use overlap matching, so a partially masked name counts as found.

## Latency

| Input | spaCy EN | GLiNER2 (CPU, 1 worker) |
|---|---|---|
| ~2 KB source file | ~35 ms | 1.5–5 s |
| 10 KB | — | 26 s |
| 40 KB | — | HTTP 500 (most likely the analyzer's 30 s worker timeout) |

GLiNER2 splits long text into chunks and runs the model on each, so time grows linearly with
input size. Memory is about 1.5–2 GB per worker, versus under 1 GB for spaCy.

## Deployment options

1. **Deploy as-is on CPU**, with long worker, probe and guardrail timeouts. Accurate, but most
   Claude Code requests would take tens of seconds or fail. Demo use only.
2. **Run GLiNER2 on a GPU** (or through ONNX with int8 quantization). This is the realistic
   production path; it needs a benchmark on the target hardware.
3. **Scan less text.** The gateway re-scans the whole conversation on every request. Scanning
   only new content (the latest user message and tool results) would cut the input per request
   by an order of magnitude. That needs a guardrail change in LiteLLM, not just a model swap.
4. **Two-stage (hybrid):** a fast first pass (spaCy or patterns) picks candidate spans, and
   GLiNER2 re-checks only those. This needs a custom recognizer.

## Outcome: deployed on RunPod (option 2)

GLiNER2 now runs as a RunPod serverless load-balancing endpoint on one GPU, restricted to EU data
centers (EU-RO-1, EU-CZ-1). It scales to zero. The gateway reaches it through an auth-adding nginx
proxy that keeps the `presidio-analyzer` Service name. Configuration:
[`infra/runpod/endpoint.yaml`](../infra/runpod/endpoint.yaml). Architecture:
[README](../README.md#pii-detection-gliner2-on-a-runpod-gpu).

Measured on the deployed endpoint (RTX A4500, EU-RO-1), including the network round trip from a
client in France, compared with the model alone on a laptop CPU (i7-8565U, 4 threads):

| Input | RunPod A4500 | Laptop CPU |
|---|---|---|
| 2 KB | 0.58 s | 2.61 s |
| 10 KB | 1.31 s | 11.57 s |
| 40 KB | 5.17 s | 55.23 s |

On a French Go file, it found every real name and city at 0.97–1.00, and the only code token above
0.35 was the variable `s` (0.36, far below the PERSON cutoff of 0.85).

Remaining gaps:
- **Latency.** Under 1 s only up to about 3–4 KB. The model still runs in 32-bit precision with the
  eager attention fallback. 16-bit precision (`gliner2` supports `quantize=True`) and scanning only
  new messages (option 3) are the next levers.
- **Cold start.** A fresh worker took about 6.5 minutes to become ready: waiting for a GPU, then
  unpacking the 9.6 GB image even from RunPod's cache. LiteLLM fails closed, so requests during a
  cold start are blocked.
- **GPU fork crash.** The first deployment failed with "Cannot re-initialize CUDA in forked
  subprocess": CUDA was initialized in gunicorn's master process before it forked the worker. It is
  fixed with `PYTORCH_NVML_BASED_CUDA_CHECK=1` and `PRESIDIO_DEVICE=cuda`, set in the endpoint
  configuration.

## Supply-chain notes

- `gliner2` installed on its own is **only a client for Fastino's hosted API** and would send the
  text being scanned off the machine. Only `gliner2[local]` runs locally.
- All benchmark images build `FROM` the pinned analyzer digest. They use hash-pinned pip
  packages with CPU-only torch, and model weights pinned to a Hugging Face commit and baked in at
  build time. The containers run in Hugging Face offline mode.
- Presidio's YAML recognizer config silently drops unknown fields (such as GLiNER's
  `model_name` and `entity_mapping`), so model settings live in small Python recognizer files
  (`bench/analyzers/*/`).
- Presidio's built-in `GLiNERRecognizer` also asks the model for every requested Presidio entity
  (phones, URLs, …). The benchmark limits it to its mapped labels so only the NER component
  differs between variants.

## Reproduce

```bash
task pii-bench                                          # all models, all languages (first run builds images)
BENCH_LANGS=fr BENCH_VARIANTS="spacy-en gliner2" task pii-bench
```

Outputs go to `.pii-score-out/bench/`: `report.txt`, `bench-results.json`, raw detections in
`raw/`, and every masked file per model and cutoff set in `masked/`.
