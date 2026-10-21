#!/usr/bin/env python3
"""Device critical path of one kernel from a recorded trace timeline.

  python3 profiler/critical_path.py <trace dir | iteration dir | timeline.json> <kernel> [--tol 64] [--json] [--top N]
  python3 profiler/critical_path.py --selftest

kernel = qkv | attn_out | ffn | the full test name. The input is a profiler/device_timeline.py timeline.json (`kernels[<kernel>].rows` with
real_begin / real_end / family / kind / real_kind / instr / bytes / ratio), or a dir that holds one under spans/ or trace/spans/
(an `ec2_run.py --out` dir, an iterations/<n>/ dir, a candidate's .run/trace).

Chain rule: walk BACKWARD from the row that ends last. The predecessor of a row is the row that ended when it started
(|pred end - begin| <= --tol cycles, default 64): for a queued DMA its FIFO predecessor (a DMA span begins at dispatch and its
transfer starts when the previous command completed, so a queued command's effective begin is that
completion), else the row with the latest end inside the tolerance, ties to the same family. When no row ends inside the
tolerance the chain records a `wait` (the device sat on nothing the trace shows: an in-order dispatch stall, a hidden barrier)
and continues from the latest earlier end. The walk stops at t <= tol. Every row on the chain is classed:
  stream     a DMA load >= 1 MB (a weight stream)              small-dma  any other load / gather (seeds, scales, tables)
  store      DmaStore / DmaScatter                             main / sub the TU pass by its joined context (unjoined: TuExec = main)
  S          a sync span that is a wait: real >= 1,500 and real/static > 5 (the launch skew lands here)
  tu-wait    a TU span with real >= 1,500 and real/static > 5: a wait booked in a TU span, not work
  sync       any other sync span (a barrier both clusters reached)        core   an unjoined Core / Cluster span
  wait       the chain's own gaps (nothing ends where the next row begins)
The summary line gives each class's share of the kernel total (a row counts up to its successor's begin, so the shares add up to
the total). S = the skew window (qkv / ffn: the first wait span; attn_out: the union of the overlapping waits) and
S uncovered = that window minus the DMA / TU rows with real/static <= 5 under it; P = total - S.

Why: the static scheduler's critical path is the main chain (it prices DMA at ~1,175 B/cycle), the device's is the weight
streams (qkv), the barrier chain + half B (ffn) or S (attn_out): a diff pays only when it shortens a row ON this chain, and
the qkv R1 head cut (norm on 16 slices, a cut on the static main chain) read flat because the head passes run under Wq and the
Wq stream, not the head, is on the chain (validated on the recorded traces, see --selftest).
Exit 0; 2 usage or input not found.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
from collections import Counter

K3 = ["sliding_project_qkv", "sliding_attention_output", "decoder_feedforward"]
SHORT = {"qkv": K3[0], "attn_out": K3[1], "ffn": K3[2]}
STREAM_MIN = 1_000_000
WAIT_MIN, WAIT_RATIO = 1500, 5
GROUPS = ("stream", "main", "S", "sub", "tu-wait", "small-dma", "store", "sync", "core", "wait", "other")


def kernel_name(k):
    return SHORT.get(k, k)


def find_timeline(path):
    """timeline.json for a file, a run dir (spans/, trace/spans/) or an iteration dir."""
    if os.path.isfile(path):
        return path
    for rel in ("spans/timeline.json", "trace/spans/timeline.json", "timeline.json"):
        p = os.path.join(path, rel)
        if os.path.isfile(p):
            return p
    hits = sorted(glob.glob(os.path.join(path, "*", "spans", "timeline.json")))
    return hits[-1] if hits else None


def load_rows(timeline_path, kernel):
    T = json.load(open(timeline_path))
    K = (T.get("kernels") or {}).get(kernel)
    if not K:
        return None, None
    rows = [dict(r) for r in K["rows"]]
    for i, r in enumerate(rows):
        r["_id"] = i
    return rows, K["summary"]


# ---------------------------------------------------------------- classes, S window ---------------------------------------

def is_wait(r):
    return r["family"] == "sync" and r["real_dur"] >= WAIT_MIN and (r.get("ratio") or 0) > WAIT_RATIO


def union(iv):
    out = []
    for b, e in sorted(iv):
        if e <= b:
            continue
        if out and b <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((b, e))
    return out


def s_window(rows, kernel):
    """(S, S uncovered, window) of the skew-absorbing wait on the traced cluster."""
    waits = sorted((r for r in rows if is_wait(r)), key=lambda r: r["real_begin"])
    if not waits:
        return 0, 0, None
    b, e = waits[0]["real_begin"], waits[0]["real_end"]
    if kernel == K3[1]:  # attn_out: the union of the waits overlapping the first
        for w in waits[1:]:
            if w["real_begin"] <= e:
                e = max(e, w["real_end"])
    cov = union([(max(r["real_begin"], b), min(r["real_end"], e)) for r in rows
                 if r["family"] in ("dma", "tu") and (r.get("ratio") is None or r["ratio"] <= WAIT_RATIO)])
    covered = sum(y - x for x, y in cov if y > x)
    return e - b, max(0, e - b - covered), (b, e)


def klass(r):
    if r["family"] == "dma":
        if (r.get("bytes") or 0) >= STREAM_MIN:
            return "stream"
        if str(r.get("tpe")) in ("DmaStore", "DmaScatter"):
            return "store"
        return "small-dma"
    if r["family"] == "tu":
        k = str(r.get("kind") or "")
        if r.get("real_dur", 0) >= WAIT_MIN and (r.get("ratio") or 0) > WAIT_RATIO:
            return "tu-wait"  # a wait booked in a TU span (segment_sweep's tu_slow)
        if k == "main":
            return "main"
        if k.startswith("sub"):
            return "sub"
        return "main" if r.get("real_kind") == "TuExec" else "sub"
    if r["family"] == "sync":
        if r.get("instr") is None:
            return "core"
        return "S" if is_wait(r) else "sync"
    return "other"


def fifo(rows):
    """Effective begin of every row: a DMA's transfer starts when the previous DMA completed (FIFO); others begin at their span."""
    prev_end, prev = None, None
    for r in sorted((r for r in rows if r["family"] == "dma"), key=lambda r: (r["real_begin"], r["real_end"])):
        r["fifo_pred"] = None
        if prev_end is not None and prev_end > r["real_begin"] and prev_end <= r["real_end"]:
            r["eff_begin"] = prev_end
            r["fifo_pred"] = prev
        else:
            r["eff_begin"] = r["real_begin"]
        if prev_end is None or r["real_end"] >= prev_end:
            prev_end, prev = r["real_end"], r
    for r in rows:
        r.setdefault("eff_begin", r["real_begin"])
        r.setdefault("fifo_pred", None)


