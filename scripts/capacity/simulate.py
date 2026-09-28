#!/usr/bin/env python3
"""Capacity and cost simulation for the production PII analyzer (pii-guardian-rjb).

Answers: which GPU option, how many replicas per hour, and what monthly cost meet the
latency target (avg <= 300 ms, p95 <= 1 s per analyzer call) under the heavy-load daily
traffic profile, for Scaleway and AWS eu-west-3 GPU options, 32-bit and 16-bit.

  simulate.py                              # self-check, then the full report (reproduces docs/capacity-simulation.md)
  simulate.py --selfcheck                  # only run the M/M/1 and deterministic self-checks
  simulate.py --gpu scaleway-l4 --rate 10 --precision fp32   # one ad-hoc scenario

Every number is either MEASURED (cited to a repo doc or the issue) or ESTIMATED (a stated
assumption, kept as a CLI parameter so it can be revisited). See docs/capacity-simulation.md
for the full assumptions list and citations. Standard library only.

Core model: each GPU replica is a single-threaded FIFO server (the issue: "model a single GPU
as serving requests one at a time"); replicas share one logical queue behind a load balancer.
That is a G/G/c queue -- Poisson arrivals, service time = request size (KB, drawn from a
lognormal so occasional large tool results create a heavy tail) times a fixed per-KB inference
time. There's no closed form for G/G/c p95 wait, so this is a discrete-event simulation rather
than an M/M/c formula.
"""
import argparse
import heapq
import math
import random
import sys
import zlib

# ---------------------------------------------------------------------------
# Decided inputs (pii-guardian-0p2 / pii-guardian-rjb) -- do not change without re-deciding.
# ---------------------------------------------------------------------------

TARGET_AVG_S = 0.300
TARGET_P95_S = 1.000
PEAK_RATE_RPS = 10.0          # measured/decided: "heavy case, ~10 requests/s at peak"
MEAN_REQUEST_KB = 5.0          # decided: "~5 KB of new text per request"

# Measured baseline (issue): 0.075 s/KB of GLiNER2 inference on an RTX A4500, 32-bit.
BASELINE_GPU_FP32_TFLOPS = 23.65  # NVIDIA RTX A4500 datasheet (single-precision FP32), measured/cited
BASELINE_S_PER_KB_FP32 = 0.075

# GPU options. fp32_tflops = spec-sheet single-precision TFLOPS, used only to scale the
# measured baseline (ESTIMATED: assumes GLiNER2 inference throughput scales linearly with
# spec-sheet FP32 TFLOPS, which ignores memory-bandwidth and batch-size-1 effects).
# Sources fetched 2026-09-27 from each vendor's public spec page (see docs/capacity-simulation.md).
# multi_zone: for Scaleway, checked against scaleway.com/en/pricing/gpu (fetched 2026-09-27):
# under the PAR-1 zone selector, L40S-1-48G and H100-1-80G both show "Not available in this
# zone. Available in: PAR-2" (single zone, matching the decided "PAR-2 only" prices); L4-1-24G
# shows no such restriction under PAR-1. That's consistent with L4 being multi-zone but doesn't
# exhaustively confirm a second zone, so treat multi_zone=True for L4 as reasonably supported,
# not proven. For AWS eu-west-3, per-AZ instance-type offering was NOT verified (would need the
# EC2 DescribeInstanceTypeOfferings API, i.e. a cloud API call, which this task must not make)
# -- multi_zone=True for the AWS options is an UNVERIFIED assumption, stated as such in the doc.
GPUS = {
    "scaleway-l4": dict(provider="Scaleway", label="L4-1-24G", fp32_tflops=30.3,
                         price=0.79, currency="EUR", note="", multi_zone=True),
    "scaleway-l40s": dict(provider="Scaleway", label="L40S-1-48G", fp32_tflops=91.6,
                           price=1.47, currency="EUR", note="PAR-2 only", multi_zone=False),
    "scaleway-h100": dict(provider="Scaleway", label="H100-1-80G", fp32_tflops=51.0,
                           price=2.87, currency="EUR", note="PAR-2 only", multi_zone=False),
    "aws-t4": dict(provider="AWS eu-west-3", label="g4dn.xlarge (T4)", fp32_tflops=8.1,
                    price=0.615, currency="USD", note="+ EKS $0.10/h", multi_zone=True),
    "aws-l4": dict(provider="AWS eu-west-3", label="g6.xlarge (L4)", fp32_tflops=30.3,
                    price=1.0216, currency="USD", note="+ EKS $0.10/h", multi_zone=True),
    "aws-a10g": dict(provider="AWS eu-west-3", label="g5.xlarge (A10G)", fp32_tflops=31.2,
                      price=1.277, currency="USD", note="+ EKS $0.10/h", multi_zone=True),
}
EKS_HOURLY_USD = 0.10

