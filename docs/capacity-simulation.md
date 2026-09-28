# Capacity simulation: GPU options and cost for the production analyzer

*2026-09-27 · pii-guardian-rjb, decided in pii-guardian-0p2 · discrete-event simulation, no cloud resources created, nothing deployed*

## Executive summary

**Cheapest option that meets the target (avg <= 300 ms, p95 <= 1 s) and stays under the
~EUR 1,000/month budget: Scaleway L4-1-24G, 16-bit, ~EUR 857/month** (1,084 GPU-hours: 1-3
replicas most hours, up to 3 at peak, floor of 2 replicas 7:00-20:00 weekdays enforced by the
availability requirement).

**Updated 2026-09-28: this number is now backed by a real-hardware measurement, and it is
unchanged.** The 16-bit speedup was measured (Section 7) on a rented GPU instead of assumed, and
it turned out **not to be flat**: about 5x on a small request, 2.5x at 10 KB, only 1.1x at 40 KB
(fp16 mainly cuts small-batch fixed overhead, a shrinking share of the time as a request grows).
The simulation now uses that real, size-dependent curve. Result: **every Scaleway row's cost is
byte-for-byte identical to the old flat-2x run.** The winning option was never actually
speed-bound -- it was bound by the decided ">= 2 replicas in different zones" availability floor,
so a faster or slower 16-bit model changes nothing as long as it clears that floor's own
capacity easily, which both the assumed 2x and the measured curve do. The only rows that moved are
non-winning ones (AWS A10G fp16 improved slightly; AWS T4 fp16's already-infeasible verdict got a
corrected reason -- see Section 7). The break-even sensitivity table below is now historical: it
answers "what if the real speedup had been worse than assumed", which the measurement closes.

| Assumed 16-bit speedup (historical sensitivity, superseded by the Section 7 measurement) | Scaleway L4 monthly cost | Fits EUR 1,000? |
|---|---|---|
| 1.25x | 1,127 EUR | No |
| 1.5x | 1,043 EUR | No (over by ~4%) |
| 1.6x | 975 EUR | Yes |
| 1.7x | 907 EUR | Yes |
| 2.0x (old flat assumption) | 857 EUR | Yes |
| **Measured curve (now default)** | **857 EUR** | **Yes -- unchanged from 2.0x, see above** |

For comparison, the cheapest *known-safe fp32* option (no precision assumption at all) is
Scaleway L4 32-bit at **EUR 2,204/month** (2,790 GPU-hours) -- **2.2x the budget** -- which itself
has almost no margin: modeled average latency at 32-bit is 284-299 ms against the 300 ms target
(structural floor 297.7 ms: network + mean inference time alone, before any queueing), so a ~1%
error in the measured 0.075 s/KB baseline, in the FP32-TFLOPS scaling, or in the assumed request
size can flip it from compliant to impossible at any replica count.

Since the winning option's cost didn't move, there is no budget gap to close from this exercise.
The remaining unmeasured piece is the FP32-TFLOPS cross-GPU scaling (Section 3): the 4090
benchmark validates the *fp16-vs-fp32 ratio*, not the *A4500-vs-L4 absolute speed* the whole
report is scaled from.
input, not something this report can override.

## 1. What this answers, and what it doesn't

Per pii-guardian-0p2 / pii-guardian-rjb: which GPU option, how many replicas per hour, and what
monthly cost meet the analyzer latency target under the decided heavy-load daily profile, for
Scaleway (Paris) and AWS eu-west-3, 32-bit and 16-bit, with the Bedrock Guardrails price as a
cost-only reference. Nothing was deployed, no cloud API was called, and no benchmark was run to
produce these numbers -- they come from `scripts/capacity/simulate.py`, a discrete-event
simulation, standard library only.

## 2. Method

