#!/usr/bin/env python3
"""acceptance_test.py: a candidate binary vs the head binary on the GEOMEAN of paired device readings. Python 3.10, stdlib only.

  python3 acceptance_test/acceptance_test.py run --head <lane> --cand <lane> --pairs N [--level info] [--out DIR] [--z 1.64] [--min-pairs 5] [--dry]
                                      [--no-early-stop] [--early-at 5] [--early-pass-z 4.0] [--early-fail-z -2.0]
  python3 acceptance_test/acceptance_test.py judge <out DIR> [--z 1.64] [--min-pairs 5] [--json]
  python3 acceptance_test/acceptance_test.py replay [--root scratch081/logs/paired] [--early-at 5] [--early-pass-z 4.0] [--early-fail-z -2.0] [--json]
  python3 acceptance_test/acceptance_test.py --selftest | --make-fixture DIR    # offline; the fixture = the F3 composite's 11 measured pairs as probe logs

Why: an ffn code change moved attn_out with the attn_out schedule byte-identical, so a candidate is judged on the score
(the geomean over the three kernels of baseline / cycles) against head readings taken beside it, never kernel by kernel.
  run    device host only (MICRO_EC2_LOCAL=1; --dry prints the commands anywhere and exits 0). Pair i runs `bash tools/081/probe081.sh <lane> <level>` from the repo
         root for both lanes, the second 1 s after the first, waits for both, then pair i + 1. Odd pairs launch the head first, even pairs the candidate (a
         slot or order bias cancels). stdout + stderr -> <out>/probe-<head|cand>-<i>.log; <out>/order.txt gets `<i> <first> <second> <utc>` before each
         launch: judge reads the launch order from it, never from the job ids (a job launched first can still get the higher id). A probe exit 64-68
         (probe081.sh submitted no job) stops the run after its pair, and a first probe that exits so within the 1 s keeps the
         second from launching. Then judge -> <out>/verdict.json (+ run: probe rcs, stop reason); the exit code is judge's. Default out
         scratch081/logs/paired/<cand>-vs-<head>-<utc compact>; an out dir that already holds probe logs is refused.
         EARLY STOP (approved by the human operator): with --pairs > --early-at (5) >= --min-pairs, judge reads pairs 1-5 once pair 5 is
         done; no failed probe, all 5 counted and z > --early-pass-z (4.0) with a PASS verdict -> stop, PASS; z < --early-fail-z (-2.0) -> stop, FAIL
         ("early stop: significantly worse, z <z> at 5 pairs"); anything else runs all N pairs ("not yet significant" never stops a run; a failed
         probe runs on, as before). --no-early-stop runs all N. verdict.json early_stop = {at, n, z, gain_pct, decision PASS | FAIL | continue,
         pass_z, fail_z, why} or null (not applicable: --no-early-stop, --pairs <= --early-at, --early-at < --min-pairs, a stop before pair 5).
  replay read-only, over the recorded run dirs under --root (default the out root; <cand>-vs-<head>-<utc>/): each dir with more than --early-at counted
         pairs gets its full verdict (judge, z bar 1.64, min pairs 5) and the early-stop decision on its pairs 1-5. A change = an early PASS on a
         non-PASS full verdict or an early FAIL on a full PASS (an early FAIL on FLAT is none: neither files). Prints the stops and changes, the
         totals (dirs, stops, continues, changes, pairs and probe jobs saved), the smallest PASS bar (0.25 steps up) and FAIL bar (0.25 steps down)
         with 0 changes, the recorded verdict.json that disagree with judge, and the FLAT runs with a composition candidate (newest first).
         Odd dirs are skipped with a counted reason (no probe logs, <= 5 counted pairs, no verdict.json and touched < 2 h ago = in progress);
         nothing under --root is written. Exit 0 no change at the given bars, 1 changes, 2 usage.
  judge  per probe log: ok = the last `collect rc=R: arena job N SUCCEEDED cycles={...}` line carries the three kernels and the log `all 3 tests passed`;
         fail = any failed test (`-> FAIL`, `FAIL --`, `tests failed`, `panicked`, a False in pass=); missing = no log or no result line; incomplete = a
         result line that is neither (not SUCCEEDED, a kernel short, no `all 3 tests passed`). A pair counts when both probes are ok; one failed probe
         anywhere makes the verdict FAIL (a correctness failure is never averaged away); a missing or incomplete probe drops its pair (reported).
         Per pair: gap_k = cand_k - head_k, g = (1/3) sum_k ln(head_k / cand_k) (> 0: the candidate scores higher). Report: per kernel the head / cand mean,
         mean gap, se = sd / sqrt(n) (sd over n - 1), moved = |mean gap| >= 2 se (a cross-kernel move shows even on PASS); the score of each side from the
         mean cycles; mean g, se(g), gain_pct = 100 (exp(mean g) - 1), z = mean g / se(g); the split by launch order for g and each kernel.
         Verdict: FAIL > INSUFFICIENT (n < --min-pairs) > PASS (z >= --z; 1.64 = one-sided 5 %) > FLAT; run's early stop (--no-early-stop, --early-at,
         --early-pass-z, --early-fail-z) may end at pair 5 with PASS (z > 4) or FAIL (z < -2; verdict_reason says so, failed stays empty). A FLAT
         verdict with a kernel whose mean gap is negative (the candidate faster) by >= 3 se is a COMPOSITION CANDIDATE: compose_candidate =
         [{kernel, gap, se_units}] and a `COMPOSITION CANDIDATE:` line (main lists it in workers/shared/COMPOSE.md; this tool never writes it).
         Exit 0 PASS, 1 FLAT / INSUFFICIENT, 3 FAIL, 2 usage.
probe081.sh cuts the collect line at 200 characters, inside pass= (the ffn entry is lost), so judge reads pass= only for a False.
Entry points: read_probe(path) -> probe; read_order(out) -> {pair: first}; judge(out, z_bar, min_pairs, max_pairs) -> report; compose_of(kernels,
  verdict); early_decision(report, pass_z, fail_z, at) -> PASS | FAIL | None; early_record(...) -> early_stop; render(report) -> lines; plan(head,
  cand, pairs, level, out); run_pairs(plan, out, cwd, delay, after); execute(...) -> rc; replay(root, at, pass_z, fail_z) -> dict; render_replay;
  selftest fixtures (never real logs): fake_probe(...), make_f3_fixture(dir), make_pairs_fixture(dir, heads, cands)."""
import argparse
import contextlib
import io
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__)); TOOLS = os.path.dirname(HERE); ROOT = os.path.dirname(TOOLS)
if HERE not in sys.path: sys.path.insert(0, HERE)
import atomic  # noqa: E402
import constants as V7  # noqa: E402

KERNELS = V7.KERNELS; LONG = V7.LONG; SHORT = V7.SHORT; BASE = V7.BASELINE_CYCLES
SIDES = ("head", "cand")
PROBE = "tools/081/probe081.sh"  # relative to the repo root, as mi_runner runs it
OUT_ROOT = "scratch081/logs/paired"
ORDER = "order.txt"; VERDICT = "verdict.json"
LEVELS = ("info", "debug", "trace")  # ec2_run.LEVELS
Z = 1.64; MIN_PAIRS = 5; DELAY_S = 1.0
EARLY_AT = 5; EARLY_PASS_Z = 4.0; EARLY_FAIL_Z = -2.0  # the early stop: after 5 pairs z > 4 PASS, z < -2 FAIL, else all pairs (bars: replay)
COMPOSE_SE = 3.0; COMPOSE_MD = "workers/shared/COMPOSE.md"  # a FLAT verdict with a kernel >= 3 se faster; main writes COMPOSE.md, never this tool
YOUNG_S = 7200  # replay: a dir without verdict.json touched within 2 h is taken as a run in progress
UTC_RE = re.compile(r"(\d{8}T\d{6}Z)$")
NO_JOB = (64, 65, 66, 67, 68)  # probe081.sh: quota gave up, submit failed, no binary, upstream-files check refused, arena gate refused
RC = {"PASS": 0, "FLAT": 1, "INSUFFICIENT": 1, "FAIL": 3}
LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")  # file-safe, as multi_input.tree_id
LOG_RE = re.compile(r"probe-(head|cand)-(\d+)\.log")
COLLECT = re.compile(r"^collect rc=(-?\d+): arena job (\d+) (\w+)\b.*?cycles=\{([^}\n]*)\}(?:.*?pass=\{([^}\n]*))?", re.M)
CYC_KV = re.compile(r"'(\w+)':\s*(\d+)"); PASS_KV = re.compile(r"'(\w+)':\s*(True|False)")
ALL_PASSED = re.compile(r"^all 3 tests passed\b", re.M)
FAIL_LINE = re.compile(r"^.*(?:-> FAIL\b|\bFAIL --|\btests failed\b|\bpanicked\b).*$", re.M)
TESTS_FAILED = re.compile(r"^\d+ of \d+ tests failed.*$", re.M)
DONE = re.compile(r"^== probe081 (\S+) job \d+", re.M); SUBMITTED = re.compile(r"SUBMITTED job \d+ \(toolchain-\d+-([^,\s]+), ")


class UsageError(Exception): pass


def utc(t=None): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))
def utc_compact(t=None): return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if t is None else t))
def log_path(out, side, i): return os.path.join(out, f"probe-{side}-{i}.log")
def score(cyc): return math.exp(sum(math.log(BASE[k] / cyc[k]) for k in KERNELS) / len(KERNELS))
def g_of(head, cand): return sum(math.log(head[k] / cand[k]) for k in KERNELS) / len(KERNELS)
def gain_pct(g): return None if g is None else 100.0 * math.expm1(g)