# MEASURED 2026-09-28 on a rented RunPod RTX 4090 pod (Ada Lovelace -- the closest architecture
# generation to the L4/L40S options above; A10G/T4 are older Ampere/Turing), running our exact
# analyzer image's extract_entities_long(quantize=True) vs quantize=False, same 2/10/40 KB
# snippet as the original benchmark (docs/ner-model-benchmark.md). Two runs, averaged:
#   2 KB: fp32 0.115s / fp16 0.023s = 5.00x   10 KB: 0.203s / 0.080s = 2.54x
#   40 KB: 0.561s / 0.506s = 1.11x
# The speedup is NOT flat: fp16 mainly cuts the small-batch fixed overhead, which is a shrinking
# share of the time as the request (and its internal chunk count) grows. Not the exact target
# GPU (RTX 4090, not L4/L40S) and not the production model's real traffic shape -- see
# docs/capacity-simulation.md "Real-hardware validation" for the caveats.
FP16_SPEEDUP_MEASURED_KB = {2.0: 5.00, 10.0: 2.54, 40.0: 1.11}

# None = use the measured curve above. --fp16-speedup sets this to a flat override, for the
# sensitivity sweep this replaced (docs/capacity-simulation.md keeps that table for reference).
FP16_SPEEDUP_OVERRIDE = None


def fp16_speedup_at(kb):
    """Piecewise-linear interpolation of FP16_SPEEDUP_MEASURED_KB, clamped flat outside the
    measured 2-40 KB range (no data beyond it, so no extrapolated slope either)."""
    if FP16_SPEEDUP_OVERRIDE is not None:
        return FP16_SPEEDUP_OVERRIDE
    points = sorted(FP16_SPEEDUP_MEASURED_KB.items())
    if kb <= points[0][0]:
        return points[0][1]
    if kb >= points[-1][0]:
        return points[-1][1]
    for (k0, s0), (k1, s1) in zip(points, points[1:]):
        if k0 <= kb <= k1:
            return s0 + (kb - k0) / (k1 - k0) * (s1 - s0)

# ESTIMATED: no production traffic exists yet (pre-launch simulation). A plausible office-hours
# shape -- ramps up, dips at lunch, tapers off -- as a fraction of the 10 req/s peak. State of
# the art here would be real access logs; there are none, so this is a labelled assumption.
WEEKDAY_HOUR_MULTIPLIER = {
    7: 0.30, 8: 0.60, 9: 0.85, 10: 1.00, 11: 1.00, 12: 0.60, 13: 0.60,
    14: 0.90, 15: 1.00, 16: 1.00, 17: 0.90, 18: 0.60, 19: 0.30,
}
OFF_HOURS_MULTIPLIER = 0.05  # weekday nights + all weekend: "much lower traffic outside" (estimated)
WEEKDAY_DAYS_PER_MONTH = 30 * 5 / 7   # ~21.43, estimated month-averaging convention
WEEKEND_DAYS_PER_MONTH = 30 * 2 / 7   # ~8.57

# ESTIMATED: in-region RTT ("drops to a few ms in-region" per the issue; no measured number).
NETWORK_RTT_S = 0.005

# ESTIMATED: lognormal shape for request size. Mean pinned to the decided 5 KB; sigma=1.0 is
# chosen so p95 (~16 KB) and p99 (~31 KB) land near the real exports seen in
# docs/gateway-test-scenarios.md (12-22 KB CSV/Markdown/JSON), i.e. "large tool results" in the
# tail. Not a measured request-size histogram -- kept as a CLI parameter.
SIGMA_KB = 1.0

FX_USD_PER_EUR = 1.08  # ESTIMATED, approximate; doesn't change which option is cheapest