# ---------------------------------------------------------------- the walk -------------------------------------------------

NEAR = 512  # a row that ended within this many cycles before the chosen predecessor is co-critical: cutting the chain gains at most that


def walk(rows, tol, near=NEAR):
    """-> (chain in time order, gaps [(from, to)], near [(at, chosen, alt, slack)]) from t=0 to the row that ends last. DramReuse
    programs are region bookkeeping (they end when the row that needs the region can go, and block nothing): never chain members.
    near = at each step the other candidates that ended within `near` cycles before the chosen predecessor (co-critical rows)."""
    live = [r for r in rows if (r["real_end"] > r["real_begin"] or r["family"] == "sync") and str(r.get("kind")) != "DramReuse"]
    if not live:
        return [], [], []
    cur = max(live, key=lambda r: (r["real_end"], -r["eff_begin"]))
    chain, gaps, nears, seen = [cur], [], [], {cur["_id"]}
    while cur["eff_begin"] > tol:
        b = cur["eff_begin"]
        cands = [r for r in live if r["_id"] not in seen and r["eff_begin"] < b and r["real_end"] <= b + tol]
        if not cands:
            gaps.append((0, b))
            break
        ins = set(cur.get("in_tensors") or [])
        producers = {r["_id"] for r in cands if ins & set(r.get("out_tensors") or [])}
        fp = cur.get("fifo_pred")
        within = [r for r in cands if r["real_end"] >= b - tol]
        if within:
            if cur["family"] == "dma" and fp is not None and fp["_id"] in {r["_id"] for r in within}:
                nxt = fp
            else:  # a true predecessor (ends at or before the begin) beats an overlapping one; then a producer; then the latest end, same family
                nxt = max(within, key=lambda r: (r["real_end"] <= b, r["_id"] in producers, r["real_end"] if r["real_end"] <= b else -r["real_end"],
                                                 r["family"] == cur["family"], -r["eff_begin"]))
        else:  # a wait: the row it waited for is its producer when one exists, else the latest earlier end
            nxt = max(cands, key=lambda r: (r["_id"] in producers, r["real_end"], -r["eff_begin"]))
            gaps.append((nxt["real_end"], b))
        for r in cands:
            if r is not nxt and nxt["real_end"] - near <= r["real_end"] and (r["family"] == "dma" or r["family"] == "sync"):
                nears.append((cur, nxt, r, max(0, nxt["real_end"] - r["real_end"])))
        chain.append(nxt)
        seen.add(nxt["_id"])
        cur = nxt
    if cur["eff_begin"] > 0 and cur["eff_begin"] <= tol:
        gaps.append((0, cur["eff_begin"]))
    return list(reversed(chain)), gaps, nears