def mean_se(xs):
    """(mean, se = sd / sqrt(n), sd over n - 1); (None, None) for no value, se None for one."""
    n = len(xs)
    if not n: return None, None
    m = sum(xs) / n
    return m, (math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n) if n > 1 else None)


# ---------------------------------------------------------------- judge

def read_probe(path):
    """One probe081.sh log -> {status ok | fail | missing | incomplete, why, job, arena, rc, cycles {k: int}, lane, log} (module docstring)."""
    p = {"status": "missing", "why": "no log", "job": None, "arena": None, "rc": None, "cycles": {}, "lane": None, "log": path}
    if not os.path.isfile(path): return p
    text = open(path, encoding="utf-8", errors="replace").read()
    lanes = DONE.findall(text) or SUBMITTED.findall(text); p["lane"] = lanes[-1] if lanes else None
    hits = COLLECT.findall(text); pas = ""
    if hits:
        rc, job, arena, cyc, pas = hits[-1]
        p.update(rc=int(rc), job=int(job), arena=arena, cycles={SHORT[k]: int(v) for k, v in CYC_KV.findall(cyc) if k in SHORT})
    bad = FAIL_LINE.findall(text); neg = [SHORT.get(k, k) for k, v in PASS_KV.findall(pas) if v == "False"]
    if bad or neg:
        first = (TESTS_FAILED.findall(text) or [l.strip() for l in bad] or [""])[0][:120]; neg_s = ", ".join(neg)
        p.update(status="fail", why="; ".join(x for x in (first, f"{len(bad)} failure lines" if bad else "", f"pass= False: {neg_s}" if neg else "") if x)); return p
    if not hits: p["why"] = "no result line"; return p
    lack = ", ".join(k for k in KERNELS if k not in p["cycles"])
    why = ([f"arena {arena}"] if arena != "SUCCEEDED" else []) + ([f"no cycles for {lack}"] if lack else []) + ([] if ALL_PASSED.search(text) else ["no `all 3 tests passed`"])
    p.update(status="incomplete" if why else "ok", why="; ".join(why)); return p


def read_order(out):
    """order.txt -> {pair: the side launched first}; `#` lines and anything after the first two fields are ignored."""
    try: lines = open(os.path.join(out, ORDER), encoding="utf-8").read().splitlines()
    except OSError: return {}
    return {int(m.group(1)): m.group(2) for m in (re.match(r"\s*(\d+)\s+(head|cand)\b", l) for l in lines) if m}


def _warnings(pairs, lanes):
    w = [f"the {s} logs name {len(lanes[s])} lanes ({', '.join(lanes[s])}): a mixed dir" for s in SIDES if len(lanes[s]) > 1]
    jobs = [r[s]["job"] for r in pairs for s in SIDES if r[s]["job"] is not None]; dup = sorted({j for j in jobs if jobs.count(j) > 1})
    if dup: w.append(f"job ids read by more than one probe log: {dup} (a copied log?)")
    unk = [r["pair"] for r in pairs if r["counted"] and r["order"] is None]
    if unk: w.append(f"no order.txt line for counted pairs {unk}: they count in neither launch-order split")
    return w


def compose_of(kern, verdict, se_bar=COMPOSE_SE):
    """A FLAT verdict's composition candidates: [{kernel, gap, se_units}] for each kernel whose mean gap is negative (the
    candidate faster) by >= se_bar se; [] for any other verdict."""
    if verdict != "FLAT": return []
    return [{"kernel": k, "gap": s["gap_mean"], "se_units": abs(_ratio(s["gap_mean"], s["gap_se"]))} for k, s in kern.items()
            if s["gap_mean"] is not None and s["gap_se"] is not None and s["gap_mean"] < 0 and -s["gap_mean"] >= se_bar * s["gap_se"]]


def judge(out, z_bar=Z, min_pairs=MIN_PAIRS, max_pairs=None):
    """The paired verdict of one out dir -> the report dict (module docstring); max_pairs = judge pairs 1..max_pairs only (run's early stop,
    replay); UsageError for no dir or no probe log."""
    if not os.path.isdir(out): raise UsageError(f"no dir {out}")
    order = read_order(out); files = {}
    for f in os.listdir(out):
        m = LOG_RE.fullmatch(f)
        if m: files[(m.group(1), int(m.group(2)))] = os.path.join(out, f)
    idx = sorted(i for i in {i for _, i in files} | set(order) if max_pairs is None or i <= max_pairs)
    if not idx: raise UsageError(f"no probe-head-<i>.log / probe-cand-<i>.log (nor order.txt) under {out}" + (f" for pairs <= {max_pairs}" if max_pairs is not None else ""))
    pairs = []
    for i in idx:
        r = {"pair": i, "order": order.get(i)}; r.update({s: read_probe(files.get((s, i), log_path(out, s, i))) for s in SIDES})
        r["counted"] = r["head"]["status"] == r["cand"]["status"] == "ok"
        if r["counted"]:
            h, c = r["head"]["cycles"], r["cand"]["cycles"]
            r.update(gap={k: c[k] - h[k] for k in KERNELS}, g=g_of(h, c), score={"head": score(h), "cand": score(c)})
        else: r["dropped"] = "; ".join(f"{s} {r[s]['status']} ({r[s]['why']})" for s in SIDES if r[s]["status"] != "ok")
        pairs.append(r)
    ok = [r for r in pairs if r["counted"]]; n = len(ok)
    def split(f): return {f"{o}_first": dict(zip(("n", "mean", "se"), (len(v),) + mean_se(v))) for o in SIDES for v in [[f(r) for r in ok if r["order"] == o]]}
    kern = {}
    for k in KERNELS:
        gm, gse = mean_se([r["gap"][k] for r in ok])
        kern[k] = {"head_mean": mean_se([r["head"]["cycles"][k] for r in ok])[0], "cand_mean": mean_se([r["cand"]["cycles"][k] for r in ok])[0], "gap_mean": gm,
                   "gap_se": gse, "moved": bool(gm and gse is not None and abs(gm) >= 2 * gse), "split": split(lambda r: r["gap"][k])}
    mg, sg = mean_se([r["g"] for r in ok])
    z = None if sg is None else mg / sg if sg > 0 else math.copysign(math.inf, mg) if mg else 0.0  # se 0: synthetic readings only
    sh, sc = (score({k: kern[k][f"{s}_mean"] for k in KERNELS}) if n else None for s in SIDES)
    failed = [{"pair": r["pair"], "side": s, "job": r[s]["job"], "why": r[s]["why"]} for r in pairs for s in SIDES if r[s]["status"] == "fail"]
    verdict = "FAIL" if failed else "INSUFFICIENT" if n < min_pairs else "PASS" if z is not None and z >= z_bar else "FLAT"
    lanes = {s: sorted({r[s]["lane"] for r in pairs if r[s]["lane"]}) for s in SIDES}
    return {"dir": os.path.abspath(out), "lanes": lanes, "z_bar": z_bar, "min_pairs": min_pairs, "max_pairs": max_pairs, "n": n, "pairs": pairs,
            "dropped": [{"pair": r["pair"], "why": r["dropped"]} for r in pairs if not r["counted"]], "failed": failed, "kernels": kern,
            "score": {"head": sh, "cand": sc, "gain_pct": 100.0 * (sc / sh - 1) if n else None},
            "g": {"mean": mg, "se": sg, "gain_pct": gain_pct(mg), "z": z, "split": split(lambda r: r["g"])},
            "verdict": verdict, "rc": RC[verdict], "verdict_reason": None, "compose_candidate": compose_of(kern, verdict), "warnings": _warnings(pairs, lanes)}


def early_decision(rep, pass_z=EARLY_PASS_Z, fail_z=EARLY_FAIL_Z, at=EARLY_AT):
    """The early-stop rule on the report of pairs 1..at -> "PASS" | "FAIL" | None (= run all pairs). Only a clean report decides: no failed probe
    (a correctness FAIL runs on, as before) and all `at` pairs counted. PASS = z > pass_z and that report's own verdict PASS (never under --z or
    --min-pairs); FAIL = z < fail_z (significantly worse). 'Not yet significant' is never a stop."""
    z = rep["g"]["z"]
    if rep["failed"] or rep["n"] != at or z is None: return None
    if z > pass_z and rep["verdict"] == "PASS": return "PASS"
    return "FAIL" if z < fail_z else None


def early_record(rep, pass_z=EARLY_PASS_Z, fail_z=EARLY_FAIL_Z, at=EARLY_AT):
    """verdict.json's early_stop for the report of pairs 1..at: {at, n, z, gain_pct, decision PASS | FAIL | continue, pass_z, fail_z, why}."""
    d = early_decision(rep, pass_z, fail_z, at); z = rep["g"]["z"]; zs = _f(z, ".2f")
    why = (f"early stop: significantly better, z {zs} > {pass_z} at {at} pairs" if d == "PASS" else f"early stop: significantly worse, z {zs} at {at} pairs" if d == "FAIL"
           else f"{len(rep['failed'])} failed probe(s): a correctness FAIL runs all pairs" if rep["failed"] else f"{rep['n']} of {at} pairs counted: all pairs run"
           if rep["n"] != at else f"z {zs} > {pass_z} but the {at}-pair verdict is {rep['verdict']}: all pairs run" if z is not None and z > pass_z
           else f"not significant (z {zs}, bars {fail_z} / {pass_z}): all pairs run")
    return {"at": at, "n": rep["n"], "z": z, "gain_pct": rep["g"]["gain_pct"], "decision": d or "continue", "pass_z": pass_z, "fail_z": fail_z, "why": why}