# Measured (docs/claude-code-guardrail.md, Latency table, tiny-text case): gateway adds the
# LiteLLM hop + regex scan on top of the direct analyzer call. Direct 0.30 s vs through-gateway
# 0.39-0.41 s => ~90-110 ms. This is reported alongside the sim, not fed into the queueing model.
GATEWAY_OVERHEAD_LOW_S = 0.39 - 0.30
GATEWAY_OVERHEAD_HIGH_S = 0.41 - 0.30

# Bedrock Guardrails sensitive-information filter: $0.10 / 1,000 text units (aws.amazon.com/
# bedrock/pricing, fetched 2026-09-27). A text unit = up to 1,000 characters, rounded up.
BEDROCK_USD_PER_1000_UNITS = 0.10
CHARS_PER_KB = 1024  # ESTIMATED: bytes-to-chars for mixed EN/FR text, not measured


def combine_seed(*parts):
    """Deterministic seed derivation (Python's str hash() is salted per-process by default;
    this must be reproducible run to run, so it uses crc32 over a fixed repr instead)."""
    return zlib.crc32(repr(parts).encode())


def per_kb_time(gpu_key, precision, kb):
    """Seconds-per-KB of GPU inference time at this request size, scaled from the measured RTX
    A4500 fp32 baseline by the spec-sheet FP32 ratio, then divided by the measured, size-dependent
    fp16 speedup (fp16_speedup_at) for the 16-bit variant."""
    fp32 = GPUS[gpu_key]["fp32_tflops"]
    t = BASELINE_S_PER_KB_FP32 * (BASELINE_GPU_FP32_TFLOPS / fp32)
    return t / fp16_speedup_at(kb) if precision == "fp16" else t


# ---------------------------------------------------------------------------
# Discrete-event G/G/c queue: c single-threaded servers, one shared FIFO queue.
# ---------------------------------------------------------------------------

def simulate_queue(rng, arrival_gap, service_time, servers, duration, warmup):
    """Run one G/G/c simulation. arrival_gap()/service_time() draw one sample each call.
    Returns (wait, service) pairs for arrivals in [warmup, warmup+duration)."""
    free = [0.0] * servers
    heapq.heapify(free)
    t = 0.0
    end = warmup + duration
    samples = []
    while True:
        t += arrival_gap()
        if t >= end:
            break
        start = max(t, heapq.heappop(free))
        wait = start - t
        service = service_time()
        heapq.heappush(free, start + service)
        if t >= warmup:
            samples.append((wait, service))
    return samples


