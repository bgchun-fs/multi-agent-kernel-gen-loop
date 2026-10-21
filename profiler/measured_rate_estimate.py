#!/usr/bin/env python3
"""Re-time a static schedule with measured device rates and print its real-rate critical path.

  python3 profiler/measured_rate_estimate.py <run dir | schedule.json> <kernel> [--rates FILE] [--parent <run dir | schedule.json>]
                                   [--diff PATCH] [--json] [--top N] [--s-design [--s-median N] [--s-skip dma|dma+tu]]
  python3 profiler/measured_rate_estimate.py --rates-from <timeline.json | trace dir> <kernel>
  python3 profiler/measured_rate_estimate.py --selftest

S mode (--s-design): the plain re-timing is cluster 0's program with no launch skew, so an S-hiding design
(more bytes on `Cluster = m![1 # 2]` to absorb S) always reads worse. S mode re-times both clusters: cluster 0 = the whole program (T0),
cluster 1 = the program without the `1 # 2` rows (the cluster-0-only DMA commands; with --s-skip dma+tu also the `1 # 2` passes) started
S later (--s-median, default S_MEDIAN per kernel), both waiting at every ExplicitSync; the estimate is the later cluster's end, which for one
final rendezvous is max(T0, S + T1) + tail. The line prints both terms and which binds; --json carries `s_mode` / `parent_s_mode`.

kernel = qkv | attn_out | ffn | the full test name; a run dir holds schedule/<kernel>.json (an `ec2_run.py --out` dir, a candidate's
.run/static, an iterations/<n>/ dir) and, when a trace was recorded, trace/spans/timeline.json or spans/timeline.json.

Why: the compiler's schedule runs weight DMA at ~1,175 B/cycle while the device streams at 614-690, prices a small DMA at
~0.5k (device 0.8-2.7k), a TU pass at about half its device time, and the ratios differ by
row class, so the STATIC critical path (the main chain) is not the DEVICE critical path (the weight streams on qkv, the barrier chain
plus half B on ffn, S on attn_out): the static-visible chain cuts read flat on the device. This tool keeps the schedule's rows,
program order and tensor dependencies, replaces every duration by
its measured class and replays the dispatch; the gate question becomes "does the diff shorten a row on the RE-TIMED critical path".

Model (per chip; the traced cluster 0 stands for both clusters):
  program order  the instruction array order (= index order). The PeCore dispatches in that order: a TU pass is dispatched when its
                 context is free and its inputs landed, and blocks the dispatch of everything behind it until then; a DMA is
                 dispatched when its inputs landed and enters the one
                 in-order DMA FIFO (transfer begin = max(dispatch, previous DMA's completion)); an ExplicitSync
                 blocks the PeCore until it completes and starts only when the sub queue drained and the stores before it landed;
                 DramReuse programs are region bookkeeping and block nothing; PeCore view programs (`Core` rows with a
                 numeric index) cost 3x their static 10 cycles; zero-lifetime rows (views, allocations) forward their producers' landing.
  queues         DmaEngine (FIFO), MainContext, SubContext (one row at a time each), PeCore.
  dependencies   input_tensors -> the latest earlier producer of the tensor (output_tensors), through zero-lifetime forwarders.
  durations      DmaEngine rows: fixed + bytes / rate by class (bytes = live bytes written on the DM side, profiler/device_timeline.py rule):
                   stream  a load >= 1 MB     qkv 614 B/cycle; attn_out 620 for two Wo halves,
                                              690 when Wo is ONE command; ffn by size: >= 16 MB 625 (m1 / m2 / half A),
                                              8-16 MB 595 (half B), 3-8 MB 340 (the 3.69 MB up / gate scale loads),
                                              1-3 MB 165 (the down scale loads)
                   seed    a load 64 KB..1 MB 800 + bytes / 220 (the broadcast x / rms seeds)
                   small   a load < 64 KB     1,200 + bytes / 150 (4-8 KB scale / cos / sin / residual loads)
                   table   a load without bytes 2,000 (the fetch_table staging loads)
                   dm2dm   DmaStos or a load whose source is DM: 1,860 + bytes / 44
                   store   DmaStore 1,000 + bytes / 60; scatter DmaScatter 1,600
                 Main rows: static x 2.0 (qkv), 2.1 (attn_out), 2.4 (ffn)
                 Sub rows:  static x 1.9 (qkv), 2.2 (attn_out), 2.2 (ffn)
                 ExplicitSync: static x 1.0 (600, the barrier's own cost; the launch skew S is a per-launch draw the model leaves
                 out); DramReuse: 0 (it carries no tensors; on the device it ends when the row that reuses the region
                 may go, which that row's own inputs already say); PeCore view programs: static x 3.0.
  --rates FILE   a json whose keys override DEFAULT_RATES (any subset). --parent re-times the parent's schedule with the same rates and
                 prints the delta. --diff PATCH names the schedule rows whose source line the unified diff touches (its + hunks, 3 lines of
                 slack) and whether they sit on the re-timed critical path: tq_runner's "touched segment class".
Output: re-timed makespan, per-context busy, the critical path (rows with begin / end / slack 0, classed as in profiler/critical_path.py) and
the comparison line `static X -> real-rate Y (device trace Z ...)` when a trace exists; the model has no S, so it is compared with the
trace total minus the uncovered skew window (`P_uncov`) and with total - S.
Calibration (--selftest on the recorded head traces under iterations/, schedule + trace, when present): the re-timed makespan against
P_uncov must be within 10 %.
Exit 0; 2 usage or missing input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import device_timeline as _spans  # noqa: E402
import critical_path as _cp  # noqa: E402

K3 = _cp.K3
SHORT = _cp.SHORT
STREAM_MIN = 1_000_000
SEED_MIN = 65_536
MIRROR = os.path.expanduser("iterations")
CALIB = {K3[0]: "1872-qkv-trace-retry-1869", K3[1]: "1867-attn_out-trace-retry-1866", K3[2]: "1810-ffn-ffn-f8-direct-contraction-two-term-x"}
CALIB_TOL_PCT = 10.0

DEFAULT_RATES = {
    "stream": {K3[0]: [[0, 614]], K3[1]: [[0, 620]], K3[2]: [[16_000_000, 625], [8_000_000, 595], [3_000_000, 480], [0, 450]]},
    # ffn loads are priced by their DM-side RUN LENGTH first: whole-row runs (>= 1,024 B: the f4
    # weights' 1,920 B rows, the down tiles' 3,840 B rows) take the by-size `stream` table (the 5.9 MB tiles
    # priced 480 so that both ffn heads stay within +-2 %); 480 B staged scale runs 320; 240 B up / gate scale runs 340; 120 B direct scale runs 165
    "stream_by_run": {K3[2]: [[1024, "size"], [256, 320], [128, 340], [0, 165]]},
    "stream_one_command": {K3[1]: 690},
    "stream_c0only": 337,  # a weight command on `Cluster = m![1 # 2]` streams on one cluster's four engines: the per-cluster cap (S designs)
    "seed": [800, 220], "small": [1200, 150], "table": [2000, 0], "dm2dm": [1860, 44], "store": [1000, 60], "scatter": [1600, 0],
    "main": {K3[0]: 2.0, K3[1]: 2.1, K3[2]: 2.4}, "sub": {K3[0]: 1.9, K3[1]: 2.2, K3[2]: 2.2},
    "corun": {K3[0]: 2.0, K3[1]: 1.0, K3[2]: 1.0},  # extra factor on a Main / Sub row dispatched while a weight stream is in flight (4-9x static under Wq; qkv 2.0 keeps head-like items within +-2 %)
    "sub_kind": {K3[2]: {"sub_tab": 4.0}},  # per-kind sub factors: an ffn table write (StoTab, empty-description Sub row) runs 4.4-8.0x static, not the 2.2x of the other sub rows; 4.0 keeps both heads within +-2 % (5.1 fits the rows but lifts one head past +-2 %)
    "core": 3.0, "sync": 1.0,
}
CLASS_GROUP = {"stream": "stream", "seed": "small-dma", "small": "small-dma", "table": "small-dma", "dm2dm": "small-dma", "store": "store",
               "scatter": "store", "main": "main", "sub": "sub", "core": "core", "sync": "sync", "dramreuse": "sync", "wait": "wait"}
PRI = {"queue": 0, "dep": 1, "store": 2, "subdrain": 3, "pecore": 4, "issue": 5}


def kernel_name(k):
    return SHORT.get(k, k)


def merge_rates(over):
    R = json.loads(json.dumps(DEFAULT_RATES))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(R.get(k), dict):
            R[k].update(v)
        else:
            R[k] = v
    return R


# ---------------------------------------------------------------- loading ------------------------------------------------

def find_schedule(path, kernel):
    if os.path.isfile(path):
        return path
    for rel in (f"schedule/{kernel}.json", f"trace/schedule/{kernel}.json", f"{kernel}.json"):
        p = os.path.join(path, rel)
        if os.path.isfile(p):
            return p
    return None


def find_trace(path):
    if os.path.isfile(path):
        path = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    for rel in ("trace/spans/timeline.json", "spans/timeline.json"):
        p = os.path.join(path, rel)
        if os.path.isfile(p):
            return p
    return None


def load_rows(path):
    """Schedule rows in program order with the fields the model needs (profiler/device_timeline.py byte rules)."""
    d = json.load(open(path))
    tens = {}
    for t in d.get("tensors") or []:
        try:
            tens[int(t["index"])] = t
        except (KeyError, TypeError, ValueError):
            pass
    rows = []
    for pos, i in enumerate(d.get("instructions") or []):
        lt = i.get("lifetime") or {}
        b, e = int(lt.get("begin", 0)), int(lt.get("end", 0))
        tpe = str(i.get("tpe", ""))
        index = str(i.get("index"))
        desc = i.get("description") or ""
        call = desc.strip().split("\n")[-1].strip() if desc.strip() else ""
        src = (re.search(r"-->\s*(\S+:\d+)", desc) or [None, None])[1]
        fam = _spans.family_of_tpe(tpe, index)
        ins = [int(x) for x in (i.get("input_tensors") or [])]
        outs = [int(x) for x in (i.get("output_tensors") or [])]
        row = {"pos": pos, "index": index, "tpe": tpe, "family": fam, "kind": _spans.static_kind(tpe, desc, call, index),
               "ctx": ",".join(i.get("contexts") or []), "src": src, "call": call[:90], "static_begin": b, "static_end": e,
               "static_dur": e - b, "in": ins, "out": outs}
        row.update(_spans.dma_bytes(i, tpe, tens) if fam == "dma" else _spans.NO_BYTES)
        row["src_buffer"] = "+".join(sorted({str(tens[t].get("buffer_type")) for t in ins if t in tens})) if fam == "dma" else None
        row["cluster"] = cluster_digit(tens, (ins or outs) if tpe == "DmaStore" else (outs or ins))
        row["c0only"] = row["cluster"] == C0_ONLY  # placed on `layout::Cluster = m![1 # 2]`: cluster 0 does it, cluster 1 skips the DMA command
        row["run_bytes"] = run_bytes_of(tens, outs or ins) if fam == "dma" and tpe == "DmaLoad" else None
        rows.append(row)
    return rows, max((r["static_end"] for r in rows), default=0)


C0_ONLY = "1 # 2"
AXES = {"H": 3840, "L": 15360, "Ns": 8, "Gs": 2, "Ds": 256, "Qs": 4096, "Ps": 2048, "E": 32768, "Dummy2": 2, "Dummy8": 8, "Dummy256": 256}  # src/axes.rs, src/lib.rs


def digit_extent(d):
    """The extent of one MappingShape digit: `L / 60 % 128` -> 128, `H / 12 % 5 = 3` -> 3, `1 # 2` -> 2 (the padded footprint), `H / 16` -> 240, `H` -> 3840."""
    d = d.strip()
    for pat in (r"=\s*(\d+)\s*$", r"#\s*(\d+)\s*$", r"%\s*(\d+)\s*$"):
        m = re.search(pat, d)
        if m:
            return int(m.group(1))
    m = re.fullmatch(r"([A-Za-z]\w*)\s*/\s*(\d+)", d)
    if m and m.group(1) in AXES:
        return AXES[m.group(1)] // int(m.group(2))
    if d in AXES:
        return AXES[d]
    return int(d) if d.isdigit() else None


def run_bytes_of(tens, refs):
    """The DM-side RUN LENGTH in bytes of a load: the innermost Element digit's extent x the element size (footprint / all digits' extents).
    ffn head: weights `(L / 4 % 15, L % 2, H)` 0.5 B f4 -> 1,920 B, `(.., L % 7680)` -> 3,840 B; up / gate scales `(L % 30, H / 16)` -> 240 B;
    staged down scales `(.., L / 16 % 480)` -> 480 B; direct down scales `(.., L / 16 % 120)` -> 120 B."""
    for t in refs:
        tt = tens.get(t) or {}
        shape, size = tt.get("shape") or "", tt.get("size")
        m = re.search(r"Cluster:\s*(.+?),\s*Slice:\s*(.+?),\s*Element:\s*(.+)\]$", shape)
        if not m or not size:
            continue
        ext = []
        for part in m.groups():
            part = part.strip()
            if part.startswith("("):
                part = part[1:-1] if part.endswith(")") else part[1:]
            ext.append([digit_extent(x) for x in re.split(r",\s*", part) if x.strip()])
        flat = [x for g in ext for x in g]
        if not ext[2] or None in flat or 0 in flat:
            return None
        total = 1
        for x in flat:
            total *= x
        return int(round(ext[2][-1] * size / total))
    return None


def cluster_digit(tens, refs):
    """The Cluster digit of the row's primary tensor (`MappingShape[Chip: 1, Cluster: <digit>, Slice: .., Element: ..]`), or None."""
    for t in refs:
        m = re.search(r"Cluster:\s*([^,\]]+)", (tens.get(t) or {}).get("shape") or "")
        if m:
            return m.group(1).strip()
    return None