def contributions(chain, gaps, total):
    """Cycles each chain row owns: from its effective begin to its successor's effective begin (a gap inside the tolerance belongs
    to the row; a recorded wait does not), the last row to its own end. Waits own their gaps and the stretch after the last row."""
    gap_ends = {e for _, e in gaps}
    out = []
    for i, r in enumerate(chain):
        if i + 1 < len(chain) and chain[i + 1]["eff_begin"] not in gap_ends:
            out.append(max(0, chain[i + 1]["eff_begin"] - r["eff_begin"]))
        else:
            out.append(max(0, r["real_end"] - r["eff_begin"]))
    wait = sum(e - b for b, e in gaps) + (max(0, total - chain[-1]["real_end"]) if chain else total)
    return out, wait


def shares(chain, gaps, total):
    sh = Counter()
    contrib, wait = contributions(chain, gaps, total)
    for r, c in zip(chain, contrib):
        sh[klass(r)] += c
    sh["wait"] += wait
    return sh


def analyze(rows, kernel, tol=64):
    fifo(rows)
    total = max(r["real_end"] for r in rows) if rows else 0
    chain, gaps, nears = walk(rows, tol)
    sh = shares(chain, gaps, total)
    S, S_unc, win = s_window(rows, kernel)
    items = []
    contrib, _ = contributions(chain, gaps, total)
    xfer = fifo_corrected_rates(rows)  # the span rate includes the FIFO queue wait behind the earlier command; the device rate is bytes / (end - max(begin, previous DMA end))
    for r, c in zip(chain, contrib):
        items.append({"class": klass(r), "instr": r.get("instr"), "name": r.get("name"), "kind": r.get("kind") or r.get("real_kind"),
                      "src": r.get("src"), "call": (r.get("call") or "")[:60], "begin": r["eff_begin"], "issue": r["real_begin"],
                      "end": r["real_end"], "dur": r["real_end"] - r["eff_begin"], "on_path": c,
                      "bytes": r.get("bytes"), "b_per_cycle": r.get("b_per_cycle"), "xfer_b_per_cycle": xfer.get(id(r)), "ratio": r.get("ratio"), "static_dur": r.get("static_dur")})
    for x in items:
        if x["class"] == "stream":
            under = [r for r in rows if r["family"] == "tu" and r["real_begin"] >= x["issue"] and r["real_end"] <= x["end"]]
            x["tu_under"] = len(under)
            x["under_slack"] = (x["end"] - max(r["real_end"] for r in under)) if under else None
    order = sorted(sh, key=lambda k: -sh[k])
    summary = " | ".join(f"{k} {100.0 * sh[k] / total:.0f} %" for k in order if sh[k] and total)
    chain_ids = {c["_id"] for c in chain}
    near_items = [{"at": cur.get("instr"), "at_begin": cur["eff_begin"], "chosen": nxt.get("instr"), "alt": r.get("instr"), "alt_class": klass(r),
                   "alt_end": r["real_end"], "slack": sl} for cur, nxt, r, sl in nears if r["_id"] not in chain_ids]
    return {"kernel": kernel, "total": total, "S": S, "S_uncovered": S_unc, "S_window": win, "P": total - S, "P_uncov": total - S_unc,
            "n_rows": len(rows), "n_chain": len(chain), "tol": tol, "shares": dict(sh), "summary": summary, "gaps": gaps, "chain": items, "near": near_items,
            "off_chain_tu": sorted(({"instr": r.get("instr"), "kind": r.get("kind"), "src": r.get("src"), "begin": r["real_begin"], "dur": r["real_dur"]}
                                    for r in rows if r["family"] == "tu" and r["_id"] not in {c["_id"] for c in chain}), key=lambda x: -x["dur"])[:10]}