def _f(x, spec): return "n/a" if x is None else format(x, spec)
def _ratio(m, se): return None if m is None or se is None else (m / se if se else math.copysign(math.inf, m) if m else 0.0)
def _sp(d): return "n/a" if d["mean"] is None else f"{d['mean']:+,.0f} (n {d['n']})"


def _cell(p):
    c = p["cycles"]
    return f"{c['qkv']:>6,} / {c['attn_out']:>6,} / {c['ffn']:>7,} ({p['job']})" if all(k in c for k in KERNELS) else p["status"].upper()


def render(rep):
    """The human report: one row per pair, the kernel table, the score / g / moved lines, FAIL and WARNING lines, then the verdict line."""
    W = 34; lanes = {s: ", ".join(rep["lanes"][s]) or "?" for s in SIDES}; g = rep["g"]; sc = rep["score"]
    L = [f"paired gate {rep['dir']}: head {lanes['head']} vs cand {lanes['cand']}",
         f"{'pair':>4}  {'order':<10}  {'head q / a / f (job)':<{W}}  {'cand q / a / f (job)':<{W}}  {'d_q':>7} {'d_a':>7} {'d_f':>7}  score head / cand  gain %"]
    for r in rep["pairs"]:
        o = {"head": "head first", "cand": "cand first"}.get(r["order"], "?")
        if r["counted"]:
            d = r["gap"]; sc2 = f"{r['score']['head']:.4f} / {r['score']['cand']:.4f}"
            tail = f"{d['qkv']:>+7,} {d['attn_out']:>+7,} {d['ffn']:>+7,}  {sc2:<17}  {gain_pct(r['g']):+.3f}"
        else: tail = ("FAIL: " if "fail" in (r["head"]["status"], r["cand"]["status"]) else "dropped: ") + r["dropped"]
        L.append(f"{r['pair']:>4}  {o:<10}  {_cell(r['head']):<{W}}  {_cell(r['cand']):<{W}}  {tail}")
    cnt = {o: sum(1 for r in rep["pairs"] if r["counted"] and r["order"] == o) for o in ("head", "cand", None)}
    drops = ", ".join(str(d["pair"]) for d in rep["dropped"]) or "none"; unk = f", {cnt[None]} order ?" if cnt[None] else ""
    L += [f"counted {rep['n']} of {len(rep['pairs'])} pairs ({cnt['head']} head first, {cnt['cand']} cand first{unk}); not counted: {drops}; failed probes: {len(rep['failed'])}",
          f"{'kernel':<9} {'head mean':>10} {'cand mean':>10} {'mean gap':>9} {'se':>6} {'gap/se':>7}  {'moved':<5}  {'head first':>14}  {'cand first':>14}"]
    for k in KERNELS:
        s = rep["kernels"][k]; rt = _ratio(s["gap_mean"], s["gap_se"])
        L.append(f"{k:<9} {_f(s['head_mean'], '>10,.0f')} {_f(s['cand_mean'], '>10,.0f')} {_f(s['gap_mean'], '>+9,.0f')} {_f(s['gap_se'], '>6,.0f')} {_f(rt, '>+7.2f')}  "
                 f"{'MOVED' if s['moved'] else '':<5}  {_sp(s['split']['head_first']):>14}  {_sp(s['split']['cand_first']):>14}")
    if rep["n"]: L.append(f"score (geomean of baseline / mean cycles): head {sc['head']:.4f}, cand {sc['cand']:.4f} ({sc['gain_pct']:+.3f} %)")
    hf, cf = g["split"]["head_first"], g["split"]["cand_first"]
    L.append(f"g = (1/3) sum_k ln(head_k / cand_k) per pair: mean {_f(g['mean'], '+.6f')}, se {_f(g['se'], '.6f')}, gain {_f(g['gain_pct'], '+.3f')} %, z {_f(g['z'], '.2f')} "
             f"(bar {rep['z_bar']}); head first {_f(gain_pct(hf['mean']), '+.3f')} % (n {hf['n']}), cand first {_f(gain_pct(cf['mean']), '+.3f')} % (n {cf['n']})")
    mv = [f"{k} {s['gap_mean']:+,.0f} ({abs(_ratio(s['gap_mean'], s['gap_se'])):.2f} se, {'slower' if s['gap_mean'] > 0 else 'faster'})" for k, s in rep["kernels"].items() if s["moved"]]
    L.append("moved (|mean gap| >= 2 se): " + ("; ".join(mv) or "none"))
    L += [f"COMPOSITION CANDIDATE: {c['kernel']} {c['gap']:+,.0f} ({c['se_units']:.2f} se) -> {COMPOSE_MD}" for c in rep.get("compose_candidate") or []]
    es = rep.get("early_stop"); planned = (rep.get("run") or {}).get("pairs")
    if es:
        rest = "" if es["decision"] == "continue" or not planned or planned <= es["at"] else f"; pairs {es['at'] + 1}-{planned} not run"
        L.append(f"early stop {'check ' if es['decision'] == 'continue' else ''}at pair {es['at']}: {es['decision']} ({es['why']}){rest}")
    L += [f"FAIL: pair {f['pair']} {f['side']} (job {f['job']}): {f['why']}" for f in rep["failed"]] + [f"WARNING: {w}" for w in rep["warnings"]]
    vr = f"; {rep['verdict_reason']}" if rep.get("verdict_reason") else ""
    L.append(f"verdict: {rep['verdict']} (gain {_f(g['gain_pct'], '+.2f')} %, z {_f(g['z'], '.2f')}, n {rep['n']}{vr})")
    return L


# ---------------------------------------------------------------- run

def plan(head, cand, pairs, level, out):
    """[(i, [(side, argv, log)] in launch order)]: odd pairs launch the head first, even pairs the candidate first."""
    lane = {"head": head, "cand": cand}
    return [(i, [(s, ["bash", PROBE, lane[s], level], log_path(out, s, i)) for s in (SIDES if i % 2 else SIDES[::-1])]) for i in range(1, pairs + 1)]


def early_off(pairs, early=True, at=EARLY_AT, min_pairs=MIN_PAIRS):
    """Why run's early stop does not apply to this run (None = it applies after pair `at`)."""
    return ("--no-early-stop" if not early else f"--pairs {pairs} <= --early-at {at}" if pairs <= at else f"--early-at {at} < --min-pairs {min_pairs}" if at < min_pairs else None)


def early_plan(pairs, early=True, at=EARLY_AT, min_pairs=MIN_PAIRS, pass_z=EARLY_PASS_Z, fail_z=EARLY_FAIL_Z):
    off = early_off(pairs, early, at, min_pairs)
    return f"early stop off ({off})" if off else f"early stop after pair {at}: z > {pass_z} PASS, z < {fail_z} FAIL, else all {pairs} pairs"


def dry_lines(P, out, z_bar, min_pairs, cwd=ROOT, early_note=None):
    """The shell rendering of run: per pair the order.txt line, both launches 1 s apart, wait; then judge (the stops on exit 64-68 and the early
    stop at pair 5 are run's alone: this rendering runs every pair)."""
    q = shlex.quote; L = [f"# acceptance_test run --dry: {len(P)} pairs from {cwd}; nothing below was run"] + ([f"# {early_note} (run only)"] if early_note else []) + [f"mkdir -p {q(out)}"]
    for i, launches in P:
        first, second = launches[0][0], launches[1][0]
        L += [f"# pair {i} of {len(P)}: {first} first", f'echo "{i} {first} {second} $(date -u +%FT%TZ)" >> {q(os.path.join(out, ORDER))}']
        for j, (s, argv, log) in enumerate(launches): L += (["sleep 1"] if j else []) + [" ".join(q(a) for a in argv) + f" > {q(log)} 2>&1 &"]
        L.append("wait")
    return L + [f"python3 acceptance_test/acceptance_test.py judge {q(out)} --z {z_bar} --min-pairs {min_pairs} --json > {q(os.path.join(out, VERDICT))}"]


def run_pairs(P, out, cwd=ROOT, delay=DELAY_S, after=None):
    """Each pair: its order.txt line, the first probe, `delay` s, the second (not launched when the first already exited 64-68), wait for both;
    then after(i) -> a stop reason or None (run's early stop). -> ({pair: {side: rc, None = not launched}}, stop reason or None)."""
    os.makedirs(out, exist_ok=True); rcs = {}; op = os.path.join(out, ORDER)
    if not os.path.exists(op): open(op, "w").write("# pair first second utc (acceptance_test run: launch order; judge reads the first two fields)\n")
    for i, launches in P:
        with open(op, "a") as fh: fh.write(f"{i} {launches[0][0]} {launches[1][0]} {utc()}\n")
        t0 = time.time(); procs = []; rcs[i] = {}
        for j, (s, argv, log) in enumerate(launches):
            if j:
                time.sleep(delay)
                if procs[0][1].poll() in NO_JOB:  # the pair cannot count: spend no job on this side
                    open(log, "w").write(f"acceptance_test: not launched, the {procs[0][0]} probe exited {procs[0][1].returncode} first (probe081.sh submitted no job)\n"); rcs[i][s] = None; break
            fh = open(log, "w"); procs.append((s, subprocess.Popen(argv, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL), fh))
        for s, p, fh in procs: rcs[i][s] = p.wait(); fh.close()
        print(f"pair {i}/{len(P)} ({launches[0][0]} first) done in {time.time() - t0:.0f} s: head rc {rcs[i]['head']}, cand rc {rcs[i]['cand']} ({utc()})", flush=True)
        stop = [f"{s} probe exit {rc}" for s, rc in sorted(rcs[i].items()) if rc in NO_JOB]
        if stop: return rcs, f"pair {i}: {', '.join(stop)} (probe081.sh submitted no job), no further pair launched"
        why = after(i) if after else None
        if why: return rcs, why
    return rcs, None