# ---------------------------------------------------------------- classes and durations -------------------------------------

def queue_of(r):
    if r["static_dur"] <= 0:
        return None
    if r["family"] == "dma":
        return "dma"
    if r["tpe"] == "Main" or "MainContext" in r["ctx"]:
        return "main"
    if r["tpe"] == "Sub" and "SubContext" in r["ctx"]:
        return "sub"
    if r["family"] == "core":
        return "pecore"
    if r["family"] == "sync":
        return "sync"
    return None


SMALL_DEFAULT_BYTES = 4096  # an unsized user load (a `to_dm_view` into a tile of an existing DmTensor: no sized tensor entry) is priced as this many bytes


def dma_class(r):
    tpe, b = r["tpe"], r["bytes"]
    if tpe == "DmaStore":
        return "store"
    if tpe == "DmaScatter":
        return "scatter"
    if tpe == "DmaStos" or (r["src_buffer"] and "Sram" in r["src_buffer"] and tpe in ("DmaLoad", "DmaGather")):
        return "dm2dm"
    if b is None:
        # the compiler's table staging is the DmaLoad WITHOUT a description (the empty-description DmaLoad + Sub rows per
        # fetch_table_lookup pass); a user `to_dm_view(.., buf.view_mut().tile::<..>(i))` has a description but no sized tensor entry
        # on either side (such operand loads would otherwise read as tables at 2,000 instead of small loads)
        return "table" if not r.get("call") else "small"
    if b >= STREAM_MIN:
        return "stream"
    if b >= SEED_MIN:
        return "seed"
    return "small"


def stream_rate(b, kernel, rates, n_streams, run_bytes=None):
    """B/cycle of a weight-class load: the one-command rate, else (when the kernel has a `stream_by_run` table and the row's DM-side run length is
    known) the rate of its RUN class, where the entry "size" sends whole-row runs to the by-size `stream` table; else the by-size table alone."""
    if n_streams == 1 and kernel in (rates.get("stream_one_command") or {}):
        return rates["stream_one_command"][kernel]
    by_run = (rates.get("stream_by_run") or {}).get(kernel)
    if by_run and run_bytes:
        for mn, rate in sorted(by_run, key=lambda x: -x[0]):
            if run_bytes >= mn:
                if rate != "size":
                    return rate
                break
    buckets = (rates.get("stream") or {}).get(kernel) or [[0, 614]]
    for mn, rate in sorted(buckets, key=lambda x: -x[0]):
        if b >= mn:
            return rate
    return buckets[-1][1]


def dma_duration(r, cls, kernel, rates, n_streams):
    b = r["bytes"] if r["bytes"] is not None else (int(rates.get("small_default_bytes", SMALL_DEFAULT_BYTES)) if cls == "small" else 0)
    if cls == "stream":
        rate = rates.get("stream_c0only", 337) if r.get("c0only") else stream_rate(b, kernel, rates, n_streams, r.get("run_bytes"))  # one cluster alone: the per-cluster cap
        return int(round(b / rate)), rate
    fixed, div = rates[cls]
    return int(round(fixed + (b / div if div else 0))), None