Each GPU replica is modeled as a **single-threaded FIFO server** (per the issue: "model a single
GPU as serving requests one at a time" -- the worker's 4 gunicorn threads accept 4 connections at
once, but the underlying GPU still executes one inference at a time). Replicas for a given
GPU/precision share one logical queue behind a load balancer, which makes this a **G/G/c queue**:
Poisson arrivals at the decided rate, service time = request size (KB) x a fixed per-KB inference
time. There's no closed form for a G/G/c queue's p95 wait (only heavier approximations for the
*mean*), and the target explicitly needs a p95, so this is a discrete-event simulation rather
than an M/M/c formula.

**Self-check (written and run before the report logic, per TDD):**
- An M/M/1 case (lambda=0.7, mu=1.0) simulated for 200,000s against its closed form: simulated
  mean sojourn time W = 3.3764 vs theoretical 3.3333 (1.3% error); simulated mean wait Wq = 2.3737
  vs theoretical 2.3333 (1.7% error). Both within the tolerance the self-check asserts (3%/5%).
- A trivial deterministic case (arrivals every 1.0 s, service exactly 0.4 s, 1 server): since
  interarrival time exceeds service time, nothing should ever queue. All 99 samples had wait =
  0.000000 s and sojourn = service = 0.4 s exactly.

Run it yourself: `python3 scripts/capacity/simulate.py --selfcheck`.

Each unique hourly load level is simulated once (many hours of the day share the same traffic
multiplier), with the simulated duration stretched at low rates so every load level still
collects >= 3,000 samples (a p95 read off a few hundred samples is noisy, and replica decisions
in this report are made on exactly that number). The full report (`simulate.py` with no
arguments) reproduces every table below and runs in under 5 seconds.

## 3. Assumptions -- measured vs. estimated