def fmt(v):
    return "-" if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def fifo_corrected_rates(rows):
    """{id(row): FIFO-corrected transfer rate} for the DMA rows: bytes / (real_end - max(real_begin, the latest real_end of the DMA rows issued before it)); the tdma engine completes
    commands in order, so the part of a span spent behind the earlier command is queue wait, not transfer."""
    out = {}; prev_end = None
    for r in sorted((x for x in rows if x.get("family") == "dma" and x.get("real_begin") is not None and x.get("real_end") is not None), key=lambda x: (x["real_begin"], x["real_end"])):
        start = max(r["real_begin"], prev_end) if prev_end is not None else r["real_begin"]; dur = r["real_end"] - start; b = r.get("bytes")
        out[id(r)] = round(b / dur, 1) if (b and dur > 0) else None
        prev_end = max(prev_end or 0, r["real_end"])
    return out


def render(A, top=None):
    L = [f"{A['kernel']}: total {fmt(A['total'])} | S {fmt(A['S'])} (uncovered {fmt(A['S_uncovered'])}) | P = total - S {fmt(A['P'])} | "
         f"chain {A['n_chain']} of {A['n_rows']} rows, {len(A['gaps'])} waits, tol {A['tol']}",
         f"critical: {A['summary']}",
         "rates: B/c = span rate (bytes / span, includes the FIFO queue wait behind the earlier command); xfer = FIFO-corrected transfer rate (bytes / (end - max(begin, previous DMA end))), ED51",
         f"{'begin':>8} {'end':>8} {'on path':>8} {'class':9} {'instr':>17} {'kind':12} {'bytes':>10} {'B/c':>6} {'xfer':>6} {'ratio':>6} src / call"]
    items = A["chain"] if top is None else sorted(A["chain"], key=lambda x: -x["on_path"])[:top]
    for x in items:
        src = (x["src"] or "").split("/")[-1]
        L.append(f"{x['begin']:>8,} {x['end']:>8,} {x['on_path']:>8,} {x['class']:9} {str(x['instr']):>17} {str(x['kind'])[:12]:12} "
                 f"{fmt(x['bytes']):>10} {fmt(x['b_per_cycle']):>6} {fmt(x.get('xfer_b_per_cycle')):>6} {fmt(x['ratio']):>6} {src} {x['call']}")
    st = [x for x in A["chain"] if x["class"] == "stream"]
    if st:
        L.append("streams on the chain: " + "; ".join(f"{x['instr']} issued {x['issue']:,}, lands {x['end']:,}, {x['tu_under']} TU rows under it"
                                                     + (f", last one ends {x['under_slack']:,} before the landing" if x["under_slack"] is not None else "") for x in st))
    if A.get("near"):
        L.append("co-critical (off the chain, ended within 512 cycles of the chosen predecessor): " +
                 "; ".join(f"at {x['at']} (begin {x['at_begin']:,}) {x['alt']} {x['alt_class']} ends {x['alt_end']:,}, slack {x['slack']:,}" for x in A["near"][:6]))
    if A["gaps"]:
        L.append("waits (nothing the trace shows ends where the next chain row begins): " + ", ".join(f"{b:,}..{e:,} ({e - b:,})" for b, e in A["gaps"]))
    if A["off_chain_tu"]:
        L.append("largest TU rows NOT on the chain (cutting them cannot move the total): " +
                 "; ".join(f"{x['instr']} {x['kind']} {(x['src'] or '').split('/')[-1]} {x['dur']:,}" for x in A["off_chain_tu"][:6]))
    return "\n".join(L)