def factor(rates, key, kernel):
    v = rates.get(key, 2.0)
    return v.get(kernel, 2.0) if isinstance(v, dict) else v


# ---------------------------------------------------------------- the replay ------------------------------------------------

def retime(rows, kernel, rates=None, skip=frozenset()):
    """Replay the dispatch with re-timed durations. -> dict with begin/end/issue/dur/cls per pos, cons (constraints), makespan.
    skip = positions this cluster does not execute (S mode: cluster 1 skips the cluster-0-only rows); they become zero-length forwarders."""
    rates = rates or DEFAULT_RATES
    n_streams = sum(1 for r in rows if r["family"] == "dma" and r["tpe"] == "DmaLoad" and (r["bytes"] or 0) >= STREAM_MIN and r["pos"] not in skip)
    producer, fwd = {}, {}
    begin, end, issue, dur, cls, cons, rate = {}, {}, {}, {}, {}, {}, {}
    qfree = {"dma": 0, "main": 0, "sub": 0}
    qlast = {"dma": None, "main": None, "sub": None}
    issue_prev, issue_prev_pos, pecore_free, pecore_last, last_store = 0, None, 0, None, None

    def resolve(p):
        while p is not None and cls.get(p) == "noop":
            p = fwd.get(p)
        return p

    for r in rows:
        p, q = r["pos"], queue_of(r)
        deps = []
        for t in r["in"]:
            if t in producer:
                pp = resolve(producer[t])
                if pp is not None:
                    deps.append((pp, end[pp]))
        deps_done = max((t for _, t in deps), default=0)
        dep_pos = max(deps, key=lambda x: x[1])[0] if deps else None
        if q is None or p in skip:
            begin[p] = end[p] = deps_done
            issue[p] = None
            dur[p] = 0
            cls[p] = "noop"
            fwd[p] = dep_pos
            for t in r["out"]:
                producer[t] = p
            continue
        C = [(issue_prev_pos, issue_prev, "issue"), (pecore_last, pecore_free, "pecore")]
        DEPS = [(pp, t, "dep") for pp, t in deps] or [(None, 0, "dep")]
        if q == "sync" and r["kind"] == "DramReuse":  # region bookkeeping: no tensors, no queue; the row that needs the region carries the deps
            begin[p] = end[p] = issue_prev
            issue[p], dur[p], cls[p] = issue_prev, 0, "dramreuse"
            cons[p] = [(issue_prev_pos, issue_prev, "issue")]
            continue
        if q == "dma":
            c = dma_class(r)
            d, rt = dma_duration(r, c, kernel, rates, n_streams)
            C += DEPS
            iss = max(t for _, t, _ in C)
            C.append((qlast["dma"], qfree["dma"], "queue"))
            tb = max(iss, qfree["dma"])
            issue[p], begin[p], end[p], dur[p], cls[p], rate[p] = iss, tb, tb + d, d, c, rt
            qfree["dma"], qlast["dma"] = end[p], p
            if c in ("store", "scatter"):
                last_store = p
        elif q in ("main", "sub"):
            f = factor(rates, q, kernel)
            if q == "sub":
                f = ((rates.get("sub_kind") or {}).get(kernel) or {}).get(r["kind"], f)  # per-kind sub factors (ffn StoTab 5.1)
            d = int(round(r["static_dur"] * f))
            C += DEPS + [(qlast[q], qfree[q], "queue")]
            b0 = max(t for _, t, _ in C)
            corun = factor(rates, f"corun_{q}", kernel) if f"corun_{q}" in rates else (factor(rates, "corun", kernel) if "corun" in rates else 1.0)  # corun_main / corun_sub split the factor per context
            if corun != 1.0 and any(cls.get(s) == "stream" and begin[s] <= b0 < end[s] for s in cons):  # a pass under a stream: DM banks shared with the DMA
                d = int(round(d * corun))
            issue[p], begin[p], end[p], dur[p], cls[p] = b0, b0, b0 + d, d, q
            qfree[q], qlast[q] = end[p], p
        elif q == "pecore":
            d = int(round(r["static_dur"] * rates.get("core", 3.0)))
            C += DEPS
            b0 = max(t for _, t, _ in C)
            issue[p], begin[p], end[p], dur[p], cls[p] = b0, b0, b0 + d, d, "core"
            pecore_free, pecore_last = end[p], p
        else:  # ExplicitSync
            d = int(round(r["static_dur"] * rates.get("sync", 1.0)))
            C += DEPS + [(qlast["sub"], qfree["sub"], "subdrain")]
            if last_store is not None:
                C.append((last_store, end[last_store], "store"))
            b0 = max(t for _, t, _ in C)
            issue[p], begin[p], end[p], dur[p], cls[p] = b0, b0, b0 + d, d, "sync"
            pecore_free, pecore_last = end[p], p
        cons[p] = C
        issue_prev, issue_prev_pos = issue[p], p
        for t in r["out"]:
            producer[t] = p
    live = [p for p in cons if cls[p] != "dramreuse"]
    makespan = max((end[p] for p in live), default=0)
    return {"begin": begin, "end": end, "issue": issue, "dur": dur, "cls": cls, "cons": cons, "rate": rate, "makespan": makespan, "n_streams": n_streams}


def slacks(M):
    """Float of every row: how far its end may move before the makespan moves (reverse pass over the recorded constraints)."""
    begin, end, cons, mk = M["begin"], M["end"], M["cons"], M["makespan"]
    succ = {}
    for v, C in cons.items():
        for u, t, k in C:
            if u is not None and u in cons and k != "issue":  # an issue edge binds u's dispatch, not its end: no duration slack through it
                succ.setdefault(u, []).append((begin[v] - t, v))
    slack = {}
    for p in sorted(cons, reverse=True):  # every constraint points to an earlier pos
        s = mk - end[p] if M["cls"][p] != "dramreuse" else mk
        for es, v in succ.get(p, []):
            s = min(s, es + slack[v])
        slack[p] = s
    return slack


def critical_path(M):
    """The chain of binding constraints from the row that ends last back to t = 0 (pos list in time order)."""
    begin, end, cons, cls = M["begin"], M["end"], M["cons"], M["cls"]
    if not cons:
        return []
    p = max(cons, key=lambda q: (end[q], -begin[q]))
    path, seen = [], set()
    while p is not None and p not in seen:
        seen.add(p)
        path.append(p)
        b = begin[p]
        binding = [(PRI.get(k, 9), u) for u, t, k in cons[p] if u is not None and u in cons and t == b and t > 0]
        p = min(binding)[1] if binding else None
    return list(reversed(path))


def analyze(rows, kernel, rates=None):
    rates = rates or DEFAULT_RATES
    M = retime(rows, kernel, rates)
    sl = slacks(M)
    path = critical_path(M)
    byidx = {r["pos"]: r for r in rows}
    items, sh = [], Counter()
    for i, p in enumerate(path):
        r = byidx[p]
        nb = M["begin"][path[i + 1]] if i + 1 < len(path) else M["end"][p]
        own = max(0, min(M["end"][p], nb) - M["begin"][p])
        gap = max(0, nb - M["end"][p])
        sh[CLASS_GROUP.get(M["cls"][p], "other")] += own
        sh["wait"] += gap
        items.append({"pos": p, "index": r["index"], "class": M["cls"][p], "group": CLASS_GROUP.get(M["cls"][p], "other"), "kind": r["kind"], "ctx": r["ctx"],
                      "src": r["src"], "call": r["call"][:60], "static_begin": r["static_begin"], "static_dur": r["static_dur"],
                      "begin": M["begin"][p], "end": M["end"][p], "dur": M["dur"][p], "on_path": own, "gap_after": gap, "bytes": r["bytes"],
                      "rate": M["rate"].get(p), "slack": sl[p]})
    if path:
        sh["wait"] += M["begin"][path[0]]
    busy = Counter()
    for p, c in M["cls"].items():
        if c not in ("noop", "dramreuse"):
            busy[queue_of(byidx[p]) or c] += M["dur"][p]
    order = sorted(sh, key=lambda k: -sh[k])
    mk = M["makespan"]
    summary = " | ".join(f"{k} {100.0 * sh[k] / mk:.0f} %" for k in order if sh[k] and mk)
    static_mk = max((r["static_end"] for r in rows), default=0)
    return {"kernel": kernel, "static_makespan": static_mk, "realrate_makespan": mk, "ratio": round(mk / static_mk, 3) if static_mk else None,
            "n_streams": M["n_streams"], "busy": dict(busy), "shares": dict(sh), "summary": summary, "path": items,
            "rows": [{"pos": r["pos"], "index": r["index"], "class": M["cls"][r["pos"]], "kind": r["kind"], "src": r["src"], "begin": M["begin"][r["pos"]],
                      "end": M["end"][r["pos"]], "dur": M["dur"][r["pos"]], "slack": sl.get(r["pos"]), "bytes": r["bytes"], "static_dur": r["static_dur"]}
                     for r in rows if M["cls"][r["pos"]] not in ("noop", "dramreuse")]}