def execute(head, cand, pairs, level, out, z_bar=Z, min_pairs=MIN_PAIRS, cwd=ROOT, delay=DELAY_S, early=True, early_at=EARLY_AT, pass_z=EARLY_PASS_Z, fail_z=EARLY_FAIL_Z):
    """run after its checks: the pairs (the early stop judged once pair early_at is done), then judge -> <out>/verdict.json and the human report.
    -> the exit code (judge's; an early stop's verdict is its decision)."""
    off = early_off(pairs, early, early_at, min_pairs); es = {}
    def after(i):
        if off or i != early_at: return None
        try: es.update(early_record(judge(out, z_bar, min_pairs, max_pairs=early_at), pass_z, fail_z, early_at))
        except Exception as ex:  # the early check never costs a live gate its remaining pairs or its verdict.json
            es.update(at=early_at, n=None, z=None, gain_pct=None, decision="continue", pass_z=pass_z, fail_z=fail_z, why=f"early check error ({type(ex).__name__}: {ex}): all pairs run")
        print(f"early stop check after pair {i}: {es['decision']} ({es['why']})", flush=True)
        return None if es["decision"] == "continue" else f"pair {i}: early stop {es['decision']} ({es['why']}), no further pair launched"
    print(f"acceptance_test run: head {head} vs cand {cand}, {pairs} pairs, level {level}, out {out} ({utc()})", flush=True)
    print(f"acceptance_test {early_plan(pairs, early, early_at, min_pairs, pass_z, fail_z)}", flush=True)
    rcs, stop = run_pairs(plan(head, cand, pairs, level, out), out, cwd, delay, after)
    rep = judge(out, z_bar, min_pairs); rep["early_stop"] = dict(es) if es else None
    if es.get("decision") in ("PASS", "FAIL"):  # FAIL: significantly worse (no probe failed); PASS: judge's own verdict on these pairs already
        rep.update(verdict=es["decision"], rc=RC[es["decision"]], verdict_reason=es["why"], compose_candidate=[])
    rep["run"] = {"head": head, "cand": cand, "pairs": pairs, "level": level, "probe_rc": rcs, "stopped": stop, "utc": utc(),
                  "early": {"on": off is None, "at": early_at, "pass_z": pass_z, "fail_z": fail_z, "off": off}}
    atomic.dump_json(rep, os.path.join(out, VERDICT))
    if stop: print(f"STOPPED: {stop}")
    print("\n".join(render(rep)) + f"\nwrote {os.path.join(out, VERDICT)}"); return rep["rc"]


def _usage(msg):
    print(f"acceptance_test: {msg}", file=sys.stderr); return 2


def cmd_run(a):
    for flag, lane in (("--head", a.head), ("--cand", a.cand)):
        if not LANE_RE.fullmatch(lane) or ".." in lane: return _usage(f"{flag} {lane!r} is not a lanes081 lane name (letters, digits, _ . -)")
    if a.head == a.cand: return _usage("--head and --cand name one lane: both probes would stage into one probes081 dir (build a second lane for an A/A run)")
    if a.pairs < 1: return _usage("--pairs must be >= 1")
    bad = _early_usage(a)
    if bad: return _usage(bad)
    out = a.out or os.path.join(OUT_ROOT, f"{a.cand}-vs-{a.head}-{utc_compact()}")
    if os.path.isdir(out) and any(LOG_RE.fullmatch(f) for f in os.listdir(out)): return _usage(f"{out} already holds probe logs: pass a new --out")
    P = plan(a.head, a.cand, a.pairs, a.level, out); ek = dict(early=not a.no_early_stop, early_at=a.early_at, pass_z=a.early_pass_z, fail_z=a.early_fail_z)
    if a.dry: print("\n".join(dry_lines(P, out, a.z, a.min_pairs, early_note=early_plan(a.pairs, ek["early"], a.early_at, a.min_pairs, a.early_pass_z, a.early_fail_z)))); return 0
    if os.environ.get("MICRO_EC2_LOCAL") != "1":
        return _usage("REFUSED: run launches real Arena jobs (probe081.sh) and runs on the device host only, with MICRO_EC2_LOCAL=1; --dry prints the commands")
    if not os.path.isfile(os.path.join(ROOT, PROBE)): return _usage(f"no {PROBE} under {ROOT}")
    return execute(a.head, a.cand, a.pairs, a.level, out, a.z, a.min_pairs, **ek)


def _early_usage(a):
    """The early-stop flags' usage error or None (run and replay)."""
    if a.early_at < 2: return "--early-at must be >= 2 (se needs two pairs)"
    if not all(math.isfinite(x) for x in (a.early_pass_z, a.early_fail_z)) or a.early_fail_z >= a.early_pass_z: return "--early-fail-z must be a finite z below --early-pass-z"
    return None


# ---------------------------------------------------------------- replay (read-only)

def _newest(paths):
    t = 0.0
    for p in paths:
        with contextlib.suppress(OSError): t = max(t, os.path.getmtime(p))
    return t


def _changes(items, pass_z, fail_z, at):
    """(early PASS on a non-PASS full verdict, early FAIL on a full PASS) over [(row, report of pairs 1..at)] at these bars."""
    cp = cf = 0
    for row, first in items:
        d = early_decision(first, pass_z, fail_z, at); cp += d == "PASS" and row["full"] != "PASS"; cf += d == "FAIL" and row["full"] == "PASS"
    return cp, cf


def _bar(start, step, bad, limit=4000):
    """The first start + k * step (k = 0 .. limit) where bad(bar) is 0; None when there is none (a change at an infinite z)."""
    return next((start + k * step for k in range(limit + 1) if not bad(start + k * step)), None)


def replay(root=OUT_ROOT, at=EARLY_AT, pass_z=EARLY_PASS_Z, fail_z=EARLY_FAIL_Z, now=None):
    """Read-only: the early-stop rule on pairs 1..at vs each recorded run's full judge() verdict under root (module docstring) -> dict."""
    now = time.time() if now is None else now; items, skipped, compose, disagree = [], {}, [], []; ndirs = 0
    def skip(why, name): skipped.setdefault(why, []).append(name)
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d): continue  # <dir>.run.log and strays
        ndirs += 1; m = UTC_RE.search(name)
        try:
            full = judge(d); files = os.listdir(d); stamp = m.group(1) if m else utc_compact(os.path.getmtime(d))
            if VERDICT not in files and now - _newest([d, d + ".run.log"] + [os.path.join(d, f) for f in files]) < YOUNG_S:
                skip("in progress (no verdict.json, touched < 2 h ago)", name); continue
        except UsageError: skip("no probe logs", name); continue
        except Exception as ex: skip(f"unreadable ({type(ex).__name__})", name); continue
        compose += [dict(c, dir=name, utc=stamp, n=full["n"], gain_pct=full["g"]["gain_pct"], z=full["g"]["z"]) for c in full["compose_candidate"]]
        if full["n"] <= at: skip(f"<= {at} counted pairs", name); continue
        try: first = judge(d, max_pairs=at)
        except Exception as ex: skip(f"pairs 1-{at} unreadable ({type(ex).__name__})", name); continue
        rec = atomic.load_json(os.path.join(d, VERDICT), None); rv = rec.get("verdict") if isinstance(rec, dict) else None
        if rv is not None and rv != full["verdict"]: disagree.append({"dir": name, "recorded": rv, "replayed": full["verdict"], "recorded_z_bar": rec.get("z_bar")})
        later = [r for r in full["pairs"] if r["pair"] > at]
        items.append(({"dir": name, "utc": stamp, "full": full["verdict"], "full_z": full["g"]["z"], "n": full["n"], "pairs": len(full["pairs"]), "n5": first["n"],
                       "z5": first["g"]["z"], "recorded": rv, "compose": [c["kernel"] for c in full["compose_candidate"]], "later_pairs": len(later), "later_jobs": sum(1 for r in later for s in SIDES if r[s]["job"] is not None)}, first))
    rows = []
    for row, first in items:
        dec = early_decision(first, pass_z, fail_z, at)
        rows.append(dict(row, early=dec or "continue", change=(dec == "PASS" and row["full"] != "PASS") or (dec == "FAIL" and row["full"] == "PASS"),
                         saved_pairs=row["later_pairs"] if dec else 0, saved_jobs=row["later_jobs"] if dec else 0))
    def cnt(f): return sum(1 for r in rows if f(r))
    tot = {"dirs": ndirs, "replayed": len(rows), "skipped": sum(len(v) for v in skipped.values()), "early_pass": cnt(lambda r: r["early"] == "PASS"),
           "early_fail": cnt(lambda r: r["early"] == "FAIL"), "continue": cnt(lambda r: r["early"] == "continue"), "changes": cnt(lambda r: r["change"]),
           "early_pass_on_non_pass": cnt(lambda r: r["early"] == "PASS" and r["full"] != "PASS"), "early_fail_on_pass": cnt(lambda r: r["early"] == "FAIL" and r["full"] == "PASS"),
           "early_fail_on_flat": cnt(lambda r: r["early"] == "FAIL" and r["full"] == "FLAT"), "early_fail_on_fail": cnt(lambda r: r["early"] == "FAIL" and r["full"] == "FAIL"),
           "early_fail_hides_compose": cnt(lambda r: r["early"] == "FAIL" and r["compose"]),
           "pairs_run": sum(r["pairs"] for r in rows), "pairs_saved": sum(r["saved_pairs"] for r in rows), "jobs_saved": sum(r["saved_jobs"] for r in rows)}
    bars = {"pass_z": _bar(pass_z, 0.25, lambda b: _changes(items, b, fail_z, at)[0]), "fail_z": _bar(fail_z, -0.25, lambda b: _changes(items, pass_z, b, at)[1])}
    rows.sort(key=lambda r: r["utc"], reverse=True); compose.sort(key=lambda c: c["utc"], reverse=True)
    return {"root": os.path.abspath(root), "at": at, "pass_z": pass_z, "fail_z": fail_z, "z_bar": Z, "min_pairs": MIN_PAIRS, "totals": tot, "bars": bars,
            "rows": rows, "skipped": skipped, "disagree": disagree, "compose": compose}