# ---------------------------------------------------------------- selftest -------------------------------------------------

MIRROR = os.path.expanduser("iterations")
HEADS = {K3[0]: "1872-qkv-trace-retry-1869", K3[1]: "1867-attn_out-trace-retry-1866", K3[2]: "1810-ffn-ffn-f8-direct-contraction-two-term-x"}
R1 = {"parent": "1993-qkv-parent-3d77d9cb-qkv", "r1": "2000-qkv-trace-retry-1999"}  # qkv R1 norm-16-slices vs its parent


def R(i, fam, kind, b, e, instr=None, tpe=None, bytes_=None, ratio=None, real_kind=None, src=None):
    return {"order": i, "name": real_kind or kind, "family": fam, "kind": kind, "real_kind": real_kind or kind, "instr": instr, "tpe": tpe,
            "src": src, "call": "", "real_begin": b, "real_end": e, "real_dur": e - b, "bytes": bytes_, "b_per_cycle": (round(bytes_ / (e - b), 1) if bytes_ else None),
            "ratio": ratio, "static_dur": (round((e - b) / ratio) if ratio else None)}


def synthetic():
    """A qkv-shaped kernel: seed load, a norm chain UNDER the weight stream (off the chain), a queued scale load, the epilogue, a scatter, S."""
    rows = [R(0, "dma", "DmaLoad", 100, 2100, "6", "DmaLoad", 245760, 1.8),
            R(1, "tu", "main", 2100, 3000, "44", "Main", None, 1.7, "TuExec", "a.rs:1"),       # norm chain, hidden under the stream
            R(2, "dma", "DmaLoad", 2100, 27700, "58", "DmaLoad", 15728640, 1.9),                # Wq stream 614 B/cycle
            R(3, "tu", "main", 3000, 8000, "53", "Main", None, 4.0, "TuExec", "a.rs:2"),
            R(4, "tu", "sub", 8000, 9000, "76", "Sub", None, 1.7, "StoVrf", "a.rs:3"),
            R(5, "dma", "DmaLoad", 20000, 28500, "156", "DmaLoad", 4096, 6.6),                  # queued behind Wq: transfer 27,700..28,500
            R(6, "tu", "main", 28500, 31000, "164", "Main", None, 2.0, "TuExec", "b.rs:1"),     # waits for the scale
            R(7, "dma", "DmaScatter", 31010, 32600, "405", "DmaScatter", None, 1.8),
            R(8, "sync", "ExplicitSync", 32610, 40000, "ExplicitSync(153)", "Core", None, 12.3, "Cluster")]
    return rows