# ---------------------------------------------------------------- S mode: two clusters, a launch skew, rendezvous ----------------

S_MEDIAN = {K3[0]: 10500, K3[1]: 12500, K3[2]: 10300}  # per-kernel median launch skew (qkv 10.5k, attn_out 12.5k, ffn 10.3k)


def s_mode(rows, kernel, rates=None, s=None, skip_tu=False):
    """Two-cluster estimate for an S design. Cluster 0 runs the whole program (T0 = today's re-timing);
    cluster 1 starts S later and runs the same program WITHOUT the rows placed on `Cluster = m![1 # 2]`: the cluster-0-only DMA commands
    always (cluster 1 skips `1 # 2` DMAs), the cluster-0-only TU passes only with skip_tu (the same device record says cluster 1 executes them). At every
    ExplicitSync both clusters wait for each other and the later one's wait shifts its own remaining rows; the estimate is the later
    cluster's end. With one final rendezvous this is max(T0, S + T1) + tail, tail = the rows after it. -> dict(estimate, T0, T1, S, tail,
    binds, syncs[{index, arrive0, arrive1, leave}], skipped_dma, skipped_tu, t0_total (today's estimate))."""
    rates = rates or DEFAULT_RATES
    s = S_MEDIAN.get(kernel, 10500) if s is None else s
    M0 = retime(rows, kernel, rates)
    skip = {r["pos"] for r in rows if r["c0only"] and (r["family"] == "dma" or (skip_tu and r["family"] == "tu"))}
    n_dma = sum(1 for r in rows if r["c0only"] and r["family"] == "dma")
    n_tu = sum(1 for r in rows if r["c0only"] and r["family"] == "tu")
    M1 = retime(rows, kernel, rates, skip=frozenset(skip))
    syncs = [r["pos"] for r in rows if r["family"] == "sync" and r["kind"] == "ExplicitSync" and M0["cls"].get(r["pos"]) == "sync"]
    shift0 = shift1 = 0
    out_syncs = []
    for p in syncs:
        a0 = M0["end"][p] + shift0
        a1 = s + M1["end"][p] + shift1
        leave = max(a0, a1)
        out_syncs.append({"index": rows[p]["index"], "arrive0": a0, "arrive1": a1, "leave": leave, "wait0": leave - a0, "wait1": leave - a1})
        shift0 += leave - a0
        shift1 += leave - a1
    final0 = M0["makespan"] + shift0
    final1 = s + M1["makespan"] + shift1
    last = syncs[-1] if syncs else None
    T0 = M0["end"][last] if last is not None else M0["makespan"]
    T1 = M1["end"][last] if last is not None else M1["makespan"]
    tail = (M0["makespan"] - M0["end"][last]) if last is not None else 0
    if out_syncs:  # which cluster arrived later at the last rendezvous decides (a tie means cluster 0 waited: S + T1)
        binds = "S + T1" if out_syncs[-1]["arrive1"] >= out_syncs[-1]["arrive0"] else "T0"
    else:
        binds = "S + T1" if final1 >= final0 else "T0"
    return {"estimate": max(final0, final1), "T0": T0, "T1": T1, "S": s, "tail": tail, "binds": binds,
            "final0": final0, "final1": final1, "syncs": out_syncs, "skipped_dma": n_dma, "skipped_tu": n_tu if skip_tu else 0, "skip_tu": skip_tu,
            "t0_total": M0["makespan"], "formula": f"max(T0 {T0:,}, S {s:,} + T1 {T1:,}) + tail {tail:,}"}


def s_mode_dir(path, kernel, rates=None, s=None, skip_tu=False):
    sp = find_schedule(path, kernel)
    if not sp:
        return None
    rows, _ = load_rows(sp)
    return s_mode(rows, kernel, rates, s, skip_tu)


def s_line(Sd, ref=None):
    what = "cluster 1 skips 1 # 2 DMAs and passes" if Sd["skip_tu"] else "cluster 1 skips 1 # 2 DMAs, executes 1 # 2 passes"
    line = (f"S mode ({what}; {Sd['skipped_dma']} DMA(s) / {Sd['skipped_tu']} pass(es) skipped): estimate {fmt(Sd['estimate'])} = {Sd['formula']}, "
            f"{Sd['binds']} binds; cluster 0 ends {fmt(Sd['final0'])}, cluster 1 {fmt(Sd['final1'])}")
    if Sd["syncs"]:
        line += "; rendezvous " + ", ".join(f"{x['index']} arrive {fmt(x['arrive0'])} / {fmt(x['arrive1'])} leave {fmt(x['leave'])}" for x in Sd["syncs"])
    if ref:
        err = 100.0 * (Sd["estimate"] / ref["total"] - 1) if ref["total"] else float("nan")
        line += f" | device trace total {fmt(ref['total'])} at S {fmt(ref['S'])}: model {err:+.1f} %"
    return line


# ---------------------------------------------------------------- the trace side and the diff side ------------------------------

def trace_reference(timeline_path, kernel):
    rows, S = _cp.load_rows(timeline_path, kernel)
    if rows is None:
        return None
    A = _cp.analyze(rows, kernel)
    return {"timeline": timeline_path, "total": A["total"], "S": A["S"], "S_uncovered": A["S_uncovered"], "P": A["P"], "P_uncov": A["P_uncov"],
            "device_summary": A["summary"]}


def touched_lines(patch_text):
    """{file: [(first, last)]} of the + side of every hunk of a unified diff."""
    out, f = {}, None
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            f = line[4:].strip().split("\t")[0]
            f = f[2:] if f.startswith("b/") else f
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m and f:
            a, n = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(f, []).append((a, a + max(n, 1) - 1))
    return out


def touched_rows(A, patch_text, slack_lines=3):
    """Rows of the analysis whose source line lies in a touched hunk (+/- slack_lines), with their path status."""
    T = touched_lines(patch_text)
    hits = []
    for r in A["rows"]:
        if not r["src"]:
            continue
        m = re.match(r"^(.*?):(\d+)(?::\d+)?$", r["src"])
        if not m:
            continue
        f, ln = m.group(1), int(m.group(2))
        for tf, ranges in T.items():
            if f.endswith(tf) or tf.endswith(f):
                if any(a - slack_lines <= ln <= b + slack_lines for a, b in ranges):
                    hits.append(r)
                    break
    on = [r for r in hits if r["slack"] == 0]
    return {"n": len(hits), "on_path": [{"index": r["index"], "class": r["class"], "src": r["src"], "dur": r["dur"]} for r in on],
            "off_path": [{"index": r["index"], "class": r["class"], "src": r["src"], "dur": r["dur"], "slack": r["slack"]} for r in hits if r["slack"] != 0],
            "verdict": ("on-path: " + ", ".join(sorted({r["class"] for r in on})) if on else
                        (f"off-path (min slack {min(r['slack'] for r in hits):,})" if hits else "no schedule row on the touched lines"))}


def fmt(v):
    return "-" if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def trace_bin_ok(path):
    """The binary-identity rule for the re-fit: a trace DIR must carry result.json bin_sha1 (a trace built before the fresh-source fix read an older
    binary's numbers). -> (ok, why). A bare timeline.json file cannot be checked: ok with a note."""
    if not os.path.isdir(path): return True, "timeline file: no binary identity to check"
    for cand in (os.path.join(path, "result.json"), os.path.join(os.path.dirname(path.rstrip("/")), "result.json")):
        if os.path.exists(cand):
            try: r = json.load(open(cand))
            except Exception: return False, f"{cand} unreadable"
            return (True, f"bin_sha1 {str(r.get('bin_sha1'))[:12]}") if r.get("bin_sha1") else (False, f"{cand} has no bin_sha1 (built before the A8 fix or outside ec2_run)")
    return False, f"no result.json under {path}"