def render_replay(res, top=15):
    """The replay report: one line per stop or change (newest first), the totals, the 0-change bars, verdict.json agreement, the composition candidates."""
    t, b, at = res["totals"], res["bars"], res["at"]
    L = [f"acceptance_test replay {res['root']} (read-only): the early stop after pair {at} (z > {res['pass_z']} PASS, z < {res['fail_z']} FAIL, else all pairs) "
         f"vs each full run's judge() verdict (z bar {res['z_bar']}, min pairs {res['min_pairs']})"]
    for r in res["rows"]:
        if r["early"] == "continue": continue
        note = (" | early FAIL on FLAT (no change: neither files)" if r["early"] == "FAIL" and r["full"] == "FLAT" else "") + (
            f" | hides the composition candidate {', '.join(r['compose'])}" if r["early"] == "FAIL" and r["compose"] else "")
        L.append(f"  {'CHANGE' if r['change'] else 'stop  '} {r['early']:<4} at pair {at}: z {_f(r['z5'], '+7.2f')} | full {r['full']:<5} z {_f(r['full_z'], '+7.2f')} n {r['n']:>2} | "
                 f"saves {r['saved_pairs']} pairs ({r['saved_jobs']} jobs) | {r['dir']}{note}")
    sk = "; ".join(f"{len(v)} {k}" for k, v in sorted(res["skipped"].items(), key=lambda kv: -len(kv[1])))
    L += [f"totals: {t['dirs']} dirs; {t['replayed']} replayed (more than {at} counted pairs); {t['skipped']} skipped ({sk or 'none'})",
          f"  early PASS stops {t['early_pass']}, early FAIL stops {t['early_fail']} ({t['early_fail_on_flat']} early FAIL on FLAT, {t['early_fail_on_fail']} on FAIL; "
          f"{t['early_fail_hides_compose']} of them hide a full run's composition candidate), continues {t['continue']}",
          f"  verdict changes {t['changes']} (early PASS on a non-PASS full verdict {t['early_pass_on_non_pass']}, early FAIL on a full PASS {t['early_fail_on_pass']})",
          f"  saved by the early stop: {t['pairs_saved']} of {t['pairs_run']} pairs ({t['jobs_saved']} probe jobs)",
          f"bars with 0 changes on this data (0.25 steps from the given): PASS z > {_f(b['pass_z'], '.2f')} (given {res['pass_z']}), FAIL z < {_f(b['fail_z'], '.2f')} (given {res['fail_z']})",
          "recorded verdict.json vs judge(): " + ("; ".join(f"{x['dir']} recorded {x['recorded']} (z bar {x['recorded_z_bar']}), judge {x['replayed']}" for x in res["disagree"]) or "all agree")]
    C = res["compose"]
    L.append(f"composition candidates (full verdict FLAT, a kernel >= {COMPOSE_SE:g} se faster; newest first, {min(top, len(C))} of {len(C)}):" + ("" if C else " none"))
    L += [f"  {c['utc']}  {c['kernel']:<8} {c['gap']:>+8,.0f} ({c['se_units']:.2f} se)  n {c['n']:>2}  gain {_f(c['gain_pct'], '+.3f')} %  {c['dir']}" for c in C[:top]]
    return L


def cmd_replay(a):
    bad = _early_usage(a)
    if bad: return _usage(bad)
    if a.early_at < MIN_PAIRS: return _usage(f"--early-at must be >= {MIN_PAIRS} (judge's --min-pairs: fewer pairs are INSUFFICIENT, never an early PASS)")
    if not os.path.isdir(a.root): return _usage(f"no dir {a.root}")
    res = replay(a.root, a.early_at, a.early_pass_z, a.early_fail_z)
    print(json.dumps(res, indent=1) if a.json else "\n".join(render_replay(res))); return 1 if res["totals"]["changes"] else 0


# ---------------------------------------------------------------- selftest fixtures (synthesized logs, never real readings)

F3_HEAD, F3_CAND = "a3-rows-g16-tf2", "tf2-f3-onetrf-ls"
F3_PAIRS = (  # the F3 composite's measured pairs: (pair, head q a f job, cand q a f job); pairs 1-5 head launched first, 6-11 the composite first; all 9/9 PASS
    (1, (89732, 39753, 241751, 80224), (89132, 40153, 240903, 80223)), (2, (88789, 39842, 242832, 80230), (88409, 40621, 241581, 80229)),
    (3, (87615, 39878, 242541, 80231), (88844, 40053, 241351, 80232)), (4, (88857, 40013, 242845, 80234), (88128, 40264, 241404, 80233)),
    (5, (87949, 40310, 242406, 80237), (88639, 40067, 242061, 80236)), (6, (88755, 40074, 243472, 80242), (88955, 39789, 241737, 80241)),
    (7, (88329, 39745, 243762, 80245), (88430, 40090, 241791, 80244)), (8, (89784, 39587, 242775, 80247), (88694, 40375, 241342, 80246)),
    (9, (88635, 39955, 242782, 80249), (87770, 39917, 241271, 80248)), (10, (88458, 39258, 242702, 80253), (89313, 39765, 240494, 80251)),
    (11, (89520, 39693, 243852, 80255), (88233, 40047, 240774, 80254)))


def fake_probe(path, lane, job, cyc, fail=None, cut=200, t="2026-09-23T13:58:01Z"):
    """A probe081.sh log of one job with medians cyc {k}; fail = the kernel whose runs FAIL (arena FAILED, the harness's `1 of 3 tests failed`). The collect
    line is ec2_run's `arena job` line cut to `cut` characters as probe081.sh cuts it (None keeps it whole)."""
    stamp = t.replace("-", "").replace(":", ""); pd = f"/home/ubuntu/scratch081/probes081/{lane}-{stamp}"
    cd = {LONG[k]: cyc[k] for k in KERNELS}; ok = {LONG[k]: k != fail for k in KERNELS}
    line = f"arena job {job} {'FAILED' if fail else 'SUCCEEDED'} cycles={cd} pass={ok} (collected)"
    L = [f"{t} regime 081: L150 clean (src/bin src/api src/lib.rs Cargo.toml[dependencies] scripts = upstream 8559916)",
         f"{t} SUBMITTED job {job} (toolchain-081-{lane}, info) tree /home/ubuntu/scratch081/lanes081/{lane} bin 48213392 B fixtures 30146088 B -> {pd}",
         "collect rc=0: " + (line[:cut] if cut else line)]
    for k in KERNELS:
        L.append(f"==> {LONG[k]}"); bad = k == fail
        for run in range(3):
            for o in (("q", "k", "v") if k == "qkv" else ("",)):
                label = f"{LONG[k]} {o} run{run}" if o else f"{LONG[k]} run{run}"
                L.append(f"[{label:34}] max|Δ|=  {0.5 if bad else 0.03125:.5f} (   0.71% of expected)  mean|Δ|= 0.000914  within tol={12.0 if bad else 100.0:6.2f}%  -> {'FAIL' if bad else 'PASS'}")
        L.append(f"    median cycles={cyc[k]} (of 3 runs: [{cyc[k] + 412}, {cyc[k]}, {cyc[k] - 97}])")
    L += [f"1 of 3 tests failed: {LONG[fail]}", "remote_entrypoint.sh: kernel tests failed (exit 1)"] if fail else ["all 3 tests passed", "remote_entrypoint.sh: all kernel tests passed"]
    L.append(f"== probe081 {lane} job {job} done 13:59:40Z ({pd})")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    return path


def make_f3_fixture(d, keep=None):
    """The F3 composite's pairs (keep = pair numbers, default all 11) as probe logs + order.txt under d; pair 1's candidate line is left uncut."""
    os.makedirs(d, exist_ok=True); rows = [r for r in F3_PAIRS if keep is None or r[0] in keep]
    with open(os.path.join(d, ORDER), "w") as fh:
        fh.write("# pair first second utc (docs/05 2026-09-23 14:14Z: pairs 1-5 head launched first, 6-11 the composite first; times not recorded)\n")
        for i, _, _ in rows: fh.write(f"{i} head cand -\n" if i <= 5 else f"{i} cand head -\n")
    for i, h, c in rows:
        for s, lane, v in (("head", F3_HEAD, h), ("cand", F3_CAND, c)): fake_probe(log_path(d, s, i), lane, v[3], dict(zip(KERNELS, v[:3])), cut=None if (s, i) == ("cand", 1) else 200)
    return d