def selftest():
    rows = synthetic()
    for i, r in enumerate(rows):
        r["_id"] = i
    A = analyze(rows, K3[0])
    ids = [x["instr"] for x in A["chain"]]
    assert ids == ["6", "58", "156", "164", "405", "ExplicitSync(153)"], ids            # the norm chain 44 / 53 / 76 is NOT on the chain
    assert A["chain"][2]["begin"] == 27700 and A["chain"][2]["issue"] == 20000, A["chain"][2]  # the queued scale load starts at Wq's completion
    xr = fifo_corrected_rates(rows); wq = [r for r in rows if r.get("instr") == "58"][0]; assert xr[id(wq)] == round(15728640 / (27700 - max(2100, max(r["real_end"] for r in rows if r.get("family") == "dma" and r["real_begin"] < 2100))), 1) if any(r.get("family") == "dma" and r["real_begin"] < 2100 for r in rows) else round(15728640 / 25600, 1), xr[id(wq)]  # the FIFO-corrected rate
    for x in A["chain"]:
        if x["class"] == "stream" and x.get("bytes"): assert x["xfer_b_per_cycle"] is not None and x["xfer_b_per_cycle"] >= (x["b_per_cycle"] or 0), x  # the correction never lowers a rate
    rl = render(A).splitlines(); assert "xfer" in rl[3] and "FIFO-corrected" in rl[2], rl[:4]  # the legend line, then the header with the xfer column
    assert A["S"] == 7390 and A["S_uncovered"] == 7390 and A["P"] == 40000 - 7390, (A["S"], A["S_uncovered"])
    sh = A["shares"]
    assert sh["stream"] == 25600 and sh["S"] == 7390 and sh["main"] == 2510 and sh["store"] == 1600 and sh["small-dma"] == 2000 + 800, sh
    assert sh["wait"] == 100 and sum(sh.values()) == A["total"] == 40000, sh  # the 10-cycle gaps inside the tolerance belong to the rows
    assert [x["instr"] for x in A["off_chain_tu"]] == ["53", "76", "44"], A["off_chain_tu"]
    wq = [x for x in A["chain"] if x["instr"] == "58"][0]
    assert wq["tu_under"] == 3 and wq["under_slack"] == 27700 - 9000, wq  # the norm chain ends 18,700 before Wq lands: cutting it is invisible
    txt = render(A)
    assert "critical: stream 64 %" in txt and "S 18 %" in txt and "NOT on the chain" in txt, txt
    # a synthetic attn_out: two overlapping waits form one S window; the post-norm chain under the second wait is covered work
    ao = [R(0, "dma", "DmaLoad", 100, 1700, "4", "DmaLoad", 262144, 1.8), R(1, "dma", "DmaLoad", 1710, 14000, "21", "DmaLoad", 7864320, 1.7),
          R(2, "dma", "DmaLoad", 14300, 27600, "33", "DmaLoad", 7864320, 1.9), R(3, "tu", "main", 27610, 30000, "41", "Main", None, 2.8, "TuExec"),
          R(4, "dma", "DmaStore", 30010, 31000, "50", "DmaStore", 7680, 0.5), R(5, "sync", "ExplicitSync", 31010, 40000, "ExplicitSync(27)", "Core", None, 14.6, "Cluster"),
          R(6, "sync", "DramReuse", 34800, 43600, "DramReuse(29)", "Core", None, 13.3, "Cluster"), R(7, "dma", "DmaLoad", 40010, 41100, "73", "DmaLoad", 7680, 1.9),
          R(8, "tu", "main", 41110, 43600, "78", "Main", None, 2.2, "TuExec"), R(9, "dma", "DmaStore", 43610, 44500, "98", "DmaStore", 7680, 1.2),
          R(10, "sync", "ExplicitSync", 44510, 44513, "ExplicitSync(38)", "Core", None, 0.005, "Cluster")]
    for i, r in enumerate(ao):
        r["_id"] = i
    B = analyze(ao, K3[1])
    assert B["S"] == 43600 - 31010 and B["S_uncovered"] == 43600 - 31010 - (41100 - 40010) - (43600 - 41110), (B["S"], B["S_uncovered"])
    assert [x["instr"] for x in B["chain"]] == ["4", "21", "33", "41", "50", "ExplicitSync(27)", "73", "78", "98", "ExplicitSync(38)"], [x["instr"] for x in B["chain"]]
    assert B["shares"]["S"] == 9000 and B["shares"]["stream"] == 12290 + 13310 and B["gaps"] == [(14000, 14300), (0, 100)], (B["shares"], B["gaps"])
    assert sum(B["shares"].values()) == B["total"] == 44513 and B["shares"]["wait"] == 400, B["shares"]  # DramReuse(29) is never a chain member
    assert klass({"family": "tu", "kind": None, "real_kind": "TuExec"}) == "main" and klass({"family": "sync", "instr": None}) == "core"
    # the recorded head traces under iterations/ and the qkv R1 reject, when present
    out = []
    for k, d in HEADS.items():
        tl = find_timeline(os.path.join(MIRROR, d))
        if not tl:
            continue
        rows, S = load_rows(tl, k)
        A = analyze(rows, k)
        assert sum(A["shares"].values()) == A["total"] and abs(A["total"] - S["total_real"]) <= 8, (A["shares"], A["total"], S["total_real"])
        top = max(A["shares"], key=lambda c: A["shares"][c])
        out.append(f"{k}: {A['summary']}")
        if k == K3[0]:
            assert top == "stream" and any(x["instr"] == "58" for x in A["chain"]), A["summary"]  # Wq (instr 58) is on the device chain
        if k == K3[2]:
            assert top == "stream", A["summary"]
    for tag, d in R1.items():
        tl = find_timeline(os.path.join(MIRROR, d))
        if not tl:
            continue
        rows, S = load_rows(tl, K3[0])
        A = analyze(rows, K3[0])
        wq = [x for x in A["chain"] if x["class"] == "stream" and x["bytes"] == 15728640]
        near_wq = [x for x in A["near"] if x["alt_class"] == "stream" and x["slack"] <= 512]
        if wq:  # R1: the Wq stream is on the chain; no TU row UNDER the stream is (rows before Wq's dispatch delayed the dispatch, not the stream)
            under = [x for x in A["chain"] if x["class"] in ("main", "sub", "tu-wait") and x["begin"] >= wq[0]["issue"] and x["end"] <= wq[0]["end"]]
            before = [x for x in A["chain"] if x["class"] in ("main", "sub", "tu-wait") and x["end"] <= wq[0]["issue"]]
            assert not under, (tag, A["summary"])
            behind = ", ".join(f"{x['instr']} {x['class']} {x['on_path']:,}" for x in before[-2:]) or "the seeds"  # Python 3.10: no nested same-quote f-strings
            out.append(f"R1 {tag}: {A['summary']} (Wq dispatched {wq[0]['issue']:,} behind {behind}, "
                       f"lands {wq[0]['end']:,}, {wq[0]['tu_under']} TU rows under it, none on the chain)")
        else:  # its parent: the head chain and Wq end within 512 cycles of each other (a tie), so the head cut can gain at most that slack
            assert near_wq, (tag, A["summary"], A["near"])
            out.append(f"R1 {tag}: {A['summary']} (Wq co-critical: ends {near_wq[0]['alt_end']:,}, slack {near_wq[0]['slack']:,} behind the head chain)")
    print("critical_path.py selftest ok")
    for line in out:
        print("  " + line)
    return 0


def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("path", nargs="?")
    ap.add_argument("kernel", nargs="?")
    ap.add_argument("--tol", type=int, default=64)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int)
    a = ap.parse_args()
    if not a.path or not a.kernel:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    tl = find_timeline(a.path)
    if not tl:
        print(f"critical_path.py: no timeline.json under {a.path}", file=sys.stderr)
        return 2
    k = kernel_name(a.kernel)
    rows, S = load_rows(tl, k)
    if rows is None:
        print(f"critical_path.py: kernel {k} not in {tl}", file=sys.stderr)
        return 2
    A = analyze(rows, k, a.tol)
    A["timeline"] = tl
    if a.json:
        print(json.dumps(A, indent=1))
    else:
        print(f"timeline {tl}")
        print(render(A, a.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