def rates_from_timeline(timeline_path, kernel):
    """A DEFAULT_RATES block from the streams on a trace's device critical path (critical_path.py "streams on the chain"): the stream rate per size
    class = the median B/cycle of the chain's weight streams in that class (ffn keeps the four size buckets; qkv / attn_out one bucket),
    `stream_one_command` when the kernel's chain carries exactly one stream >= 8 MB (the attn_out one-command Wo). Every stream is listed so the
    reader can see what the median hides; the small / seed / store / main / sub entries are not fitted here (too few rows per trace) and stay as
    DEFAULT_RATES says. The block is a PROPOSAL for the main session: a rate change re-prices every estimate (decision constant, held)."""
    rows, _ = _cp.load_rows(timeline_path, kernel)
    if rows is None:
        return None
    A = _cp.analyze(rows, kernel)
    st = [x for x in A["chain"] if x["class"] == "stream" and x.get("bytes") and x.get("b_per_cycle")]
    lines = [f"{x['instr']} {x['bytes']:,} B at {x['b_per_cycle']} B/cycle, issued {x['issue']:,}, lands {x['end']:,}, {x.get('tu_under')} TU rows under it" for x in st]
    import statistics
    out = {"stream": {}, "_streams_on_the_chain": lines, "_from": timeline_path, "_device_critical": A["summary"]}
    if not st:
        return out
    if kernel == K3[2]:
        buckets = [(16_000_000, None), (8_000_000, None), (3_000_000, None), (0, None)]
        table = []
        for i, (mn, _) in enumerate(buckets):
            mx = buckets[i - 1][0] if i else float("inf")
            rs = [x["b_per_cycle"] for x in st if mn <= x["bytes"] < mx]
            table.append([mn, int(round(statistics.median(rs))) if rs else None])
        default = dict((m, r) for m, r in DEFAULT_RATES["stream"][kernel])
        out["stream"][kernel] = [[mn, r if r is not None else default.get(mn)] for mn, r in table]
        out["_note"] = "ffn buckets without a stream on this chain keep the DEFAULT_RATES value"
    else:
        out["stream"][kernel] = [[0, int(round(statistics.median([x["b_per_cycle"] for x in st])))]]
    big = [x for x in st if x["bytes"] >= 8_000_000]
    if kernel == K3[1] and len(big) == 1:
        out["stream_one_command"] = {kernel: int(round(big[0]["b_per_cycle"]))}
    rs = [x["b_per_cycle"] for x in st]
    if len(rs) > 1 and max(rs) > 1.15 * min(rs):  # a probe of several forms (maps, cluster-0-only commands): the median is not one rate
        out["_warning"] = f"stream rates spread {min(rs):.0f}..{max(rs):.0f} B/cycle (> 15 %): the commands are different forms; read _streams_on_the_chain per command, do not paste the median into DEFAULT_RATES"
    return out


def compare_line(A, ref):
    s = f"static {fmt(A['static_makespan'])} -> real-rate {fmt(A['realrate_makespan'])} (x{A['ratio']})"
    if ref:
        err = 100.0 * (A["realrate_makespan"] / ref["P_uncov"] - 1) if ref["P_uncov"] else float("nan")
        s += (f" (device trace {fmt(ref['total'])}; S {fmt(ref['S'])}, uncovered {fmt(ref['S_uncovered'])}; total - S {fmt(ref['P'])}; "
              f"total - S_uncov {fmt(ref['P_uncov'])}: model {err:+.1f} %)")
    return s


def render(A, ref=None, parent=None, touch=None, top=None):
    L = [f"{A['kernel']}: {compare_line(A, ref)}"]
    if parent:
        d = A["realrate_makespan"] - parent["realrate_makespan"]
        L.append(f"parent: static {fmt(parent['static_makespan'])} -> real-rate {fmt(parent['realrate_makespan'])}; delta real-rate {d:+,} "
                 f"({100.0 * d / parent['realrate_makespan']:+.2f} %), static {A['static_makespan'] - parent['static_makespan']:+,}")
    L.append("re-timed busy: " + ", ".join(f"{k} {fmt(v)} ({100.0 * v / A['realrate_makespan']:.0f} %)" for k, v in sorted(A["busy"].items(), key=lambda kv: -kv[1])))
    L.append(f"critical (re-timed, {len(A['path'])} rows): {A['summary']}")
    if ref:
        L.append(f"critical (device trace):  {ref['device_summary']}")
    L.append(f"{'begin':>8} {'end':>8} {'on path':>8} {'class':9} {'index':>17} {'kind':12} {'static':>7} {'bytes':>10} {'rate':>5} src / call")
    items = A["path"] if top is None else sorted(A["path"], key=lambda x: -x["on_path"])[:top]
    for x in items:
        L.append(f"{x['begin']:>8,} {x['end']:>8,} {x['on_path']:>8,} {x['class']:9} {x['index']:>17} {str(x['kind'])[:12]:12} {x['static_dur']:>7,} "
                 f"{fmt(x['bytes']):>10} {fmt(x['rate']):>5} {(x['src'] or '').split('/')[-1]} {x['call']}" + (f"  [+{x['gap_after']:,} wait]" if x["gap_after"] else ""))
    if touch:
        L.append(f"touched rows ({touch['n']}): {touch['verdict']}" + ("; on path: " + ", ".join(f"{r['index']} {r['class']} {(r['src'] or '').split('/')[-1]}" for r in touch["on_path"][:8]) if touch["on_path"] else "")
                 + ("; off path: " + ", ".join(f"{r['index']} {r['class']} slack {r['slack']:,}" for r in touch["off_path"][:6]) if touch["off_path"] else ""))
    return "\n".join(L)


def run_dir(path, kernel, rates):
    sp = find_schedule(path, kernel)
    if not sp:
        return None, None, None
    rows, _ = load_rows(sp)
    A = analyze(rows, kernel, rates)
    tl = find_trace(path)
    ref = trace_reference(tl, kernel) if tl else None
    return A, ref, sp


# ---------------------------------------------------------------- selftest -------------------------------------------------

def I(index, tpe, ctx, b, e, ins, outs, desc=""):
    return {"index": index, "tpe": tpe, "contexts": [ctx], "lifetime": {"begin": b, "end": e}, "input_tensors": ins, "output_tensors": outs, "description": desc}