def make_pairs_fixture(d, heads, cands, verdict=True):
    """Synthetic pairs under d (never real readings): pair i = heads[i - 1] / cands[i - 1] ({k: cycles}), odd pairs head first; verdict=True also
    writes judge's verdict.json, as a finished run leaves it (replay's selftest)."""
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, ORDER), "w").write("".join(f"{i} {'head cand' if i % 2 else 'cand head'} -\n" for i in range(1, len(heads) + 1)))
    for i, (h, c) in enumerate(zip(heads, cands), 1): fake_probe(log_path(d, "head", i), "H", 70000 + 2 * i, h); fake_probe(log_path(d, "cand", i), "C", 70001 + 2 * i, c)
    if verdict: atomic.dump_json(judge(d), os.path.join(d, VERDICT))
    return d


def gap_fixture(d, gaps, verdict=True):
    """make_pairs_fixture with every head at one base and pair i's candidate at base + gaps[k][i - 1] (a kernel not in gaps: equal)."""
    n = len(next(iter(gaps.values()))); base = {"qkv": 88700, "attn_out": 39900, "ffn": 242800}
    return make_pairs_fixture(d, [dict(base) for _ in range(n)], [{k: base[k] + gaps.get(k, [0] * n)[i] for k in KERNELS} for i in range(n)], verdict)


STUB = r'''#!/bin/bash
echo "$1" >> "$PAIRED_STUB_CALLS"; j=$((91000 + $(wc -l < "$PAIRED_STUB_CALLS" | tr -d ' '))); k=$(grep -c "^$1\$" "$PAIRED_STUB_CALLS")
[ "$1" = NOBIN ] && { echo "no binary for $1 (x): run build081.sh first"; exit 66; }
case "$1" in H) c="88000, 'sliding_attention_output': 40000, 'decoder_feedforward': 242000";;
  SLOW) c="88000, 'sliding_attention_output': 40000, 'decoder_feedforward': $((k % 2 ? 244000 : 244600))";;
  NOISY) c="88000, 'sliding_attention_output': 40000, 'decoder_feedforward': $((k % 2 ? 241000 : 243000))";;
  *) c="87900, 'sliding_attention_output': 40010, 'decoder_feedforward': 240000";; esac
echo "2026-09-23T00:00:00Z SUBMITTED job $j (toolchain-081-$1, $2) tree x bin 1 B fixtures 1 B -> x"
echo "collect rc=0: arena job $j SUCCEEDED cycles={'sliding_project_qkv': $c} pass={'sliding_project_qkv': True, '"
echo "all 3 tests passed"; echo "== probe081 $1 job $j done 00:00:01Z (x)"
'''