def percentile(sorted_data, p):
    if not sorted_data:
        return float("nan")
    k = (len(sorted_data) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_data[int(k)]
    return sorted_data[lo] * (hi - k) + sorted_data[hi] * (k - lo)


def draw_size_kb(rng, mean_kb, sigma):
    mu = math.log(mean_kb) - sigma * sigma / 2.0
    return rng.lognormvariate(mu, sigma)


def run_scenario(gpu_key, precision, rate, servers, sim_seconds, warmup, seed,
                  mean_kb=MEAN_REQUEST_KB, sigma=SIGMA_KB, network_rtt=NETWORK_RTT_S,
                  min_samples=3000):
    """Simulate `servers` replicas of `gpu_key`/`precision` under Poisson arrivals at `rate`
    req/s. Returns dict with mean/p95 total latency (network + queue wait + inference).

    Duration is stretched (never shortened) so low-rate hours still collect ~min_samples
    arrivals -- a p95 read off a few hundred samples is noisy, and the report makes replica
    decisions on exactly that number."""
    duration = max(sim_seconds, min_samples / rate)
    rng = random.Random(seed)

    def service_time():
        kb = draw_size_kb(rng, mean_kb, sigma)
        return kb * per_kb_time(gpu_key, precision, kb)

    samples = simulate_queue(
        rng,
        arrival_gap=lambda: rng.expovariate(rate),
        service_time=service_time,
        servers=servers, duration=duration, warmup=warmup,
    )
    totals = sorted(network_rtt + w + s for w, s in samples)
    return {
        "servers": servers, "n": len(totals),
        "mean_s": sum(totals) / len(totals) if totals else float("inf"),
        "p95_s": percentile(totals, 0.95) if totals else float("inf"),
    }


def min_replicas(gpu_key, precision, rate, floor, sim_seconds, warmup, seed,
                  target_avg=TARGET_AVG_S, target_p95=TARGET_P95_S, cap=200,
                  mean_kb=MEAN_REQUEST_KB, network_rtt=NETWORK_RTT_S, **kw):
    """Smallest replica count >= floor meeting both latency targets. Seeds each candidate
    deterministically (base seed, gpu, precision, rate, c) so results are reproducible.

    More replicas can only push queueing wait toward 0; they can't shrink the inference time
    itself. So if network + mean inference time alone already exceeds target_avg, the target is
    structurally unreachable on this GPU/precision -- skip the search instead of simulating up
    to `cap` replicas for nothing (this is exactly the AWS T4 case: its GLiNER2 inference is
    too slow at any replica count)."""
    if network_rtt + mean_kb * per_kb_time(gpu_key, precision, mean_kb) > target_avg:
        return None, None
    c = floor
    while c <= cap:
        s = seed ^ combine_seed(gpu_key, precision, round(rate, 4), c)
        stats = run_scenario(gpu_key, precision, rate, c, sim_seconds, warmup, s,
                              mean_kb=mean_kb, network_rtt=network_rtt, **kw)
        if stats["mean_s"] <= target_avg and stats["p95_s"] <= target_p95:
            return c, stats
        c += 1
    return None, stats


# ---------------------------------------------------------------------------
# Self-check: an M/M/1 case against its closed form, and a trivial deterministic case.
# ---------------------------------------------------------------------------

def selfcheck(seed=42, verbose=True):
    ok = True

    # M/M/1: lambda=0.7, mu=1.0 -> rho=0.7. Closed form: W = 1/(mu-lambda), Wq = rho/(mu-lambda).
    rng = random.Random(seed)
    lam, mu = 0.7, 1.0
    samples = simulate_queue(
        rng, arrival_gap=lambda: rng.expovariate(lam), service_time=lambda: rng.expovariate(mu),
        servers=1, duration=200_000, warmup=2_000,
    )
    w_sim = sum(w + s for w, s in samples) / len(samples)
    wq_sim = sum(w for w, s in samples) / len(samples)
    w_theory, wq_theory = 1.0 / (mu - lam), (lam / mu) / (mu - lam)
    w_err, wq_err = abs(w_sim - w_theory) / w_theory, abs(wq_sim - wq_theory) / wq_theory
    passed = w_err < 0.03 and wq_err < 0.05
    ok &= passed
    if verbose:
        print(f"  M/M/1 (lambda={lam}, mu={mu}): sim W={w_sim:.4f} theory={w_theory:.4f} "
              f"(err {w_err:.1%}); sim Wq={wq_sim:.4f} theory={wq_theory:.4f} (err {wq_err:.1%}) "
              f"-> {'PASS' if passed else 'FAIL'}", file=sys.stderr)

    # Trivial deterministic case: arrivals every 1.0s, service exactly 0.4s, 1 server.
    # Interarrival > service, so nothing should ever queue: wait == 0, sojourn == service.
    rng2 = random.Random(seed)
    det = simulate_queue(rng2, arrival_gap=lambda: 1.0, service_time=lambda: 0.4,
                          servers=1, duration=100.0, warmup=0.0)
    passed_det = len(det) > 0 and all(abs(w) < 1e-9 and abs(s - 0.4) < 1e-9 for w, s in det)
    ok &= passed_det
    if verbose:
        print(f"  Deterministic D/D/1 (arrivals every 1.0s, service 0.4s): "
              f"{'PASS' if passed_det else 'FAIL'} ({len(det)} samples, "
              f"max wait {max((w for w, _ in det), default=0):.6f}s)", file=sys.stderr)

    # fp16_speedup_at: exact at the 3 measured points, clamped flat outside [2, 40] KB.
    exact = all(fp16_speedup_at(kb) == ratio for kb, ratio in FP16_SPEEDUP_MEASURED_KB.items())
    clamped = fp16_speedup_at(0.5) == 5.00 and fp16_speedup_at(100) == 1.11
    midpoint = abs(fp16_speedup_at(6.0) - (5.00 + 2.54) / 2) < 1e-9  # linear midpoint of 2-10 KB
    passed_curve = exact and clamped and midpoint
    ok &= passed_curve
    if verbose:
        print(f"  fp16_speedup_at (measured curve): exact-points={exact} clamped={clamped} "
              f"midpoint={midpoint} -> {'PASS' if passed_curve else 'FAIL'}", file=sys.stderr)

    return ok


# ---------------------------------------------------------------------------
# Full report: replicas/latency/cost per GPU option, for the weekday+weekend daily profile.
# ---------------------------------------------------------------------------

def daily_shapes():
    """Unique (multiplier, floor) load shapes across a weekday+weekend, so each distinct load
    level is only simulated once (many hours share the same shape)."""
    shapes = {(WEEKDAY_HOUR_MULTIPLIER.get(h, OFF_HOURS_MULTIPLIER),
               2 if h in WEEKDAY_HOUR_MULTIPLIER else 1) for h in range(24)}
    shapes.add((OFF_HOURS_MULTIPLIER, 1))
    return shapes


def structural_floor_s(gpu_key, precision, mean_kb=MEAN_REQUEST_KB, network_rtt=NETWORK_RTT_S):
    """Best-case average latency (network + mean inference time, zero queueing) -- the floor no
    replica count can beat. If this alone exceeds the target, the option is infeasible."""
    return network_rtt + mean_kb * per_kb_time(gpu_key, precision, mean_kb)


def build_report(sim_seconds, warmup, seed, target_avg=TARGET_AVG_S,
                  mean_kb=MEAN_REQUEST_KB, network_rtt=NETWORK_RTT_S, **kw):
    shapes = daily_shapes()
    report = {}  # gpu_key -> precision -> {infeasible: bool, floor_s, by_hour, monthly_hours, cost_local, cost_eur}
    for gpu_key in GPUS:
        report[gpu_key] = {}
        for precision in ("fp32", "fp16"):
            floor_s = structural_floor_s(gpu_key, precision, mean_kb, network_rtt)
            if floor_s > target_avg:
                report[gpu_key][precision] = dict(infeasible=True, floor_s=floor_s)
                continue
            by_shape = {}
            for (mult, floor) in shapes:
                rate = mult * PEAK_RATE_RPS
                c, stats = min_replicas(gpu_key, precision, rate, floor, sim_seconds, warmup, seed,
                                         target_avg=target_avg, mean_kb=mean_kb, network_rtt=network_rtt, **kw)
                by_shape[(mult, floor)] = (c, stats)
            by_hour = {}
            weekday_replica_sum = 0
            weekend_replica_sum = 0
            for h in range(24):
                mult = WEEKDAY_HOUR_MULTIPLIER.get(h, OFF_HOURS_MULTIPLIER)
                floor = 2 if h in WEEKDAY_HOUR_MULTIPLIER else 1
                c, stats = by_shape[(mult, floor)]
                by_hour[(True, h)] = (c, stats)
                weekday_replica_sum += c or 0
                c2, stats2 = by_shape[(OFF_HOURS_MULTIPLIER, 1)]
                by_hour[(False, h)] = (c2, stats2)
                weekend_replica_sum += c2 or 0
            if weekday_replica_sum == 0 and weekend_replica_sum == 0:
                # min_replicas returned None (target unreachable within `cap` replicas) for every
                # single hour -- distinct from the mean-only floor_s check above, which passed (it
                # ignores p95 and queueing). Without this, monthly_hours would be 0 and the report
                # would print a misleading EKS-only "cost" for an option that serves no traffic.
                report[gpu_key][precision] = dict(infeasible=True, floor_s=floor_s,
                                                   reason="p95 target unreachable at every hour, even at the replica cap")
                continue
            monthly_hours = weekday_replica_sum * WEEKDAY_DAYS_PER_MONTH + weekend_replica_sum * WEEKEND_DAYS_PER_MONTH
            gpu = GPUS[gpu_key]
            cost_local = monthly_hours * gpu["price"]
            if gpu["currency"] == "USD":
                cost_local += EKS_HOURLY_USD * 24 * 30  # one cluster, flat, regardless of node count
            cost_eur = cost_local if gpu["currency"] == "EUR" else cost_local / FX_USD_PER_EUR
            report[gpu_key][precision] = dict(infeasible=False, floor_s=floor_s, by_hour=by_hour,
                                               monthly_hours=monthly_hours, cost_local=cost_local, cost_eur=cost_eur)
    return report


def bedrock_reference_cost_eur():
    """Monthly cost reference for AWS Bedrock Guardrails' sensitive-information filter at the
    same traffic volume (not sized to the latency target -- Bedrock's latency isn't modeled)."""
    monthly_requests = 0.0
    for h in range(24):
        mult = WEEKDAY_HOUR_MULTIPLIER.get(h, OFF_HOURS_MULTIPLIER)
        monthly_requests += mult * PEAK_RATE_RPS * 3600 * WEEKDAY_DAYS_PER_MONTH
        monthly_requests += OFF_HOURS_MULTIPLIER * PEAK_RATE_RPS * 3600 * WEEKEND_DAYS_PER_MONTH
    text_units_per_request = math.ceil(MEAN_REQUEST_KB * CHARS_PER_KB / 1000)
    monthly_usd = monthly_requests * text_units_per_request / 1000 * BEDROCK_USD_PER_1000_UNITS
    return monthly_requests, text_units_per_request, monthly_usd / FX_USD_PER_EUR


def render_report(report):
    lines = [
        "# Capacity simulation results",
        "",
        f"Target: avg <= {TARGET_AVG_S*1000:.0f} ms, p95 <= {TARGET_P95_S*1000:.0f} ms per analyzer call. "
        f"Peak {PEAK_RATE_RPS:.0f} req/s, mean {MEAN_REQUEST_KB:.0f} KB/request (lognormal, sigma={SIGMA_KB}).",
        "",
        f"fp16 speedup: measured on an RTX 4090 (not the exact target GPU), size-dependent -- "
        f"{', '.join(f'{kb:.0f} KB={r:.2f}x' for kb, r in sorted(FP16_SPEEDUP_MEASURED_KB.items()))}, "
        f"interpolated between points and clamped flat outside them. "
        f"{'Overridden flat at ' + format(FP16_SPEEDUP_OVERRIDE, '.2f') + 'x for this run.' if FP16_SPEEDUP_OVERRIDE is not None else ''}",
        "",
    ]
    for gpu_key, gpu in GPUS.items():
        for precision in ("fp32", "fp16"):
            r = report[gpu_key][precision]
            lines += [f"## {gpu['provider']} {gpu['label']} -- {precision} "
                      f"({gpu['price']} {gpu['currency']}/h{', ' + gpu['note'] if gpu['note'] else ''})", ""]
            if not gpu["multi_zone"]:
                lines += ["**PAR-2 only (scaleway.com/en/pricing/gpu, fetched 2026-09-27): if "
                          "that means a single availability zone, this SKU alone cannot satisfy "
                          "the decided floor of >= 2 replicas in different zones during working "
                          "hours. Listed for latency/cost comparison only.**", ""]
            if r["infeasible"]:
                if r.get("reason"):
                    lines += [f"**Infeasible: {r['reason']}.** Network + mean inference time alone "
                              f"= {r['floor_s']*1000:.0f} ms (under the {TARGET_AVG_S*1000:.0f} ms "
                              f"average target), but the p95 tail (large requests) can't be brought "
                              f"under {TARGET_P95_S*1000:.0f} ms even at the replica search cap.", ""]
                else:
                    lines += [f"**Structurally infeasible at any replica count.** Network + mean "
                              f"inference time alone = {r['floor_s']*1000:.0f} ms, already above the "
                              f"{TARGET_AVG_S*1000:.0f} ms average target; adding replicas only "
                              f"reduces queueing wait, not inference time.", ""]
                continue
            lines += ["| Hour | Weekday replicas | Weekday avg/p95 | Weekend replicas | Weekend avg/p95 |",
                      "|---|---|---|---|---|"]
            for h in range(24):
                c_wd, s_wd = r["by_hour"][(True, h)]
                c_we, s_we = r["by_hour"][(False, h)]
                fmt = lambda c, s: (f"{c}" if c else "N/A (target unreachable)",
                                     f"{s['mean_s']*1000:.0f}/{s['p95_s']*1000:.0f} ms" if c else "-")
                cw, lw = fmt(c_wd, s_wd)
                ce, le = fmt(c_we, s_we)
                lines.append(f"| {h:02d}:00 | {cw} | {lw} | {ce} | {le} |")
            eur_note = "" if gpu["currency"] == "EUR" else f" (~{r['cost_eur']:.0f} EUR at {FX_USD_PER_EUR} USD/EUR)"
            lines += ["", f"Monthly GPU-hours: {r['monthly_hours']:.0f}. "
                          f"Monthly cost: {r['cost_local']:.0f} {gpu['currency']}{eur_note}.",
                      ""]
    requests, units, cost_eur = bedrock_reference_cost_eur()
    lines += [
        "## Bedrock Guardrails reference (cost only, not latency-sized)",
        "",
        f"~{requests:,.0f} requests/month x {units} text unit(s)/request x "
        f"${BEDROCK_USD_PER_1000_UNITS}/1,000 units = ~{cost_eur:.0f} EUR/month "
        f"(at {FX_USD_PER_EUR} USD/EUR). Text units computed from the mean request size "
        f"({MEAN_REQUEST_KB:.0f} KB), not the full size distribution -- an approximation.",
        "",
        "## Gateway overhead per request (not part of the queueing model above)",
        "",
        f"Measured, tiny text (docs/claude-code-guardrail.md): direct 0.30 s vs through-gateway "
        f"0.39-0.41 s => ~{GATEWAY_OVERHEAD_LOW_S*1000:.0f}-{GATEWAY_OVERHEAD_HIGH_S*1000:.0f} ms "
        f"for the LiteLLM hop + regex scan on ONE analyzer call. A gateway turn with N new text "
        f"blocks pays this roughly ceil(N/4) times (4 analyzer calls in flight at once, per "
        f"claude-code-guardrail.md). This linear-in-size model has no fixed per-call overhead "
        f"term of its own (it is fit from 12 KB ~= 0.9 s, i.e. 0.075 s/KB exactly), so it likely "
        f"understates latency for very small requests.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global FP16_SPEEDUP_OVERRIDE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true", help="only run the self-check, then exit")
    ap.add_argument("--gpu", choices=sorted(GPUS), help="ad-hoc mode: one GPU option")
    ap.add_argument("--precision", choices=("fp32", "fp16"), default="fp32")
    ap.add_argument("--rate", type=float, help="ad-hoc mode: arrival rate, req/s")
    ap.add_argument("--replicas", type=int, help="ad-hoc mode: force this replica count instead of searching")
    ap.add_argument("--sim-seconds", type=float, default=900.0)
    ap.add_argument("--warmup-seconds", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mean-kb", type=float, default=MEAN_REQUEST_KB)
    ap.add_argument("--sigma", type=float, default=SIGMA_KB)
    ap.add_argument("--min-samples", type=int, default=3000,
                    help="stretch simulated duration so low-rate hours still collect this many samples")
    ap.add_argument("--fp16-speedup", type=float, default=None,
                    help="flat 16-bit speedup multiplier, overriding the measured size-dependent "
                         "curve (FP16_SPEEDUP_MEASURED_KB) everywhere; for a sensitivity check")
    ap.add_argument("--out", help="write the report to this file instead of stdout")
    args = ap.parse_args()
    FP16_SPEEDUP_OVERRIDE = args.fp16_speedup

    ok = selfcheck(seed=args.seed)
    if not ok:
        print("SELF-CHECK FAILED", file=sys.stderr)
        sys.exit(1)
    print("self-check: PASS", file=sys.stderr)
    if args.selfcheck:
        return

    if args.gpu and args.rate is not None:
        if args.replicas:
            stats = run_scenario(args.gpu, args.precision, args.rate, args.replicas,
                                  args.sim_seconds, args.warmup_seconds, args.seed,
                                  mean_kb=args.mean_kb, sigma=args.sigma, min_samples=args.min_samples)
            c = args.replicas
        else:
            floor = 1
            c, stats = min_replicas(args.gpu, args.precision, args.rate, floor,
                                     args.sim_seconds, args.warmup_seconds, args.seed,
                                     mean_kb=args.mean_kb, sigma=args.sigma, min_samples=args.min_samples)
        if c is None:
            print(f"{args.gpu} {args.precision} @ {args.rate} req/s: structurally infeasible "
                  f"(floor {structural_floor_s(args.gpu, args.precision, args.mean_kb)*1000:.0f} ms "
                  f"> {TARGET_AVG_S*1000:.0f} ms target)")
            return
        print(f"{args.gpu} {args.precision} @ {args.rate} req/s: replicas={c} "
              f"avg={stats['mean_s']*1000:.0f}ms p95={stats['p95_s']*1000:.0f}ms "
              f"(n={stats['n']} samples)")
        return

    report = build_report(args.sim_seconds, args.warmup_seconds, args.seed,
                           mean_kb=args.mean_kb, sigma=args.sigma, min_samples=args.min_samples)
    text = render_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"report: {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