def synthetic_schedule(wq_bytes=15_728_640, norm_static=3000, c0_extra=False):
    """A qkv-shaped kernel: seed load -> (norm chain on main | Wq stream) -> contraction (needs both) -> scale load queued behind Wq ->
    epilogue -> scatter -> ExplicitSync. Statically the norm chain (3,000) outlives Wq (here 1,000); re-timed, Wq does.
    c0_extra: an S-hiding form: the shared weight command shrinks to 75 % for both clusters and the other 25 % (3.93 MB)
    streams as a `Cluster = m![1 # 2]` command that only cluster 0 executes, with its own pass and store; the epilogue does not wait for it."""
    T = [{"index": "1", "size": 245760, "shape": "MappingShape[Chip: 1, Cluster: Ns / 4 % 2, Slice: Dummy256 / 16, Element: H]", "buffer_type": "Sram"},
         {"index": "2", "size": (wq_bytes * 3 // 4) if c0_extra else wq_bytes, "shape": "MappingShape[Chip: 1, Cluster: Qs / 1920 % 2, Slice: Qs % 8, Element: H]", "buffer_type": "Sram"},
         {"index": "3", "size": 4096, "shape": "MappingShape[Chip: 1, Cluster: E / 2048 % 2, Slice: E % 8, Element: 1]", "buffer_type": "Sram"},
         {"index": "4", "size": 8}, {"index": "5", "size": 8}, {"index": "6", "size": 8}, {"index": "7", "size": 8}, {"index": "8", "size": 8},
         {"index": "9", "size": wq_bytes // 4, "shape": "MappingShape[Chip: 1, Cluster: 1 # 2, Slice: Ps / 32 % 16, Element: Ps % 8, H]", "buffer_type": "Sram"},
         {"index": "10", "size": 8, "shape": "MappingShape[Chip: 1, Cluster: 1 # 2, Slice: Ps / 8 % 32, Element: Ps % 8]", "buffer_type": "Sram"},
         {"index": "11", "size": 7680, "shape": "MappingShape[Chip: 1, Element: Ps]", "buffer_type": "Dram"}]
    ins = [I("0", "Sub", "Noop", 0, 0, [], [1], "DmTensor::new()"),
           I("6", "DmaLoad", "DmaEngine", 1000, 2135, [100], [1], "--> src/device/layout.rs:254:81\n\nx.to_dm(&mut ctx.tdma)"),
           I("44", "Main", "MainContext", 2135, 2135 + norm_static, [1], [4], "--> src/device/layout.rs:195:77\n\n.commit()"),
           I("50", "Core", "PeCore", 2135, 2145, [1], [], "")]
    if c0_extra:  # the S-hiding shape: the cluster-0-only extra command is issued FIRST, in front of the shared stream (cluster 1 skips it)
        ins += [I("56", "DmaLoad", "DmaEngine", 2135, 2400, [104], [9], "--> src/device/sliding/projection.rs:300:5\n\nk_extra.to_dm(&mut ctx.tdma)")]
    ins += [I("58", "DmaLoad", "DmaEngine", 2400, 3135, [101], [2], "--> src/device/sliding/projection.rs:90:91\n\nq_weight.to_dm(&mut ctx.tdma)"),
            I("156", "DmaLoad", "DmaEngine", 3135, 3690, [102], [3], "--> src/device/sliding/projection.rs:84:82\n\nv_weight_scale.to_dm(&mut ctx.tdma)"),
            I("177", "Main", "MainContext", 5135, 6379, [2, 4], [5], "--> src/device/sliding/projection.rs:243:5\n\n.commit()")]
    if c0_extra:
        ins += [I("190", "Main", "MainContext", 6379, 6900, [9, 4], [10], "--> src/device/sliding/projection.rs:320:5\n\n.commit()")]
    ins += [I("179", "Sub", "SubContext", 6379, 6646, [3], [6], "--> src/device/sliding/projection.rs:169:75\n\n.to_vrf()"),
            I("204", "Main", "MainContext", 6646, 7423, [5, 6], [7], "--> src/device/sliding/projection.rs:272:5\n\n.commit()")]
    if c0_extra:
        ins += [I("300", "DmaStore", "DmaEngine", 7423, 7500, [10], [11], "--> src/device/sliding/projection.rs:330:5\n\nk_extra_out.to_hbm(&mut ctx.tdma)")]
    ins += [I("405", "DmaScatter", "DmaEngine", 7423, 8352, [7, 103], [8], "--> src/ops.rs:134:5\n\nv.dma_scatter::<m![1], _, _>(kv_offset, v_cache)"),
            I("ExplicitSync(153)", "Core", "PeCore", 8352, 8952, [8] + ([11] if c0_extra else []), [], "")]
    return {"instructions": ins, "tensors": T}


def selftest():
    t = tempfile.mkdtemp()
    sp = os.path.join(t, "schedule", K3[0] + ".json")
    os.makedirs(os.path.dirname(sp))
    json.dump(synthetic_schedule(), open(sp, "w"))
    rows, mk = load_rows(sp)
    assert mk == 8952 and [r["pos"] for r in rows] == list(range(11))
    A = analyze(rows, K3[0])
    P = {x["index"]: x for x in A["path"]}
    R = {x["index"]: x for x in A["rows"]}
    # durations by class: seed 800 + 245,760 / 220 = 1,917; Wq 15,728,640 / 614 = 25,617; small 1,200 + 4,096 / 150 = 1,227; main x2.0; sub x1.9; scatter 1,600; sync 600
    assert R["6"]["dur"] == 1917 and R["58"]["dur"] == 25617 and R["156"]["dur"] == 1227 and R["44"]["dur"] == 6000 and R["179"]["dur"] == round(267 * 1.9), R
    assert R["405"]["dur"] == 1600 and R["ExplicitSync(153)"]["dur"] == 600 and R["50"]["dur"] == 30, R
    # dispatch: the seed lands at 1,917; the norm pass and the view program and Wq are dispatched then; Wq's transfer starts at the seed's completion
    assert R["6"]["begin"] == 0 and R["44"]["begin"] == 1917 and R["58"]["begin"] == 1917 + 30 and R["58"]["end"] == 1947 + 25617, (R["6"], R["44"], R["58"])
    # the scale load queues behind Wq (FIFO); the contraction waits for Wq (dep), not for the norm chain (which ends at 7,917)
    assert R["156"]["begin"] == R["58"]["end"] and R["177"]["begin"] == R["58"]["end"], (R["156"], R["177"])
    assert R["179"]["begin"] == R["156"]["end"] and R["204"]["begin"] == max(R["177"]["end"], R["179"]["end"]) and R["405"]["begin"] == R["204"]["end"]
    assert R["ExplicitSync(153)"]["begin"] == R["405"]["end"] and A["realrate_makespan"] == R["ExplicitSync(153)"]["end"], A["realrate_makespan"]
    # the critical path is seed -> view program -> Wq -> contraction -> pass -> scatter -> sync: the norm chain 44 and the scale load 156 /
    # sub lift 179 have slack (the norm chain would be the STATIC critical path: 3,000 static against Wq's 1,000)
    assert list(P) == ["6", "50", "58", "177", "204", "405", "ExplicitSync(153)"], list(P)
    assert R["44"]["slack"] > 0 and R["179"]["slack"] == R["204"]["begin"] - R["179"]["end"] and R["156"]["slack"] == R["179"]["slack"], (R["44"], R["179"], R["156"])
    assert all(x["slack"] == 0 for x in A["path"]) and R["177"]["slack"] == 0
    assert sum(A["shares"].values()) == A["realrate_makespan"] and A["shares"]["stream"] == 25617 and A["shares"]["wait"] == 0 and A["shares"]["core"] == 30, A["shares"]
    assert A["summary"].startswith("stream ") and A["static_makespan"] == 8952 and A["busy"]["dma"] == 1917 + 25617 + 1227 + 1600, A
    # a shorter norm chain (the R1 kind of cut) leaves the re-timed makespan unchanged although the static makespan shrinks
    json.dump(synthetic_schedule(norm_static=1000), open(sp, "w"))
    rows2, mk2 = load_rows(sp)
    A2 = analyze(rows2, K3[0])
    assert {r["index"]: r for r in rows2}["44"]["static_dur"] == 1000 and A2["realrate_makespan"] == A["realrate_makespan"], (A2["realrate_makespan"], A["realrate_makespan"])
    # an unsized user load (to_dm_view into a tile: a described DmaLoad with no sized tensor) is a small load, not a table staging
    unsized = dict(rows[5], bytes=None, call=".to_dm_view(&mut ctx.tdma, operands.view_mut().tile::<m![Dummy8], 1, m![H]>(0))", src_buffer=None)
    assert dma_class(unsized) == "small" and dma_duration(unsized, "small", K3[0], DEFAULT_RATES, 3)[0] == 1200 + SMALL_DEFAULT_BYTES // 150, dma_duration(unsized, "small", K3[0], DEFAULT_RATES, 3)
    assert dma_class(dict(unsized, call="")) == "table" and dma_duration(dict(unsized, call=""), "table", K3[0], DEFAULT_RATES, 3)[0] == 2000  # the compiler's empty-description staging
    assert dma_duration(unsized, "small", K3[0], merge_rates({"small_default_bytes": 7680}), 3)[0] == 1200 + 7680 // 150
    # corun_main / corun_sub split the co-running factor per context (a sub lift under a stream vs a main pass under it)
    A5 = analyze(rows, K3[0], merge_rates({"corun_main": {K3[0]: 1.0}, "corun_sub": {K3[0]: 3.0}}))
    R5 = {x["index"]: x for x in A5["rows"]}
    assert R5["44"]["dur"] == 6000 and R5["179"]["dur"] == round(267 * 1.9), (R5["44"], R5["179"])  # main row 44 is dispatched before Wq's transfer, sub row 179 after Wq landed: neither co-runs
    # run-length pricing (ffn): the run bytes come from the tensor shape, whole rows take the by-size table, short runs their own rates; qkv / attn_out unchanged
    T = {1: {"size": 5_898_240, "shape": "MappingShape[Chip: 1, Cluster: H / 1920 % 2, Slice: (H / 60 % 64, 1 # 4), Element: (H / 12 % 5 = 1, H / 4 % 3, L % 7680)]"},  # down tile (f4)
         2: {"size": 3_686_400, "shape": "MappingShape[Chip: 1, Cluster: L / 7680 % 2, Slice: (L / 60 % 128, 1 # 2), Element: (L % 30, H / 16)]"},           # up / gate scale
         3: {"size": 2_211_840, "shape": "MappingShape[Chip: 1, Cluster: H / 1920 % 2, Slice: (H / 60 % 64, 1 # 4), Element: (H / 12 % 5 = 3, H / 4 % 3, L / 16 % 480)]"},  # staged down scale
         4: {"size": 2_211_840, "shape": "MappingShape[Chip: 1, Cluster: H / 1920 % 2, Slice: (H / 60 % 32, 1 # 8), Element: (H / 12 % 5 = 3, H % 12, L / 16 % 120)]"},   # direct down scale
         5: {"size": 29_491_200, "shape": "MappingShape[Chip: 1, Cluster: L / 7680 % 2, Slice: (L / 60 % 128, 1 # 2), Element: (L / 4 % 15, L % 2, H)]"},      # m1 (f4 rows)
         6: {"size": 15_728_640, "shape": "MappingShape[Chip: 1, Cluster: Qs / 2048 % 2, Slice: (Qs / 64 % 32, Qs % 8), Element: (Qs / 8 % 8, H)]"}, 7: {"size": 8}}  # qkv Wq (f8 rows)
    assert [run_bytes_of(T, [i]) for i in (1, 2, 3, 4, 5, 6, 7)] == [3840, 240, 480, 120, 1920, 3840, None], [run_bytes_of(T, [i]) for i in (1, 2, 3, 4, 5, 6, 7)]
    assert digit_extent("1 # 2") == 2 and digit_extent("H / 12 % 5 = 3") == 3 and digit_extent("Dummy256 / 16") == 16 and digit_extent("foo") is None
    F = K3[2]
    assert stream_rate(5_898_240, F, DEFAULT_RATES, 5, 3840) == 480 and stream_rate(17_694_720, F, DEFAULT_RATES, 5, 3840) == 625 and stream_rate(29_491_200, F, DEFAULT_RATES, 5, 1920) == 625
    assert stream_rate(3_686_400, F, DEFAULT_RATES, 5, 240) == 340 and stream_rate(2_211_840, F, DEFAULT_RATES, 5, 480) == 320 and stream_rate(2_211_840, F, DEFAULT_RATES, 5, 120) == 165
    assert stream_rate(2_211_840, F, DEFAULT_RATES, 5, None) == 450 and stream_rate(15_728_640, K3[0], DEFAULT_RATES, 3, 3840) == 614  # unknown run: by size; qkv has no run table
    # the per-kind sub factor: an ffn StoTab row (kind sub_tab, an empty-description Sub row) at 5.1x, other sub rows at the class factor
    tab = {"pos": 0, "index": "t", "tpe": "Sub", "family": "tu", "kind": "sub_tab", "ctx": "SubContext", "src": None, "call": "", "static_begin": 0, "static_end": 1000, "static_dur": 1000, "in": [], "out": [1], "bytes": None}
    vrf = dict(tab, pos=1, index="v", kind="sub_vrf", call=".to_vrf()", out=[2])
    Mk = retime([dict(tab, in_=[]) if False else tab, vrf], F)
    assert Mk["dur"][0] == 4000 and Mk["dur"][1] == 2200, (Mk["dur"][0], Mk["dur"][1])
    # a rates override and the one-command rule
    A3 = analyze(rows, K3[0], merge_rates({"stream": {K3[0]: [[0, 700]]}}))
    assert {x["index"]: x for x in A3["rows"]}["58"]["dur"] == round(15728640 / 700), A3["rows"]
    A4 = analyze(rows, K3[1])
    assert {x["index"]: x for x in A4["rows"]}["58"]["dur"] == round(15728640 / 690) and A4["n_streams"] == 1, A4["n_streams"]
    # --diff: a hunk on projection.rs:243 (the contraction, off path) and one on projection.rs:90 (Wq, on path)
    patch = "--- a/src/device/sliding/projection.rs\n+++ b/src/device/sliding/projection.rs\n@@ -240,6 +240,7 @@ x\n+y\n@@ -88,3 +89,4 @@ x\n+z\n"
    tch = touched_rows(A, patch)
    assert tch["n"] == 2 and [r["index"] for r in tch["on_path"]] == ["58", "177"] and tch["off_path"] == [] and tch["verdict"] == "on-path: main, stream", tch
    tch2 = touched_rows(A, "--- a/src/device/layout.rs\n+++ b/src/device/layout.rs\n@@ -190,6 +190,8 @@ x\n")  # the norm pass 44: off the re-timed path
    assert tch2["n"] == 1 and tch2["on_path"] == [] and tch2["verdict"].startswith("off-path (min slack "), tch2
    assert touched_rows(A, "--- a/src/x.rs\n+++ b/src/x.rs\n@@ -1,2 +1,2 @@\n")["verdict"] == "no schedule row on the touched lines"
    txt = render(A, None, A2, tch)
    assert "static 8,952 -> real-rate" in txt and "delta real-rate +0" in txt and "touched rows (2): on-path: main, stream" in txt, txt
    # run_dir on a dir without a trace
    Ad, ref, spath = run_dir(t, K3[0], DEFAULT_RATES)
    assert spath == sp and ref is None and Ad["realrate_makespan"] == A2["realrate_makespan"]
    # S mode: the symmetric schedule has no `1 # 2` row, so T1 = T0 and the estimate is S + T0 (the sync at the end, no tail)
    rows0, _ = load_rows(sp)
    assert not any(r["c0only"] for r in rows0) and {r["cluster"] for r in rows0 if r["family"] == "dma"} >= {"Ns / 4 % 2", "Qs / 1920 % 2"}, [(r["index"], r["cluster"]) for r in rows0 if r["family"] == "dma"]
    S0 = s_mode(rows0, K3[0], s=7949)
    assert S0["T0"] == S0["T1"] == S0["t0_total"] and S0["tail"] == 0 and S0["estimate"] == 7949 + S0["t0_total"] and S0["binds"] == "S + T1" and S0["skipped_dma"] == 0, S0
    assert len(S0["syncs"]) == 1 and S0["syncs"][0]["wait0"] == 7949 and S0["syncs"][0]["wait1"] == 0, S0["syncs"]
    # the S-hiding form: a 3.93 MB command + its pass on `Cluster = m![1 # 2]`: cluster 0 gets longer (T0 up), cluster 1 skips the command (T1 shorter)
    json.dump(synthetic_schedule(c0_extra=True), open(sp, "w"))
    rows1, _ = load_rows(sp)
    assert [r["index"] for r in rows1 if r["c0only"]] == ["56", "190", "300"] and sum(r["c0only"] and r["family"] == "dma" for r in rows1) == 2, [r["index"] for r in rows1 if r["c0only"]]
    S1 = s_mode(rows1, K3[0], s=10500)                    # default: cluster 1 executes the `1 # 2` pass, skips the DMAs
    S1b = s_mode(rows1, K3[0], s=10500, skip_tu=True)     # the literal spec: skips the pass as well
    assert S1["T1"] < S1["T0"] and S1b["T1"] < S1["T1"] and S1["skipped_dma"] == 2 and S1["skipped_tu"] == 0 and S1b["skipped_tu"] == 1, (S1["T0"], S1["T1"], S1b["T1"])
    assert S1["T1"] < S0["T1"], (S1["T1"], S0["T1"])  # cluster 1 streams 75 % of Wq and skips the extra command: its program is shorter than the symmetric one
    ex = [r for r in rows1 if r["index"] == "56"][0]
    assert ex["bytes"] == 1_966_080 and dma_duration(ex, "stream", K3[0], DEFAULT_RATES, 2) == (round(1_966_080 / 337), 337), dma_duration(ex, "stream", K3[0], DEFAULT_RATES, 2)  # live bytes of the `1 # 2` command at the per-cluster cap
    xfer = round(1_966_080 / 337)
    assert xfer <= S1["T0"] - S1["T1"] <= xfer + 3000, (S1["T0"], S1["T1"], xfer)  # cluster 0 alone pays the extra transfer (and its store)
    assert abs(S1["T0"] - S0["T0"]) < 3000, (S1["T0"], S0["T0"])  # cluster 0 moves the same live bytes (75 % shared at the chip rate + 25 % alone at the cap): about the symmetric length
    assert S1["estimate"] == max(S1["final0"], S1["final1"]) and S1["formula"].startswith("max(T0 ")
    big = s_mode(rows1, K3[0], s=40000); small = s_mode(rows1, K3[0], s=0)
    assert big["binds"] == "S + T1" and big["estimate"] == 40000 + big["T1"] + big["tail"] and small["binds"] == "T0" and small["estimate"] == small["T0"] + small["tail"], (big["binds"], small["binds"])
    # hiding S pays at a large S (cluster 1's shorter program ends first); at S 0 the form is about neutral in the model (the device's pad streaming and
    # DMA-issue holes of such forms are not in it); the median sits between
    assert big["estimate"] < s_mode(rows0, K3[0], s=40000)["estimate"] - 3000 and abs(small["estimate"] - s_mode(rows0, K3[0], s=0)["estimate"]) < 3000, (big["estimate"], small["estimate"])
    assert small["estimate"] < S1["estimate"] <= big["estimate"] and "S mode" in s_line(S1) and "binds" in s_line(S1)
    json.dump(synthetic_schedule(), open(sp, "w"))
    import tempfile as _tf
    _td = _tf.mkdtemp(); assert trace_bin_ok(_td)[0] is False and "no result.json" in trace_bin_ok(_td)[1]
    json.dump({"arena_job": 71895}, open(os.path.join(_td, "result.json"), "w")); assert trace_bin_ok(_td)[0] is False and "no bin_sha1" in trace_bin_ok(_td)[1]
    json.dump({"arena_job": 71999, "bin_sha1": "c" * 40}, open(os.path.join(_td, "result.json"), "w")); assert trace_bin_ok(_td) == (True, "bin_sha1 cccccccccccc") and trace_bin_ok(os.path.join(_td, "result.json"))[0] is True
    # --rates-from on critical_path's synthetic timelines: the qkv chain has one 15.7 MB stream at 614 B/cycle; the attn_out one two Wo halves (no one-command rate)
    tl = os.path.join(t, "spans", "timeline.json"); os.makedirs(os.path.dirname(tl))
    syn = _cp.synthetic(); ao = [_cp.R(0, "dma", "DmaLoad", 100, 1700, "4", "DmaLoad", 262144, 1.8), _cp.R(1, "dma", "DmaLoad", 1710, 14000, "21", "DmaLoad", 7864320, 1.7),
                                _cp.R(2, "dma", "DmaLoad", 14300, 27600, "33", "DmaLoad", 7864320, 1.9), _cp.R(3, "tu", "main", 27610, 30000, "41", "Main", None, 2.8, "TuExec")]
    json.dump({"kernels": {K3[0]: {"rows": syn, "summary": {}}, K3[1]: {"rows": ao, "summary": {}}}}, open(tl, "w"))
    RF = rates_from_timeline(tl, K3[0])
    assert RF["stream"][K3[0]] == [[0, 614]] and len(RF["_streams_on_the_chain"]) == 1 and "58 15,728,640 B at 614.4 B/cycle" in RF["_streams_on_the_chain"][0] and "stream_one_command" not in RF, RF
    RA = rates_from_timeline(tl, K3[1])
    assert RA["stream"][K3[1]] == [[0, round((7864320 / 12290 + 7864320 / 13300) / 2)]] or RA["stream"][K3[1]][0][0] == 0, RA
    assert "stream_one_command" not in RA and len(RA["_streams_on_the_chain"]) == 2, RA  # two halves: no one-command rate
    assert rates_from_timeline(tl, K3[2]) is None  # ffn not in this timeline
    # calibration on the mirrored head traces, when present
    out = []
    for k, d in CALIB.items():
        path = os.path.join(MIRROR, d)
        if not find_schedule(path, k) or not find_trace(path):
            continue
        Ak, refk, _ = run_dir(path, k, DEFAULT_RATES)
        err = 100.0 * (Ak["realrate_makespan"] / refk["P_uncov"] - 1)
        out.append(f"{SHORT_INV[k]}: static {Ak['static_makespan']:,} -> real-rate {Ak['realrate_makespan']:,} vs total - S_uncov {refk['P_uncov']:,} "
                   f"(trace {refk['total']:,}, S {refk['S']:,}, uncovered {refk['S_uncovered']:,}): {err:+.1f} %; critical {Ak['summary']}")
        assert abs(err) <= CALIB_TOL_PCT, out[-1]
    print("measured_rate_estimate.py selftest ok")
    for line in out:
        print("  " + line)
    return 0


SHORT_INV = {v: k for k, v in SHORT.items()}


def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("path", nargs="?")
    ap.add_argument("kernel", nargs="?")
    ap.add_argument("--rates")
    ap.add_argument("--parent")
    ap.add_argument("--diff")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int)
    ap.add_argument("--rates-from", metavar="TIMELINE", help="print a DEFAULT_RATES block from the streams on this trace's device chain (usage: --rates-from <timeline.json | trace dir> <kernel>)")
    ap.add_argument("--allow-no-bin", action="store_true", help="--rates-from: accept a trace dir whose result.json has no bin_sha1 (traces from before the binary-identity rule; diagnosis only)")
    ap.add_argument("--s-design", action="store_true", help="S mode: estimate = the later cluster's end with a launch skew S (cluster 1 skips the `Cluster = m![1 # 2]` rows)")
    ap.add_argument("--s-median", type=int, help="the launch skew S to use (default per kernel: S_MEDIAN)")
    ap.add_argument("--s-skip", choices=["dma", "dma+tu"], default="dma", help="what cluster 1 skips: the 1 # 2 DMA commands (default) or the DMAs and the 1 # 2 TU passes")
    a = ap.parse_args()
    if a.rates_from:
        k = kernel_name(a.kernel or a.path or "")
        okb, whyb = trace_bin_ok(a.rates_from)
        if not okb and not a.allow_no_bin: print(f"measured_rate_estimate --rates-from REFUSED: {whyb} (A8: a trace whose binary identity is unknown never re-fits the rates; --allow-no-bin overrides)", file=sys.stderr); sys.exit(3)
        tl = _cp.find_timeline(a.rates_from)
        if not tl or k not in K3:
            print("measured_rate_estimate.py --rates-from <timeline.json | trace dir> <kernel>", file=sys.stderr)
            return 2
        R = rates_from_timeline(tl, k)
        if R is None:
            print(f"measured_rate_estimate.py: kernel {k} not in {tl}", file=sys.stderr)
            return 2
        print(json.dumps(R, indent=1))
        return 0
    if not a.path or not a.kernel:
        print(__doc__.split("\n\n")[0] + "\n" + "\n".join(__doc__.split("\n")[2:5]), file=sys.stderr)
        return 2
    k = kernel_name(a.kernel)
    rates = merge_rates(json.load(open(a.rates))) if a.rates else DEFAULT_RATES
    A, ref, sp = run_dir(a.path, k, rates)
    if A is None:
        print(f"measured_rate_estimate.py: no schedule for {k} under {a.path}", file=sys.stderr)
        return 2
    parent = run_dir(a.parent, k, rates)[0] if a.parent else None
    if a.parent and parent is None:
        print(f"measured_rate_estimate.py: no parent schedule for {k} under {a.parent}", file=sys.stderr)
        return 2
    touch = touched_rows(A, open(a.diff).read()) if a.diff else None
    Sd = Sp = None
    if a.s_design:
        Sd = s_mode_dir(a.path, k, rates, a.s_median, a.s_skip == "dma+tu")
        Sp = s_mode_dir(a.parent, k, rates, a.s_median, a.s_skip == "dma+tu") if a.parent else None
    if a.json:
        out = {"schedule": sp, "analysis": {kk: v for kk, v in A.items() if kk != "rows"}, "trace": ref,
               "parent": ({kk: v for kk, v in parent.items() if kk not in ("rows", "path")} if parent else None), "touched": touch,
               "s_mode": Sd, "parent_s_mode": Sp}
        print(json.dumps(out, indent=1))
    else:
        print(f"schedule {sp}" + (f"; trace {ref['timeline']}" if ref else ""))
        print(render(A, ref, parent, touch, a.top))
        if Sd:
            print(s_line(Sd, ref))
            if Sp:
                print(f"S mode parent: estimate {fmt(Sp['estimate'])} = {Sp['formula']}, {Sp['binds']} binds; delta S mode {100.0 * (Sd['estimate'] / Sp['estimate'] - 1):+.2f} % "
                      f"(T0-only delta {100.0 * (A['realrate_makespan'] / parent['realrate_makespan'] - 1):+.2f} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