def selftest():
    tmp = tempfile.mkdtemp(prefix="paired-gate-")
    def cli(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()): rc = main(argv)
        return rc, buf.getvalue()
    try:
        # (a) the F3 composite's 11 real pairs: FLAT, attn_out and ffn moved (ffn -1,546 se 217, attn_out +276 se 108, qkv -171 se 254, 7.5897 / 7.5933)
        A = make_f3_fixture(os.path.join(tmp, "f3")); r = judge(A); K = r["kernels"]
        assert r["n"] == 11 and not r["dropped"] and not r["failed"] and not r["warnings"], (r["n"], r["dropped"], r["warnings"])
        assert abs(K["ffn"]["gap_mean"] + 1546) <= 1 and abs(K["attn_out"]["gap_mean"] - 276) <= 1 and abs(K["qkv"]["gap_mean"] + 171) <= 1, {k: K[k]["gap_mean"] for k in KERNELS}
        assert [round(K[k]["gap_se"]) for k in KERNELS] == [254, 108, 217] and (round(r["score"]["head"], 4), round(r["score"]["cand"], 4)) == (7.5897, 7.5933), r["score"]
        assert round(r["g"]["gain_pct"], 2) == 0.05 and 0.3 < r["g"]["z"] < 0.5 and r["verdict"] == "FLAT" and r["rc"] == 1, r["g"]
        assert [K[k]["moved"] for k in KERNELS] == [False, True, True] and all(K["attn_out"]["split"][f"{o}_first"]["mean"] > 0 for o in SIDES), K["attn_out"]["split"]
        assert (r["g"]["split"]["head_first"]["n"], r["g"]["split"]["cand_first"]["n"]) == (5, 6) and r["lanes"] == {"head": [F3_HEAD], "cand": [F3_CAND]}
        assert r["pairs"][0]["cand"]["job"] == 80223 and r["pairs"][0]["head"]["job"] == 80224, "the uncut and the cut collect lines both parse"
        rc, txt = cli(["judge", A]); rows = [l for l in txt.splitlines() if re.match(r"\s*\d+  (head|cand) first  ", l)]
        assert rc == 1 and txt.rstrip().endswith("verdict: FLAT (gain +0.05 %, z 0.41, n 11)") and [l.split()[1] for l in rows] == ["head"] * 5 + ["cand"] * 6, txt[-300:]
        rc, js = cli(["judge", A, "--json"]); assert rc == 1 and json.loads(js)["verdict"] == "FLAT"
        # (b) a synthetic clear win: the candidate's ffn 2 % faster in every pair, qkv / attn_out equal up to +-150 cycles of noise
        B = os.path.join(tmp, "win"); os.makedirs(B); rnd = random.Random(20260923); base = {"qkv": 88700, "attn_out": 39900, "ffn": 242800}
        open(os.path.join(B, ORDER), "w").write("".join(f"{i} {'head cand' if i % 2 else 'cand head'} -\n" for i in range(1, 9)))
        for i in range(1, 9):
            h = {k: base[k] + rnd.randint(-150, 150) for k in KERNELS}; c = {k: base[k] + rnd.randint(-150, 150) for k in KERNELS}; c["ffn"] = round(base["ffn"] * 0.98) + rnd.randint(-150, 150)
            fake_probe(log_path(B, "head", i), "H", 90000 + 2 * i, h); fake_probe(log_path(B, "cand", i), "C", 90001 + 2 * i, c)
        r = judge(B); assert r["verdict"] == "PASS" and r["n"] == 8 and 0.6 < r["g"]["gain_pct"] < 0.75 and r["g"]["z"] > 5 and r["kernels"]["ffn"]["moved"], (r["verdict"], r["g"])
        assert cli(["judge", B])[0] == 0
        # (c) one candidate probe fails a test (arena FAILED, `-> FAIL` runs, no `all 3 tests passed`): FAIL, exit 3, even with 10 good pairs
        C = os.path.join(tmp, "fail"); shutil.copytree(A, C); fake_probe(log_path(C, "cand", 3), F3_CAND, 80232, {"qkv": 88844, "attn_out": 40053, "ffn": 241351}, fail="ffn")
        r = judge(C); rc, txt = cli(["judge", C])
        assert r["verdict"] == "FAIL" and rc == 3 and r["n"] == 10 and [(f["pair"], f["side"], f["job"]) for f in r["failed"]] == [(3, "cand", 80232)], r["failed"]
        assert "1 of 3 tests failed: decoder_feedforward" in r["failed"][0]["why"] and "FAIL: pair 3 cand (job 80232)" in txt and "verdict: FAIL (" in txt, txt[-400:]
        neg = fake_probe(os.path.join(tmp, "neg.log"), F3_CAND, 1, base); text = open(neg).read()
        open(neg, "w").write(text.replace("pass={'sliding_project_qkv': True", "pass={'sliding_project_qkv': False"))
        assert read_probe(neg)["status"] == "fail" and "pass= False: qkv" in read_probe(neg)["why"], read_probe(neg)
        # (d) fewer pairs than --min-pairs: INSUFFICIENT (exit 1); the same 4 pairs judge under --min-pairs 3
        D = make_f3_fixture(os.path.join(tmp, "four"), keep=(1, 2, 3, 4)); r = judge(D)
        assert r["verdict"] == "INSUFFICIENT" and r["n"] == 4 and cli(["judge", D])[0] == 1 and judge(D, min_pairs=3)["verdict"] in ("FLAT", "PASS"), r["verdict"]
        # (e) pair 7 lost its head log, pair 9's candidate never read a result (collect INFRA ERROR): both dropped and reported, no FAIL
        E = os.path.join(tmp, "missing"); shutil.copytree(A, E); os.remove(log_path(E, "head", 7))
        open(log_path(E, "cand", 9), "w").write("2026-09-23T14:30:00Z SUBMITTED job 80248 (toolchain-081-tf2-f3-onetrf-ls, info) tree x bin 1 B fixtures 1 B -> x\n"
                                                 "collect rc=3: INFRA ERROR: CalledProcessError(127, ['/home/ubuntu/micro2026/repo/env/ec2.sh', 'ip'])\n== probe081 tf2-f3-onetrf-ls job 80248 done 14:31:00Z (x)\n")
        r = judge(E); _, txt = cli(["judge", E])
        assert r["n"] == 9 and r["verdict"] == "FLAT" and [d["pair"] for d in r["dropped"]] == [7, 9] and "head missing (no log)" in r["dropped"][0]["why"], r["dropped"]
        assert "cand missing (no result line)" in r["dropped"][1]["why"] and "dropped: head missing (no log)" in txt and "not counted: 7, 9" in txt, txt
        # (h) early_decision, the pure rule: strict bars; only a clean report of exactly `at` counted pairs decides; an early PASS never under --z
        def ER(z, n=5, failed=(), v="PASS"): return {"g": {"z": z}, "n": n, "failed": list(failed), "verdict": v}
        assert (early_decision(ER(4.01)), early_decision(ER(4.0)), early_decision(ER(-2.01, v="FLAT")), early_decision(ER(-2.0, v="FLAT"))) == ("PASS", None, "FAIL", None)
        assert early_decision(ER(9.0, n=4)) is None and early_decision(ER(-9.0, failed=[{}], v="FAIL")) is None and early_decision(ER(None, v="INSUFFICIENT")) is None
        assert early_decision(ER(5.0, v="FLAT")) is None and early_decision(ER(math.inf)) == "PASS" and early_decision(ER(3.0), pass_z=2.5) == "PASS" and early_decision(ER(9.0, n=6), at=6) == "PASS"
        # (i) judge's first N pairs of synthetic 11-pair runs: ffn 2 % faster -> PASS at 5; ffn 1 % slower -> FAIL at 5 (significantly worse); mixed -> continue
        e = [50, -30, 20, -40, 10, 0, 30, -20, 40, -10, -50]; mid = [400, -300, 200, -400, 300, -200, 100, -300, 400, -100, 0]
        W = gap_fixture(os.path.join(tmp, "early-win"), {"ffn": [-4856 + x for x in e]}, verdict=False)
        S = gap_fixture(os.path.join(tmp, "early-worse"), {"ffn": [2428 + x for x in e]}, verdict=False); M = gap_fixture(os.path.join(tmp, "early-mid"), {"ffn": mid}, verdict=False)
        w5, s5, m5 = (judge(x, max_pairs=5) for x in (W, S, M))
        assert [len(x["pairs"]) for x in (w5, s5, m5)] == [5, 5, 5] and len(judge(W)["pairs"]) == 11 and w5["max_pairs"] == 5, "max_pairs reads pairs 1-5 only"
        assert (early_decision(w5), early_decision(s5), early_decision(m5)) == ("PASS", "FAIL", None) and w5["g"]["z"] > 4 and s5["g"]["z"] < -2 and -2 < m5["g"]["z"] < 4, (w5["g"]["z"], s5["g"]["z"], m5["g"]["z"])
        er = early_record(s5); assert er["decision"] == "FAIL" and er["why"] == f"early stop: significantly worse, z {s5['g']['z']:.2f} at 5 pairs" and early_record(m5)["decision"] == "continue", er
        assert (judge(W)["verdict"], judge(S)["verdict"], judge(M)["verdict"]) == ("PASS", "FLAT", "FLAT")
        # (j) the composition candidate: F3 is FLAT with ffn -1,546 at 7.1 se -> marked (verdict line still last); PASS, FAIL, a FLAT without a fast kernel -> none
        r = judge(A); cc = r["compose_candidate"]; txt = "\n".join(render(r))
        assert [c["kernel"] for c in cc] == ["ffn"] and abs(cc[0]["gap"] + 1546) <= 1 and 7.0 < cc[0]["se_units"] < 7.3, cc
        assert re.search(r"^COMPOSITION CANDIDATE: ffn -1,546 \(7\.\d\d se\) -> workers/shared/COMPOSE\.md$", txt, re.M) and txt.endswith("verdict: FLAT (gain +0.05 %, z 0.41, n 11)"), txt[-500:]
        assert judge(B)["compose_candidate"] == [] and judge(C)["compose_candidate"] == [] and judge(M)["compose_candidate"] == [], "PASS, FAIL, FLAT without a >= 3 se faster kernel"
        # (f) run --dry alternates the launch order and names the logs judge reads; without --dry and MICRO_EC2_LOCAL=1 run refuses (exit 2), as for one lane twice
        F = os.path.join(tmp, "dry"); rc, txt = cli(["run", "--head", "H", "--cand", "C", "--pairs", "4", "--dry", "--out", F])
        seq = [(w[2], w[5]) for w in (shlex.split(l) for l in txt.splitlines() if l.startswith("bash " + PROBE))]; LN = {"head": "H", "cand": "C"}
        want = [(LN[s], log_path(F, s, i)) for i, sides in ((1, SIDES), (2, SIDES[::-1]), (3, SIDES), (4, SIDES[::-1])) for s in sides]
        assert rc == 0 and seq == want and txt.count("\nsleep 1\n") == 4 and txt.count("\nwait\n") == 4 and "judge " + F in txt and not os.path.exists(F), txt
        assert [l.split("\"")[1].split()[:2] for l in txt.splitlines() if l.startswith("echo ")] == [["1", "head"], ["2", "cand"], ["3", "head"], ["4", "cand"]]
        saved = os.environ.pop("MICRO_EC2_LOCAL", None)
        try: assert cli(["run", "--head", "H", "--cand", "C", "--pairs", "2", "--out", F])[0] == 2 and not os.path.exists(F), "run without MICRO_EC2_LOCAL=1 must refuse"
        finally:
            if saved is not None: os.environ["MICRO_EC2_LOCAL"] = saved
        assert cli(["run", "--head", "H", "--cand", "H", "--pairs", "2", "--dry"])[0] == 2 and cli(["judge", os.path.join(tmp, "nothere")])[0] == 2
        assert "\n# early stop off (--pairs 4 <= --early-at 5) (run only)\n" in txt, txt[:300]
        rc, txt = cli(["run", "--head", "H", "--cand", "C", "--pairs", "11", "--dry", "--out", F])
        assert rc == 0 and "\n# early stop after pair 5: z > 4.0 PASS, z < -2.0 FAIL, else all 11 pairs (run only)\n" in txt and txt.count("\nwait\n") == 11, txt[:300]
        assert "early stop off (--no-early-stop)" in cli(["run", "--head", "H", "--cand", "C", "--pairs", "11", "--dry", "--no-early-stop"])[1]
        assert [cli(["run", "--head", "H", "--cand", "C", "--pairs", "11", "--dry"] + x)[0] for x in (["--early-at", "1"], ["--early-fail-z", "4"], ["--early-pass-z", "nan"])] == [2, 2, 2]
        # (g) the launch loop itself against a stub probe081.sh in a temp repo root: launch order, logs, order.txt, verdict.json; a probe exit 66 stops the run
        R = os.path.join(tmp, "root"); os.makedirs(os.path.join(R, "tools", "081")); open(os.path.join(R, PROBE), "w").write(STUB)
        os.environ["PAIRED_STUB_CALLS"] = calls = os.path.join(tmp, "calls.txt"); G = os.path.join(tmp, "g")
        try:
            with contextlib.redirect_stdout(io.StringIO()): rc = execute("H", "C", 2, "info", G, min_pairs=2, cwd=R, delay=0.2)
            v = json.load(open(os.path.join(G, VERDICT)))
            assert open(calls).read().split() == ["H", "C", "C", "H"] and read_order(G) == {1: "head", 2: "cand"} and rc == 0 and v["n"] == 2 and v["run"]["stopped"] is None, (open(calls).read(), v["verdict"])
            assert v["verdict"] == "PASS" and v["g"]["z"] == math.inf and v["run"]["probe_rc"] == {"1": {"head": 0, "cand": 0}, "2": {"head": 0, "cand": 0}}, v["g"]
            assert v["early_stop"] is None and v["run"]["early"]["off"] == "--pairs 2 <= --early-at 5" and not v["run"]["early"]["on"], v["run"]["early"]
            with contextlib.redirect_stdout(io.StringIO()): rc = execute("H", "NOBIN", 3, "info", os.path.join(tmp, "g2"), cwd=R, delay=0.2)
            v = json.load(open(os.path.join(tmp, "g2", VERDICT)))
            assert open(calls).read().split()[4:] == ["H", "NOBIN"] and "cand probe exit 66" in v["run"]["stopped"] and rc == 1 and v["dropped"][0]["why"].startswith("cand missing"), v["run"]
            G3 = os.path.join(tmp, "g3")
            with contextlib.redirect_stdout(io.StringIO()): execute("NOBIN", "C", 3, "info", G3, cwd=R, delay=0.5)
            v = json.load(open(os.path.join(G3, VERDICT)))
            assert open(calls).read().split()[6:] == ["NOBIN"] and v["run"]["probe_rc"] == {"1": {"head": 66, "cand": None}} and "not launched" in open(log_path(G3, "cand", 1)).read(), v["run"]
            # (k) run's early stop on the stub (--pairs 11): C (z inf) stops at pair 5 PASS; SLOW (ffn about +1 %) stops at 5 FAIL; NOISY runs on (7 pairs)
            def stub_run(cand, pairs, out, **kw):
                os.environ["PAIRED_STUB_CALLS"] = cf = os.path.join(tmp, f"calls-{os.path.basename(out)}.txt"); buf = io.StringIO()
                with contextlib.redirect_stdout(buf): rc = execute("H", cand, pairs, "info", out, cwd=R, delay=0.1, **kw)
                return rc, json.load(open(os.path.join(out, VERDICT))), open(cf).read().split(), buf.getvalue()
            rc, v, ck, txt = stub_run("C", 11, os.path.join(tmp, "k1")); es = v["early_stop"]
            assert rc == 0 and v["verdict"] == "PASS" and v["n"] == 5 and len(ck) == 10 and (es["decision"], es["at"], es["n"]) == ("PASS", 5, 5) and v["compose_candidate"] == [], (rc, v["verdict"], es, ck)
            assert v["run"]["stopped"].startswith("pair 5: early stop PASS") and "early stop at pair 5: PASS (early stop: significantly better, z inf > 4.0 at 5 pairs); pairs 6-11 not run" in txt, txt[-700:]
            rc, v, ck, txt = stub_run("SLOW", 11, os.path.join(tmp, "k2")); es = v["early_stop"]
            assert rc == 3 and v["verdict"] == "FAIL" and not v["failed"] and len(ck) == 10 and es["decision"] == "FAIL" and es["z"] < -2 and v["compose_candidate"] == [], (rc, v["verdict"], es)
            assert v["verdict_reason"] == f"early stop: significantly worse, z {es['z']:.2f} at 5 pairs" and txt.rstrip().endswith(f"n 5; {v['verdict_reason']})\nwrote {os.path.join(tmp, 'k2', VERDICT)}"), txt[-400:]
            rc, v, ck, txt = stub_run("NOISY", 7, os.path.join(tmp, "k3")); es = v["early_stop"]
            assert rc == 1 and v["verdict"] == "FLAT" and v["n"] == 7 and len(ck) == 14 and es["decision"] == "continue" and -2 < es["z"] < 4 and v["run"]["stopped"] is None, (v["verdict"], es)
            assert "early stop check at pair 5: continue (not significant (z " in txt and v["verdict_reason"] is None, txt[-600:]
            rc, v, ck, _ = stub_run("C", 6, os.path.join(tmp, "k4"), early=False)
            assert rc == 0 and v["n"] == 6 and len(ck) == 12 and v["early_stop"] is None and v["run"]["early"]["off"] == "--no-early-stop", v["run"]
        finally: os.environ.pop("PAIRED_STUB_CALLS", None)
        # (l) replay over a synthetic root: an early PASS on a full FLAT (the one change), an early PASS on PASS and an early FAIL on FLAT (no change),
        #     2 continues (one a FLAT with ffn >= 3 se faster: a composition candidate), an early FAIL that hides one; skipped: 3 pairs, an empty dir, a run in progress
        RR = os.path.join(tmp, "replay"); os.makedirs(RR)
        def mk(name, gaps, v=True): return gap_fixture(os.path.join(RR, name + "-vs-h-20260924T0" + str(len(os.listdir(RR))) + "0000Z"), gaps, v)
        mk("chg", {"ffn": [-1500, -900, -1200, -600, -1800] + [2000 + x for x in (30, -20, 10, -40, 20, 0)]}); mk("win", {"ffn": [-4856 + x for x in e]})
        mk("worse", {"ffn": [2428 + x for x in e]}); mk("mid", {"ffn": mid}); mk("fast", {"ffn": [-1200 + x for x in e], "attn_out": [2500, -1500, 2000, -1000, 3000, -2000, 1500, -500, 2500, -1500, 1000]})
        mk("three", {"ffn": [-4856, -4800, -4900]}); os.makedirs(os.path.join(RR, "empty-vs-h-20260924T060000Z")); mk("running", {"ffn": [-4856 + x for x in e[:6]]}, False)
        mk("hide", {"ffn": [-1200 + x for x in e], "attn_out": [1200 + x for x in e[:5]] + [-1200 + x for x in e[5:]]})  # early FAIL, full FLAT + ffn candidate
        open(os.path.join(RR, "chg-vs-h-20260924T000000Z.run.log"), "w").write("acceptance_test run: head h vs cand chg, 11 pairs (a stray file, not a dir)\n")
        def snap(): return {os.path.join(p, f): (os.path.getmtime(os.path.join(p, f)), os.path.getsize(os.path.join(p, f))) for p, ds, fs in os.walk(RR) for f in fs + ds}
        before = snap(); rp = replay(RR); t = rp["totals"]; rows = {x["dir"].split("-vs-")[0]: x for x in rp["rows"]}
        assert snap() == before, "replay writes nothing under its root (files and dirs, mtimes and sizes)"
        assert (t["dirs"], t["replayed"], t["skipped"], t["early_pass"], t["early_fail"], t["continue"], t["changes"], t["early_fail_on_flat"], t["early_fail_hides_compose"]) == (9, 6, 3, 2, 2, 2, 1, 2, 1), t
        assert rows["chg"]["change"] and (rows["chg"]["early"], rows["chg"]["full"]) == ("PASS", "FLAT") and not rows["win"]["change"] and rows["worse"]["early"] == "FAIL" and not rows["worse"]["change"], rows
        assert (t["pairs_saved"], t["jobs_saved"], t["pairs_run"]) == (24, 48, 66) and sorted(rp["skipped"]) == ["<= 5 counted pairs", "in progress (no verdict.json, touched < 2 h ago)", "no probe logs"], (t, rp["skipped"])
        z5, bp = rows["chg"]["z5"], rp["bars"]["pass_z"]
        assert 4.0 < z5 <= bp < z5 + 0.25 and (bp - 4.0) % 0.25 == 0 and rp["bars"]["fail_z"] == -2.0 and rows["fast"]["full"] == "FLAT", (z5, rp["bars"])
        assert [(c["dir"].split("-vs-")[0], c["kernel"]) for c in rp["compose"]] == [("hide", "ffn"), ("fast", "ffn")] and rp["compose"][0]["se_units"] > 3 and rows["hide"]["early"] == "FAIL", rp["compose"]
        rc, txt = cli(["replay", "--root", RR]); assert rc == 1 and "  CHANGE PASS at pair 5" in txt and "early FAIL on FLAT (no change" in txt and "verdict changes 1 " in txt and "hides the composition candidate ffn" in txt, txt
        assert f"PASS z > {bp:.2f} (given 4.0)" in txt and cli(["replay", "--root", RR, "--early-pass-z", str(bp)])[0] == 0 and json.loads(cli(["replay", "--root", RR, "--json"])[1])["totals"]["changes"] == 1
        assert cli(["replay", "--root", RR, "--early-at", "4"])[0] == 2 and cli(["replay", "--root", os.path.join(tmp, "nothere")])[0] == 2 and snap() == before
    finally: shutil.rmtree(tmp, ignore_errors=True)
    print("acceptance_test selftest ok: (a) the F3 11 pairs FLAT (+0.05 %, z 0.41; ffn -1,546 se 217 and attn_out +276 se 108 moved, qkv -171 se 254 not; 7.5897 / 7.5933), "
          "(b) ffn 2 % win PASS, (c) one failed cand probe FAIL rc 3, (d) 4 pairs INSUFFICIENT, (e) a lost log and a result-less probe dropped + reported, "
          "(f) run --dry alternates head / cand first and names judge's logs, refuses off EC2, (g) the launch loop on a stub probe081.sh (order, logs, verdict.json; "
          "an exit 66 stops the run, a first probe's exit 66 keeps the second unlaunched), (h) early_decision's strict bars, (i) pairs 1-5 of synthetic 11-pair runs: "
          "PASS / FAIL / continue at 5, (j) F3 FLAT marks ffn a COMPOSITION CANDIDATE, (k) run on the stub stops at pair 5 PASS (rc 0) and FAIL (rc 3), runs on "
          "when not significant, --no-early-stop, (l) replay: 1 change of 6 replayed, an early FAIL hiding a composition candidate, 3 odd dirs skipped, the PASS bar raised to 0 changes, nothing written")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv):
    if "--selftest" in argv: return selftest()
    if argv[:1] == ["--make-fixture"]:
        if len(argv) != 2: return _usage("--make-fixture DIR")
        d = make_f3_fixture(argv[1]); print(f"wrote {d}: 22 probe logs + order.txt (the F3 composite's 11 pairs, jobs 80223-80255: synthesized logs, the recorded cycles)"); return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0]); sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="device host: N paired probes of the head and the candidate lane, then judge"); p.add_argument("--head", required=True); p.add_argument("--cand", required=True)
    p.add_argument("--pairs", type=int, required=True); p.add_argument("--level", default="info", choices=LEVELS); p.add_argument("--out"); p.add_argument("--dry", action="store_true")
    p.add_argument("--z", type=float, default=Z); p.add_argument("--min-pairs", type=int, default=MIN_PAIRS)
    def early_flags(p):
        p.add_argument("--early-at", type=int, default=EARLY_AT); p.add_argument("--early-pass-z", type=float, default=EARLY_PASS_Z)
        p.add_argument("--early-fail-z", type=float, default=EARLY_FAIL_Z)
    early_flags(p); p.add_argument("--no-early-stop", action="store_true")
    p = sub.add_parser("judge", help="the paired verdict of a run's out dir"); p.add_argument("out"); p.add_argument("--z", type=float, default=Z)
    p.add_argument("--min-pairs", type=int, default=MIN_PAIRS); p.add_argument("--json", action="store_true")
    p = sub.add_parser("replay", help="read-only: the early-stop rule replayed over the recorded run dirs"); p.add_argument("--root", default=OUT_ROOT)
    early_flags(p); p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "replay": return cmd_replay(a)
    if a.min_pairs < 2: return _usage("--min-pairs must be >= 2 (se needs two pairs)")
    if a.cmd == "run": return cmd_run(a)
    try: rep = judge(a.out, a.z, a.min_pairs)
    except UsageError as ex: return _usage(str(ex))
    print(json.dumps(rep, indent=1) if a.json else "\n".join(render(rep))); return rep["rc"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