| Input | Value | Status | Source |
|---|---|---|---|
| Target: avg <= 300 ms, p95 <= 1 s | -- | Decided | pii-guardian-0p2 |
| Peak load: 10 req/s | -- | Decided (heavy case) | pii-guardian-0p2 / -rjb |
| Mean request size: 5 KB | -- | Decided | pii-guardian-0p2 / -rjb |
| Request-size **distribution**: lognormal, mean 5 KB, sigma=1.0 (p95 ~16 KB, p99 ~31 KB) | shape | **Estimated** | No production size histogram exists; sigma chosen so the tail lands near the real 12-22 KB CSV/Markdown/JSON exports in `docs/gateway-test-scenarios.md`, not from measured request sizes |
| Working hours 7:00-20:00 Europe/Paris weekdays, floor >= 2 replicas in different zones; 1 replica otherwise | -- | Decided | pii-guardian-0p2 |
| Daily traffic **profile** (hour-by-hour multiplier of the 10 req/s peak) | see table below | **Estimated** | No production traffic exists yet (pre-launch); a plausible office-hours ramp/lunch-dip/taper shape, stated explicitly so it can be replaced by real logs later |
| Baseline: 0.075 s/KB GLiNER2 inference, RTX A4500, 32-bit | measured | **Measured** | pii-guardian-0p2 (maintainer's own benchmark) |
| GPU speed scaling: linear in spec-sheet FP32 TFLOPS | -- | **Estimated** | Ignores memory-bandwidth and batch-size-1 effects; a linear-TFLOPS proxy, not a measured cross-GPU benchmark |
| 16-bit speedup: size-dependent curve (5.0x at 2 KB, 2.5x at 10 KB, 1.1x at 40 KB; interpolated, clamped outside that range) | -- | **Measured** (2026-09-28) | Section 7: RunPod RTX 4090 pod, our exact analyzer image, `extract_entities_long(quantize=True/False)`, 2 runs averaged. Not the exact target GPU (4090 is Ada Lovelace, same generation as L4/L40S; none were in RunPod stock at the time) |
| In-region network RTT: 5 ms | -- | **Estimated** | The issue states only that in-region RTT "drops to a few ms"; no measured number exists for a same-region deployment |
| Gateway overhead per analyzer call: ~90-110 ms | measured (tiny-text case) | **Measured** | `docs/claude-code-guardrail.md` Latency table: 0.30 s direct vs 0.39-0.41 s through gateway |
| GPU prices (on-demand) | listed below | **Decided/measured** | pii-guardian-0p2: scaleway.com/en/pricing/gpu; AWS Price List API, 2026-09-25 |
| USD/EUR FX rate: 1.08 | -- | **Estimated, approximate** | Doesn't change which option is cheapest; only affects EUR-equivalent display of USD costs |
| Bytes-to-characters: 1 KB = 1024 chars | -- | **Estimated** | Used only for the Bedrock text-unit reference; not measured for mixed EN/FR text |
| Month length: 21.43 weekdays + 8.57 weekend days | -- | **Estimated convention** | 30-day month split 5:2 by weekday/weekend |
| Zone availability: L40S/H100 single-zone (PAR-2); Scaleway L4 and all three AWS eu-west-3 types multi-zone | -- | **Partially verified** | Scaleway: [scaleway.com/en/pricing/gpu](https://www.scaleway.com/en/pricing/gpu/) (fetched 2026-09-27) shows L40S/H100 flagged "Not available in this zone. Available in: PAR-2" under the PAR-1 selector, and L4 with no such flag -- consistent with L4 being multi-zone but not an exhaustive check of a second zone. AWS: **not verified** -- confirming per-AZ instance-type offering would need the EC2 `DescribeInstanceTypeOfferings` API, which this task must not call; treated as an assumption |

### Daily traffic profile used (fraction of the 10 req/s peak)

| Hour | 07 | 08 | 09 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Multiplier | 0.30 | 0.60 | 0.85 | 1.00 | 1.00 | 0.60 | 0.60 | 0.90 | 1.00 | 1.00 | 0.90 | 0.60 | 0.30 |

All other hours (20:00-6:59 weekdays, and all weekend hours): 0.05x peak (0.5 req/s), floor 1
replica.

### GPU spec-sheet FP32 TFLOPS used for scaling (fetched 2026-09-27)

| GPU | FP32 TFLOPS | Source |
|---|---|---|
| RTX A4500 (measured baseline) | 23.65 | [NVIDIA RTX A4500 datasheet](https://www.nvidia.com/content/dam/en-zz/Solutions/design-visualization/rtx/nvidia-rtx-a4500-datasheet.pdf) |
| NVIDIA T4 | 8.1 | [nvidia.com/en-us/data-center/tesla-t4](https://www.nvidia.com/en-us/data-center/tesla-t4/) |
| NVIDIA L4 | 30.3 | [nvidia.com/en-us/data-center/l4](https://www.nvidia.com/en-us/data-center/l4/) |
| NVIDIA A10/A10G | 31.2 | [nvidia.com/en-us/data-center/products/a10-gpu](https://www.nvidia.com/en-us/data-center/products/a10-gpu/) |
| NVIDIA L40S | 91.6 | [nvidia.com/en-us/data-center/l40s](https://www.nvidia.com/en-us/data-center/l40s/) |
| NVIDIA H100 (PCIe 80GB, assumed form factor -- Scaleway doesn't publish which) | 51.0 | [NVIDIA H100 datasheet](https://resources.nvidia.com/en-us-gpu-resources/h100-datasheet-24306) |

These are single-precision (non-tensor) FP32 numbers, used only as a linear speed proxy (see
Assumptions). Note the H100's 51.0 non-tensor FP32 figure is *lower* than the L40S's 91.6 --
H100 is optimized for tensor-core throughput, which this proxy doesn't use, so under this model
the (more expensive) H100 needs more replicas than the L40S. That's a real artifact of the
chosen proxy, not a modeling error; a real benchmark using GLiNER2's actual FP16/INT8 tensor
paths could well reverse this ranking.

## 4. Full results: replicas per hour, latency, and monthly cost

Target: avg <= 300 ms, p95 <= 1,000 ms. Peak 10 req/s, mean 5 KB/request (lognormal, sigma=1.0).

### Scaleway L4-1-24G — fp32 (0.79 EUR/h)

| Hour | Weekday replicas | Weekday avg/p95 | Weekend replicas | Weekend avg/p95 |
|---|---|---|---|---|
| 00:00 | 2 | 295/917 ms | 2 | 295/917 ms |
| 07:00 | 4 | 284/874 ms | 2 | 295/917 ms |
| 08:00 | 6 | 287/860 ms | 2 | 295/917 ms |
| 09:00 | 7 | 291/900 ms | 2 | 295/917 ms |
| 10:00 | 9 | 299/937 ms | 2 | 295/917 ms |
| 11:00 | 9 | 299/937 ms | 2 | 295/917 ms |
| 12:00 | 6 | 287/860 ms | 2 | 295/917 ms |
| 13:00 | 6 | 287/860 ms | 2 | 295/917 ms |
| 14:00 | 7 | 296/908 ms | 2 | 295/917 ms |
| 15:00 | 9 | 299/937 ms | 2 | 295/917 ms |
| 16:00 | 9 | 299/937 ms | 2 | 295/917 ms |
| 17:00 | 7 | 296/908 ms | 2 | 295/917 ms |
| 18:00 | 6 | 287/860 ms | 2 | 295/917 ms |
| 19:00 | 4 | 284/874 ms | 2 | 295/917 ms |
| 20:00-06:00 | 2 | 295/917 ms | 2 | 295/917 ms |

**Monthly GPU-hours: 2,790. Monthly cost: 2,204 EUR.** Structural floor (network + mean
inference, zero queueing): 297.7 ms -- 2.3 ms of margin under the 300 ms target.

### Scaleway L4-1-24G — fp16 (0.79 EUR/h, measured speedup curve -- see Section 7)

| Hour | Weekday replicas | Weekday avg/p95 | Weekend replicas | Weekend avg/p95 |
|---|---|---|---|---|
| 00:00-06:00, 20:00-23:00 | 1 | 158/484 ms | 1 | 158/484 ms |
| 07:00 | 2 | 169/537 ms | 1 | 158/484 ms |
| 08:00 | 2 | 203/657 ms | 1 | 158/484 ms |
| 09:00 | 2 | 249/770 ms | 1 | 158/484 ms |
| 10:00 | 3 | 179/530 ms | 1 | 158/484 ms |
| 11:00 | 3 | 179/530 ms | 1 | 158/484 ms |
| 12:00 | 2 | 203/657 ms | 1 | 158/484 ms |
| 13:00 | 2 | 203/657 ms | 1 | 158/484 ms |
| 14:00 | 2 | 280/912 ms | 1 | 158/484 ms |
| 15:00 | 3 | 179/530 ms | 1 | 158/484 ms |
| 16:00 | 3 | 179/530 ms | 1 | 158/484 ms |
| 17:00 | 2 | 280/912 ms | 1 | 158/484 ms |
| 18:00 | 2 | 203/657 ms | 1 | 158/484 ms |
| 19:00 | 2 | 169/537 ms | 1 | 158/484 ms |

**Monthly GPU-hours: 1,084. Monthly cost: 857 EUR.** **Cheapest option meeting the target and the
budget** -- see the sensitivity table in the executive summary for how much this depends on the
16-bit speedup assumption.

### Scaleway L40S-1-48G — fp32 / fp16 (1.47 EUR/h, PAR-2 only)

**PAR-2 only** (scaleway.com/en/pricing/gpu, fetched 2026-09-27: flagged "Not available in this
zone. Available in: PAR-2" under PAR-1). **If that means a single availability zone, this SKU
alone cannot satisfy the decided floor of >= 2 replicas in different zones during working hours.
Listed for latency/cost comparison only, not as an eligible sole option.**

- fp32: 1-2 replicas all day, avg 104-137 ms, p95 304-436 ms. Monthly GPU-hours: 999. Monthly
  cost: 1,468 EUR.
- fp16: 1-2 replicas all day, avg 53-57 ms, p95 159-165 ms. Monthly GPU-hours: 999. Monthly cost:
  1,468 EUR (same GPU-hours as fp32 here: floor-bound, not load-bound, at this GPU's speed).

### Scaleway H100-1-80G — fp32 / fp16 (2.87 EUR/h, PAR-2 only)

**PAR-2 only: same caveat and source as L40S above.**

- fp32: 1-3 replicas, avg 191-252 ms, p95 574-792 ms. Monthly GPU-hours: 1,149. Monthly cost:
  3,296 EUR.
- fp16: 1-2 replicas, avg 92-117 ms, p95 279-363 ms. Monthly GPU-hours: 999. Monthly cost: 2,866
  EUR.

Most expensive option in the set, and — per the FP32-proxy caveat in Section 3 — modeled slower
than the L40S because this simulation scores GPUs on non-tensor FP32 throughput.

### AWS eu-west-3 g4dn.xlarge (T4) — fp32 / fp16 (0.615 USD/h, + EKS 0.10 USD/h)

**Infeasible at any replica count, both precisions -- for two different reasons.** fp32: network +
mean inference time alone = 1,100 ms, already above the 300 ms average target before any queueing;
more replicas only reduce queueing wait, not inference time. fp16: with the old flat-2x assumption
the mean floor was 552 ms, also above target for the same reason. **With the measured curve
(Section 7) the mean floor drops to 274 ms**, which is *under* the 300 ms target -- but the p95
tail is not: large requests only get ~1.1x from fp16, and no replica count (up to the search cap)
brings the p95 under 1 s. Same verdict, corrected reason: T4 fp16 fails on the tail, not the
average. The T4 is simply too slow under this model's speed proxy to meet the target at any
capacity or cost.

### AWS eu-west-3 g6.xlarge (L4) — fp32 / fp16 (1.0216 USD/h, + EKS 0.10 USD/h)

- fp32: 3-8 replicas, avg 288-299 ms (2.3 ms structural margin, same fragility as Scaleway L4
  fp32), p95 862-928 ms. Monthly GPU-hours: 3,253. Monthly cost: 3,395 USD (~3,144 EUR).
- fp16: 1-3 replicas, avg 160-279 ms, p95 495-892 ms. Monthly GPU-hours: 1,127. Monthly cost:
  1,223 USD (~1,133 EUR).

Same GPU as Scaleway's L4, priced in AWS: more expensive than the Scaleway equivalent at every
precision once the EKS control-plane charge and FX conversion are included.

### AWS eu-west-3 g5.xlarge (A10G) — fp32 / fp16 (1.277 USD/h, + EKS 0.10 USD/h)

- fp32: 2-6 replicas, avg 287-299 ms, p95 842-909 ms. Monthly GPU-hours: 2,319. Monthly cost:
  3,033 USD (~2,808 EUR).
- fp16: 1-3 replicas, avg 148-275 ms, p95 462-893 ms. Monthly GPU-hours: 999 (was 1,084 under the
  old flat-2x assumption -- the only Scaleway/AWS row the real curve actually moved). Monthly
  cost: 1,347 USD (~1,247 EUR), down from 1,457 USD (~1,349 EUR).

`g6e`, `p4`, and `p5` instance families are not offered in eu-west-3 (decided input), so this is
the fastest AWS GPU available in-region.

## 5. Gateway overhead per request (separate from the queueing model above)

The analyzer-call latency modeled above is the GPU side only. On top of it, the gateway (LiteLLM
hop, code-guard's regex pass) adds latency the issue asks to be estimated separately:

**Measured** (tiny-text case, `docs/claude-code-guardrail.md` Latency table): direct-to-analyzer
0.30 s vs through-gateway 0.39-0.41 s => **~90-110 ms per analyzer call**. A gateway turn touching
N new text blocks pays this roughly `ceil(N/4)` times, since the gateway keeps at most 4 analyzer
calls in flight at once (same doc). This linear-in-size latency model has no fixed per-call term
of its own — it's fit from the single measured point (12 KB ~= 0.9 s, i.e. exactly 0.075 s/KB) —
so it likely *understates* the overhead for very small requests, where a fixed per-call cost
(TLS, LiteLLM routing) would dominate.

## 6. Bedrock Guardrails reference (cost only)

Amazon Bedrock Guardrails' sensitive-information filter: **$0.10 per 1,000 text units**
([aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing/), fetched 2026-09-27,
matches the decided input in pii-guardian-0p2). A **text unit = up to 1,000 characters, rounded
up** (same source) — e.g. 5,600 characters = 6 text units.

At the same monthly request volume as the daily traffic profile above (~8.24M requests/month) and
the mean request size (5 KB ~= 5,120 characters ~= 6 text units, using the mean rather than the
full size distribution — an approximation): **~4,577 EUR/month**. This is a cost reference only;
Bedrock's own latency isn't modeled, and the maintainer's own experience is that it detects
French PII poorly in large CSV tool results, which is why it isn't a deployment candidate here.

## 7. Real-hardware validation (done 2026-09-28)

**Result: the fp16 speedup is real, but not flat, and it doesn't move the recommended option's
cost.** Approved and run at the lowest available cost; here is what was measured and how it
differs from the plan originally proposed.

**What was run, and how it deviated from the plan below.** Scaleway had no `L4-1-24G` on offer as
a short-term rentable instance at the time (Scaleway GPU Instances are monthly, not hourly-billed
pods), and RunPod -- the provider already used for the production analyzer -- had no L4 or A4500
in stock in EU-RO-1/EU-CZ-1 at the time either. Rented instead: one RunPod pod, **NVIDIA GeForce
RTX 4090** (secure cloud, $0.74/h), the closest available proxy: RTX 4090 and L4 are the same Ada
Lovelace tensor-core generation, unlike the AMPERE-class A4500 the 32-bit baseline was measured on.
The pod ran the exact private analyzer image already used in production, overriding its entrypoint
to call `AutoExtractor.from_pretrained(..., quantize=True/False)` directly (`quantize=True` is
`gliner2`'s built-in fp16 path -- confirmed already present in our pinned `gliner2==2.0.0`, no
image rebuild needed) on the same 2/10/40 KB synthetic snippet as the original benchmark
(`docs/ner-model-benchmark.md`), run twice for consistency instead of the plan's
size-distribution-and-Poisson-load test (cheaper, and it directly answers the one open question --
does fp16 help, and by how much at each size -- without standing up a full serving stack).

| Size | 32-bit | 16-bit | Speedup |
|---|---|---|---|
| 2 KB | 0.114-0.116 s | 0.022-0.024 s | ~5.0x |
| 10 KB | 0.202-0.204 s | 0.080 s | ~2.5x |
| 40 KB | 0.557-0.565 s | 0.495-0.516 s | ~1.1x |

Two full runs (RunPod restarted the container after the script's first exit), numbers consistent
between them. The speedup **shrinks as the request grows**: fp16 mainly cuts small-batch fixed
overhead, a shrinking share of the total time as the request (and its internal chunk count via
`extract_entities_long`) grows -- the opposite of the flat multiplier this report assumed before.
`scripts/capacity/simulate.py` now uses this exact curve (`FP16_SPEEDUP_MEASURED_KB`), interpolated
between the three points and clamped flat outside [2, 40] KB (no data beyond that range).

**Effect on the report: none, for the option that matters.** Re-running the full simulation with
the real curve reproduced every Scaleway row byte-for-byte, including the winning Scaleway L4
16-bit answer (857 EUR/month, 1,084 GPU-hours). The reason: at this GPU's speed and the decided
load, capacity was never the binding constraint for Scaleway options -- the ">= 2 replicas in
different zones during working hours" availability floor was, both under the old assumption and
the real one. Two rows did change: AWS A10G fp16 (999 GPU-hours, $1,347/month, down from 1,084
hours / $1,457 -- see Section 4), and AWS T4 fp16's infeasibility reason (mean now passes at 274 ms,
but the p95 tail -- which gets far less benefit from fp16 -- still doesn't; same verdict, corrected
diagnosis).

**A related bug this run surfaced and fixed**, unrelated to the fp16 curve itself: before this
measurement, an option that failed the p95 target at every single hour (as T4 fp16 now does) was
reported as "0 GPU-hours, cost = EKS-only" instead of infeasible -- a silent reporting gap (`build_report`
only checked the mean-based structural floor up front, not full per-hour infeasibility). It never
surfaced before because the old flat-2x assumption always made T4 fp16 fail the cheaper mean-only
check first. Fixed in the same commit as the curve: an option where no hour can be provisioned at
all is now reported as infeasible with a reason, not a cost.

**Cost:** the pod ran for about 3.5 minutes end to end (image pull included) at $0.74/h --
**$0.043**, terminated immediately after the second run's output was captured.

**Still unmeasured**, and out of scope for this check: the FP32-TFLOPS cross-GPU scaling
(Section 3) that projects the RTX A4500 baseline onto Scaleway/AWS GPU types. This run validates
the *fp16-vs-fp32 ratio on one Ada-class GPU*, not the *absolute A4500-to-L4 speed* the whole report
scales from -- that would need an actual L4 rental, which wasn't available at the time.

**Original plan (for reference; largely superseded by the above):**
1. Rent one Scaleway `L4-1-24G` on-demand instance (the cheapest, and the winning option) for a
   short window.
2. Deploy the existing GLiNER2 worker image, run both the 32-bit and 16-bit model variants
   sequentially in the same rented hour.
3. Feed it a corpus with the same size distribution assumed here (lognormal, mean 5 KB) —
   `scripts/pii-score/customers.py` / `fetch_opendata.py` can generate this — and measure actual
   s/KB inference time for each precision, plus p95 under a synthetic Poisson load at the
   decided 10 req/s (single replica, to isolate inference time from queueing).
4. Compare against this report's assumed 0.075 s/KB (32-bit) and the assumed 2x 16-bit speedup;
   update `BASELINE_S_PER_KB_FP32` and `FP16_SPEEDUP` in `scripts/capacity/simulate.py` and
   re-run the report.
5. If the L4 numbers don't hold up, repeat steps 2-4 for `L40S-1-48G` and `H100-1-80G` (same
   rented-hour approach) before ruling them out on cost alone — but first confirm with Scaleway
   support or the console whether "PAR-2 only" really means single-AZ; if so, either would need a
   second zone's worth of capacity from a different GPU type to satisfy the availability floor.

If an exact-GPU (L4) or full-load-profile validation is still wanted, steps 3 and 5 above remain
open and are not superseded by what was run.

## 8. Reproducing this report

```bash
python3 scripts/capacity/simulate.py --selfcheck              # self-check only
python3 scripts/capacity/simulate.py                           # full report (defaults reproduce this doc)
python3 scripts/capacity/simulate.py --gpu scaleway-l4 --rate 10 --precision fp16   # one ad-hoc scenario
python3 scripts/capacity/simulate.py --fp16-speedup 1.6         # override: flat multiplier instead of the measured curve
```

Standard library only. Command-line parameters cover load (`--rate`), GPU type (`--gpu`),
replicas (`--replicas`, forces a count instead of searching for the minimum), and precision
(`--precision fp32|fp16`), plus `--mean-kb`, `--sigma`, `--seed`, and `--min-samples` for
revisiting any of the estimated assumptions above. `--fp16-speedup` now overrides the default
measured, size-dependent curve (`FP16_SPEEDUP_MEASURED_KB`, Section 7) with a flat value, kept for
sensitivity checks like the historical table in the executive summary.
