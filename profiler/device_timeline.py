#!/usr/bin/env python3
"""Real TUC span timeline of one Arena run, joined with the static schedule, per kernel.

usage: profiler/device_timeline.py <run dir> [--out DIR] [--top N]
       profiler/device_timeline.py --log arena/<job>.log --schedule-dir schedule/ [--out DIR] [--top N]
       profiler/device_timeline.py --diff <parent run dir> <candidate run dir> [--kernel K] [--target INSTR ...] [--out DIR] [--top N]
       profiler/device_timeline.py --selftest
  run dir    an `ec2_run.py --out` dir: the newest arena/*.log is the real side, schedule/<kernel>.json the static side
  --out      where spans.json, timeline.json and timeline.md go (default <run dir>/spans/)
  --top      rows per kernel in timeline.md, longest real duration first (default 25; the json always holds every row)
  --diff     per-instruction real deltas candidate minus parent (both dirs are `ec2_run.py --out` dirs of trace-level runs;
             their spans/timeline.json is built when missing): writes span_deltas.json and span_deltas.md into the CANDIDATE
             dir (or --out). Rows of the two runs match on the key (family, kind, call text, bytes) and, inside one key group,
             the k-th parent row by static begin pairs with the k-th candidate row; surplus rows are reported as `removed`
             (parent only) or `added` (candidate only). Source lines and instruction indexes are never part of the key: both
             shift when code is inserted. --target <parent instruction index> flags the targeted rows (repeatable).
             Per matched row: real begin/dur, real/static ratio, idle gap, B/cycle of both runs and their deltas; per kernel:
             the totals of both runs (total real, cycles=, static makespan, idle, per-family sums, DMA B/cycle) and their
             deltas, the sum of matched deltas per family, the sum of removed and of added rows, and the net span delta
             (matched + added - removed; spans overlap, so it is not the delta of the total). Markdown: totals, targeted rows,
             top N rows by |delta real dur|, removed, added.
Real side: the lines tests/test_kernels.rs prints after each test's `cycles=` line, in runtime decode order:
    span kernel=<test name> name=<span name> begin=<u64> end=<u64>
With TUC_PROFILE_LEVEL=trace the runtime names spans by command type: `DMA`, `Renegade::TuExec`, `Renegade::StoTab`,
`Renegade::StoVrf`, `Renegade::StoTrf`, `Cluster`, `Core` (both sync programs: ExplicitSync / DramReuse), `Task`; info and debug give
one `Task` span per kernel. The runtime callback carries name / begin / end only (furiosa-opt-std ffi.rs:45, bindings `SpanFn`): no
cluster, PE, engine or counter field exists anywhere in the chain.
Static side: `cargo furiosa-opt compile --dump-schedule` json: instructions[] (index, tpe, contexts, description, lifetime.begin/end,
input_tensors, output_tensors) and tensors[] (index, size in bytes, shape); instruction tensor refs are tensor `index` values.

Join rule (chosen per kernel, recorded in timeline.json kernels.<k>.summary.join_rule, tried in this order):
  1. name: a span name that is an instruction index, or whose last integer is one, is that instruction; used when at least half of
     the spans resolve to distinct instructions (the rest stay unjoined, instr null).
  2. order/type: spans and instructions are split into families; inside a family an order-preserving alignment pairs spans (by real
     begin) with instructions (by static begin), never crossing, under a kind constraint:
       dma:  static tpe Dma* <-> real `DMA` (the runtime does not name the DMA kind, so every Dma* is compatible)
       tu:   static Main (`.commit()` on the Main context)          <-> `Renegade::TuExec`
             static Sub with an empty description (table write)     <-> `Renegade::StoTab`
             static Sub whose call ends in `.to_trf()`              <-> `Renegade::StoTrf`
             static Sub whose call ends in `.to_vrf()`              <-> `Renegade::StoVrf` or `Renegade::TuExec` (the runtime emits either)
             any other Sub, or a span name the runtime does not use <-> any tu span / any tu instruction
       sync: static tpe Core with a named index (`ExplicitSync(n)`, `DramReuse(n)`: the PeCore sync programs) <-> real `Cluster` or `Core`
     The alignment maximizes the number of compatible pairs; two instructions with the SAME static begin have no defined static order,
     so they may pair in either order. An incompatible pair is never made: when the real order contradicts the static order both
     sides stay unjoined (instr null; the instruction is listed under unpaired_instructions). Static tpe Core with a numeric index
     (PeCore programs) emits no span of its own and is excluded from the pairing (static_core_excluded); zero-lifetime instructions
     never pair. pair_check in the summary holds, per family, the real/static/paired counts, the histogram of static kind -> span name
     pairs, and the Spearman rank correlation of static vs real duration over the pairs.
  3. order: when no span name classifies, the k-th span by real begin pairs with the k-th non-zero, non-Core instruction by static begin.
An envelope span (one containing every other span of its kernel: `Task`) stays in spans.json but is excluded from the join and the sums.
Real begin/end in the tables are relative to the kernel's earliest span begin (absolute cycle counters stay in spans.json).

Bytes of a DMA row come from the moved tensor's schedule entry (size = SRAM footprint, shape = MappingShape string):
  padding `A # N` (A live positions of N; the others hold arbitrary values and are not written)
  scales the footprint by A/N; a named broadcast axis `DummyN [/ k]` (the DMA writes every one of its N/k copies: src/device/layout.rs)
  keeps the footprint and divides the unique payload by N/k. bytes = live bytes written on the DM side (the destination tensor of
  DmaLoad/DmaGather/DmaStos); for DmaStore/DmaScatter the destination is HBM and not listed, so bytes = the source tensor's unique
  payload. footprint_bytes, padding, broadcast, payload_bytes (= bytes / broadcast) and bytes_source sit next to bytes. A tensor without
  a shape string has an unknown transfer size: bytes, B/cycle and payload stay null and only footprint_bytes is kept (bytes_source says
  "footprint only (no shape): bytes unknown"); such rows are left out of the aggregate B/cycle. B/cycle = bytes / real duration;
  payload_b_per_cycle uses payload_bytes.
Per row: instruction (index, tpe, kind, ctx, src, call), static begin/end/dur, real begin/end/dur, real/static ratio, gap to the previous
real frontier (negative = overlap with an earlier span), bytes, B/cycle. Per kernel summary: total real (max end - min begin), the harness
cycles= value and whether it matches, union busy, total idle, per family the sum and the union of real durations and the median
real/static ratio, engine overlap (dma only / tu only / both), static makespan, real/static ratio, aggregate DMA B/cycle (sum of bytes /
sum of real duration over DMA rows with bytes), top-10 slowest vs static (real dur - static dur), top-10 longest real.
Per row also: static_util (the compiler's util.total_util of the instruction), in_tensors / out_tensors (schedule tensor indexes, for
dependency joins), dm_tensor / dm_buffer (name and buffer type of the DM-side tensor of a DMA). Per kernel summary also: t0_abs (absolute
cycle of the kernel's first span), n_unjoined + unjoined_spans (spans the join could not place), n_unpaired_static, n_span_lines_unparsed
(span lines SPAN_RE rejected), tu_split (main vs sub-context time, by kind), sync_by_kind (Cluster vs Core). timeline.json carries
spans_version; a timeline written by another version is rebuilt from its log (here and in record_attempt.ensure_timeline).
Exit 0; 1 when the log has no span lines; 2 usage."""
import argparse, glob, json, os, re, sys, tempfile
from collections import Counter
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
def sanitize(t): return t
K3 = ["sliding_project_qkv", "sliding_attention_output", "decoder_feedforward"]
SPAN_RE = re.compile(r"^\s*span kernel=(\S+) name=(.*?) begin=(\d+) end=(\d+)(?: tid=(\d+))?(?: run=(\d+))?\s*$")  # tid / run: the 0.8.1 measurement harness (tools/harness081)
DMA_TOK = {"dma", "load", "store", "gather", "scatter", "stos", "dmaload", "dmastore", "dmagather", "dmascatter", "dmastos"}
TU_TOK = {"main", "sub", "tu", "vector", "compute", "maincontext", "subcontext"}
TU_PREFIX = ("renegade", "tuexec", "sto")  # Renegade::TuExec / StoTab / StoVrf / StoTrf: the tensor-unit command spans
DST_OUT = ("DmaLoad", "DmaGather", "DmaStos")  # HBM/DM -> DM: the destination is the output tensor
RENEGADE = {"TuExec", "StoTab", "StoVrf", "StoTrf"}
COMPAT = {"main": {"TuExec"}, "sub_tab": {"StoTab"}, "sub_trf": {"StoTrf"}, "sub_vrf": {"StoVrf", "TuExec"}, "sub": RENEGADE}
FAMS = ("dma", "tu", "sync", "other")
VERSION = 2  # timeline.json format (spans_version): 2 = Core sync spans joined, tu_split / unjoined / tensor fields
SPAN_RUN = int(os.environ.get("SPANS_RUN", "0"))
SPAN_TID = int(os.environ["SPANS_TID"]) if os.environ.get("SPANS_TID", "") != "" else None  # --tid N / SPANS_TID: one cluster's rows only (the per-tid timeline of a 0.8.1 trace)  # the 0.8.1 measurement harness: which run's rows form the timeline (HARNESS_RUNS=1 traces have only run 0)
SYNC_NAMES = {"Cluster", "Core"}  # the runtime's two sync span names; both lower from ExplicitSync / DramReuse programs

def rel(p): return os.path.relpath(os.path.abspath(p), ROOT)

def parse_log_full(text):
    """-> ({kernel: [span in decode order]}, {kernel: cycles= value}, {kernel: count of `span kernel=` lines SPAN_RE rejected})."""
    spans, cycles, unparsed, cur = {}, {}, {}, None
    for line in text.splitlines():
        m = re.match(r"==> (\w+)", line)
        if m: cur = m.group(1); continue
        m = re.match(r"^\s*cycles=(\d+)(?: run=(\d+))?", line)
        if m and cur:
            if m.group(2) is None or int(m.group(2)) == SPAN_RUN: cycles[cur] = int(m.group(1))  # several runs per kernel: the timeline is ONE run (SPAN_RUN, default 0)
            continue
        m = SPAN_RE.match(line)
        if m:
            if m.group(6) is not None and int(m.group(6)) != SPAN_RUN: continue  # the other runs' rows: not this timeline
            if SPAN_TID is not None and m.group(5) is not None and int(m.group(5)) != SPAN_TID: continue  # --tid: the other cluster's rows
            lst = spans.setdefault(m.group(1), [])
            lst.append({"order": len(lst), "name": m.group(2), "begin": int(m.group(3)), "end": int(m.group(4)), **({"tid": int(m.group(5))} if m.group(5) is not None else {})})
        elif "span kernel=" in line:
            mk = re.search(r"span kernel=(\S+)", line); k = mk.group(1) if mk else (cur or "?")
            unparsed[k] = unparsed.get(k, 0) + 1
    return spans, cycles, unparsed

def parse_log(text):
    """-> ({kernel: [span in decode order]}, {kernel: cycles= value})."""
    spans, cycles, _ = parse_log_full(text); return spans, cycles

def family_of_tpe(tpe, index=""):
    t = str(tpe)
    if t.startswith("Dma"): return "dma"
    if t in ("Main", "Sub"): return "tu"
    if t == "Core": return "core" if str(index).isdigit() else "sync"  # ExplicitSync(n) / DramReuse(n) emit the `Cluster` spans
    return "other"

def family_of_name(name):
    toks = {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}
    if "cluster" in toks or "core" in toks: return "sync"
    if toks & DMA_TOK or any("dma" in t for t in toks): return "dma"
    if toks & TU_TOK or any(t.startswith(TU_PREFIX) for t in toks): return "tu"
    return "other"

def real_kind(name):
    """`Renegade::X` -> X, `DMA` -> DMA, `Cluster` / `Core` -> the sync kinds; any other name -> None (compatible with every instruction of its family)."""
    n = name.strip().split("::")[-1]
    return n if n in RENEGADE or n == "DMA" or n in SYNC_NAMES else None

def static_kind(tpe, desc, call, index=""):
    if tpe == "Main": return "main"
    if tpe == "Core": return tpe if str(index).isdigit() else re.sub(r"\(.*", "", str(index))  # ExplicitSync / DramReuse
    if tpe != "Sub": return tpe
    if not desc.strip(): return "sub_tab"
    if call.endswith(".to_trf()"): return "sub_trf"
    if call.endswith(".to_vrf()"): return "sub_vrf"
    return "sub"

def compatible(kind, instr):
    if kind is None: return True
    if instr["family"] == "dma": return kind == "DMA"
    if instr["family"] == "sync": return kind in SYNC_NAMES
    return kind in COMPAT.get(instr["kind"], RENEGADE)

def shape_factors(shape):
    """-> (pad_live, pad_of, broadcast): live positions and total positions over every `A # N` token, copies over `DummyN [/ k]`."""
    live, of, bc = 1, 1, 1
    for a, n in re.findall(r"(\d+)\s*#\s*(\d+)", shape or ""):
        a, n = int(a), int(n)
        if 0 < a < n: live *= a; of *= n
    for n, k in re.findall(r"Dummy(\d+)(?:\s*/\s*(\d+))?", shape or ""):
        bc *= max(1, int(n) // (int(k) if k else 1))
    return live, of, bc

def tensor_bytes(t):
    size = t.get("size"); shape = t.get("shape") or ""
    if not isinstance(size, int): return None
    live, of, bc = shape_factors(shape)
    written = round(size * live / of)
    return {"footprint": size, "padding": (of // live if of % live == 0 else round(of / live, 3)), "broadcast": bc,
            "written": written, "payload": round(written / bc), "shaped": bool(shape.strip())}

NO_BYTES = {"bytes": None, "bytes_source": None, "footprint_bytes": None, "padding": None, "broadcast": None, "payload_bytes": None, "dm_tensor": None, "dm_buffer": None}

def dma_bytes(i, tpe, tens):
    """bytes = live bytes written on the DM side; payload = unique bytes; fields say which tensor and rule produced them;
    dm_tensor / dm_buffer = the schedule `name` and `buffer_type` of the tensor(s) the bytes were taken from."""
    refs = (i.get("output_tensors") or [], i.get("input_tensors") or []) if tpe in DST_OUT else (i.get("input_tensors") or [], i.get("output_tensors") or [])
    dm_side = [(tens[r], tensor_bytes(tens[r])) for r in refs[0] if r in tens and tensor_bytes(tens[r])]
    hbm_side = [(tens[r], tensor_bytes(tens[r])) for r in refs[1] if r in tens and tensor_bytes(tens[r])]
    out = dict(NO_BYTES)
    pick, side, key = (dm_side, "dst", "written") if tpe in DST_OUT else (dm_side, "src", "payload")  # a store writes one copy to HBM
    if not pick and hbm_side: pick, side, key = hbm_side, ("src" if tpe in DST_OUT else "dst"), "payload"
    if not pick: return out
    ts = [t for t, _ in pick]; pick = [x for _, x in pick]
    out.update(dm_tensor=("+".join(str(t.get("name") or "") for t in ts).strip("+") or None), dm_buffer=("+".join(sorted({str(t["buffer_type"]) for t in ts if t.get("buffer_type")})) or None))
    out["footprint_bytes"] = sum(x["footprint"] for x in pick)
    if not all(x["shaped"] for x in pick):  # no MappingShape: the footprint is an allocation, not a transfer size -> bytes unknown
        out["bytes_source"] = f"{side} footprint only (no shape): bytes unknown"; return out
    out.update(bytes=sum(x[key] for x in pick), payload_bytes=sum(x["payload"] for x in pick),
               padding=(pick[0]["padding"] if len(pick) == 1 else None), broadcast=(pick[0]["broadcast"] if len(pick) == 1 else None),
               bytes_source=f"{side} {'live' if key == 'written' else 'payload'}")
    return out

def load_schedule(path):
    """-> (instructions sorted by static begin, static makespan)."""
    d = json.load(open(path))
    tens = {}
    for t in d.get("tensors") or []:
        try: tens[int(t.get("index"))] = t
        except (TypeError, ValueError): pass
    out = []
    for i in d.get("instructions") or []:
        lt = i.get("lifetime") or {}; b, e = int(lt.get("begin", 0)), int(lt.get("end", 0))
        tpe = str(i.get("tpe", "")); index = str(i.get("index")); fam = family_of_tpe(tpe, index); desc = i.get("description") or ""
        src = (re.search(r"-->\s*(\S+:\d+)", desc) or [None, None])[1]
        call = desc.strip().split("\n")[-1].strip() if desc.strip() else ""
        util = i.get("util") if isinstance(i.get("util"), dict) else {}
        row = {"index": index, "tpe": tpe, "family": fam, "kind": static_kind(tpe, desc, call, index), "ctx": ",".join(i.get("contexts") or []),
               "src": src, "call": call[:90], "static_begin": b, "static_end": e, "static_dur": e - b, "static_util": util.get("total_util"),
               "in_tensors": list(i.get("input_tensors") or []), "out_tensors": list(i.get("output_tensors") or [])}
        row.update(dma_bytes(i, tpe, tens) if fam == "dma" else NO_BYTES)
        out.append(row)
    out.sort(key=lambda x: (x["static_begin"], x["static_end"]))
    return out, max((x["static_end"] for x in out), default=0)

def mark_envelope(spans):
    if len(spans) == 1: spans[0]["envelope"] = True; return  # one span = the whole-kernel Task window (TUC_PROFILE_LEVEL info/debug)
    if not spans: return
    lo = min(s["begin"] for s in spans); hi = max(s["end"] for s in spans)
    env = [s for s in spans if s["begin"] == lo and s["end"] == hi]
    for s in spans: s["envelope"] = len(env) == 1 and s is env[0]

def align(spans, instrs):
    """Order-preserving alignment of spans (by real begin) with instructions (by static begin) that maximizes compatible pairs;
    instructions sharing a static begin may pair in either order. Surplus items on either side stay unpaired. -> {span order: index}"""
    R = sorted(spans, key=lambda s: (s["begin"], s["end"])); S = instrs; n, m = len(R), len(S)
    ok = [[compatible(real_kind(R[i]["name"]), S[j]) for j in range(m)] for i in range(n)]
    tie = [j >= 1 and S[j]["static_begin"] == S[j - 1]["static_begin"] for j in range(m)]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            b = max(dp[i - 1][j], dp[i][j - 1])
            if ok[i - 1][j - 1]: b = max(b, dp[i - 1][j - 1] + 1)
            if i >= 2 and tie[j - 1] and ok[i - 2][j - 1] and ok[i - 1][j - 2]: b = max(b, dp[i - 2][j - 2] + 2)
            dp[i][j] = b
    pairs = {}; i, j = n, m
    while i > 0 and j > 0:  # skips first, so surplus items are the LAST ones and the k-th pairs with the k-th when kinds allow
        v = dp[i][j]
        if v == dp[i - 1][j]: i -= 1
        elif v == dp[i][j - 1]: j -= 1
        elif ok[i - 1][j - 1] and v == dp[i - 1][j - 1] + 1: pairs[R[i - 1]["order"]] = S[j - 1]["index"]; i -= 1; j -= 1
        else: pairs[R[i - 2]["order"]] = S[j - 1]["index"]; pairs[R[i - 1]["order"]] = S[j - 2]["index"]; i -= 2; j -= 2
    return pairs

def rank_corr(xs, ys):
    """Spearman rank correlation (average ranks for ties); None below 3 pairs or when one side is constant."""
    if len(xs) < 3: return None
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]: j += 1
            for k in range(i, j + 1): r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys); mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx); syy = sum((b - my) ** 2 for b in ry)
    if not sxx or not syy: return None
    return round(sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sxx * syy) ** 0.5, 3)

def join(spans, sched):
    """-> (rows sorted by real begin, rule, unpaired instructions, pair check)."""
    mark_envelope(spans)
    work = [s for s in spans if not s.get("envelope")]
    nz = [i for i in sched if i["static_dur"] > 0 and i["family"] != "core"]
    idx = {i["index"]: i for i in sched}
    pairs, rule, hits = {}, None, {}
    for s in work:
        nums = re.findall(r"\d+", s["name"])
        cand = s["name"].strip() if s["name"].strip() in idx else (nums[-1] if nums else None)
        if cand in idx: hits[s["order"]] = cand
    if work and len(hits) * 2 >= len(work) and len(set(hits.values())) == len(hits): rule, pairs = "name", hits
    elif work:
        fam = {s["order"]: family_of_name(s["name"]) for s in work}
        if any(f != "other" for f in fam.values()):
            rule = "order/type"
            for f in ("dma", "tu", "sync"):
                pairs.update(align([s for s in work if fam[s["order"]] == f], [i for i in nz if i["family"] == f]))
        else:
            rule = "order"
            for s, i in zip(sorted(work, key=lambda s: (s["begin"], s["end"])), nz): pairs[s["order"]] = i["index"]
    t0 = min(s["begin"] for s in spans) if spans else 0
    rows, frontier = [], None
    for s in sorted(work, key=lambda s: (s["begin"], s["end"])):
        i = idx.get(pairs.get(s["order"])); rd = s["end"] - s["begin"]
        gap = None if frontier is None else s["begin"] - frontier
        frontier = s["end"] if frontier is None else max(frontier, s["end"])
        rows.append({"order": s["order"], "name": s["name"], "real_kind": real_kind(s["name"]) or s["name"], "family": i["family"] if i else family_of_name(s["name"]),
                     "instr": i["index"] if i else None, "tpe": i["tpe"] if i else None, "kind": i["kind"] if i else None, "ctx": i["ctx"] if i else None,
                     "src": i["src"] if i else None, "call": i["call"] if i else None,
                     "static_begin": i["static_begin"] if i else None, "static_end": i["static_end"] if i else None,
                     "static_dur": i["static_dur"] if i else None, "real_begin": s["begin"] - t0, "real_end": s["end"] - t0, "real_dur": rd,
                     "ratio": round(rd / i["static_dur"], 3) if i and i["static_dur"] > 0 else None, "gap": gap,
                     "bytes": i["bytes"] if i else None, "bytes_source": i["bytes_source"] if i else None, "footprint_bytes": i["footprint_bytes"] if i else None,
                     "padding": i["padding"] if i else None, "broadcast": i["broadcast"] if i else None, "payload_bytes": i["payload_bytes"] if i else None,
                     "b_per_cycle": round(i["bytes"] / rd, 1) if i and i["bytes"] and rd > 0 else None,
                     "payload_b_per_cycle": round(i["payload_bytes"] / rd, 1) if i and i["payload_bytes"] and rd > 0 else None,
                     "static_util": i["static_util"] if i else None, "in_tensors": i["in_tensors"] if i else None, "out_tensors": i["out_tensors"] if i else None,
                     "dm_tensor": i["dm_tensor"] if i else None, "dm_buffer": i["dm_buffer"] if i else None})
    used = set(pairs.values())
    unpaired = [{"index": i["index"], "tpe": i["tpe"], "kind": i["kind"], "src": i["src"], "static_dur": i["static_dur"]} for i in nz if i["index"] not in used]
    check = {"static_core_excluded": sum(1 for i in sched if i["family"] == "core" and i["static_dur"] > 0)}
    for f in FAMS:
        rs = [r for r in rows if r["family"] == f]; pr = [r for r in rs if r["instr"]]
        check[f] = {"real": len(rs), "static": sum(1 for i in nz if i["family"] == f), "paired": len(pr),
                    "kind_pairs": dict(sorted(Counter(f"{r['kind']}->{r['real_kind']}" for r in pr).items())),
                    "dur_rank_corr": rank_corr([r["static_dur"] for r in pr], [r["real_dur"] for r in pr])}
    return rows, rule, unpaired, check

def union_len(iv):
    """Total length covered by a list of (begin, end) intervals."""
    busy, cb, ce = 0, None, None
    for b, e in sorted(iv):
        if cb is None: cb, ce = b, e
        elif b > ce: busy += ce - cb; cb, ce = b, e
        else: ce = max(ce, e)
    return busy + ((ce - cb) if cb is not None else 0)

def median(v):
    v = sorted(v); n = len(v)
    return None if not n else (v[n // 2] if n % 2 else round((v[n // 2 - 1] + v[n // 2]) / 2, 3))

def summarize(k, spans, rows, makespan, cycles_line, rule, check, unpaired=(), unparsed=0):
    lo = min(s["begin"] for s in spans); hi = max(s["end"] for s in spans); total = hi - lo
    unj = [r for r in rows if r["instr"] is None]; tu_rows = [r for r in rows if r["family"] == "tu"]
    def part(rs): return {"n": len(rs), "sum": sum(r["real_dur"] for r in rs), "union": union_len([(r["real_begin"], r["real_end"]) for r in rs])}
    main_rows = [r for r in tu_rows if r["kind"] == "main"]; sub_rows = [r for r in tu_rows if str(r["kind"]).startswith("sub")]
    tu_split = {"main": part(main_rows), "sub": dict(part(sub_rows), by_kind={kd: part([r for r in sub_rows if r["kind"] == kd]) for kd in sorted({r["kind"] for r in sub_rows})}),
                "unjoined": part([r for r in tu_rows if r["instr"] is None]),
                "main_sub_overlap": part(main_rows)["union"] + part(sub_rows)["union"] - union_len([(r["real_begin"], r["real_end"]) for r in main_rows + sub_rows]),
                "rule": "tu rows by the joined instruction's kind: main = Main-context commits; sub = Sub context (sub_tab table write, sub_trf .to_trf(), sub_vrf .to_vrf(), sub other); unjoined = tu spans without an instruction"}
    sync_by_kind = {kd: {"n": sum(1 for r in rows if r["family"] == "sync" and r["real_kind"] == kd), "sum": sum(r["real_dur"] for r in rows if r["family"] == "sync" and r["real_kind"] == kd)}
                    for kd in sorted({r["real_kind"] for r in rows if r["family"] == "sync"})}
    inner = [s for s in spans if not s.get("envelope")] or spans  # only an envelope: it is the busy window itself
    busy = union_len([(s["begin"], s["end"]) for s in inner])
    fam_sum = {f: sum(r["real_dur"] for r in rows if r["family"] == f) for f in FAMS}
    fam_n = {f: sum(1 for r in rows if r["family"] == f) for f in FAMS}
    fam_union = {f: union_len([(r["real_begin"], r["real_end"]) for r in rows if r["family"] == f]) for f in FAMS}
    both = fam_union["dma"] + fam_union["tu"] - union_len([(r["real_begin"], r["real_end"]) for r in rows if r["family"] in ("dma", "tu")])
    fam_median = {f: median([r["ratio"] for r in rows if r["family"] == f and r["ratio"] is not None]) for f in ("dma", "tu", "sync")}
    fam_median["tu_main"] = median([r["ratio"] for r in rows if r["kind"] == "main" and r["ratio"] is not None])
    fam_median["tu_sub"] = median([r["ratio"] for r in rows if str(r["kind"]).startswith("sub") and r["ratio"] is not None])
    def brief(r): return {"order": r["order"], "name": r["name"], "instr": r["instr"], "tpe": r["tpe"], "src": r["src"], "static_dur": r["static_dur"],
                          "real_dur": r["real_dur"], "excess": (r["real_dur"] - r["static_dur"]) if r["static_dur"] is not None else None, "ratio": r["ratio"],
                          "bytes": r["bytes"], "b_per_cycle": r["b_per_cycle"]}
    slow = sorted([r for r in rows if r["static_dur"]], key=lambda r: -(r["real_dur"] - r["static_dur"]))[:10]
    dma = [r for r in rows if r["family"] == "dma" and r["bytes"]]
    db, dc, dpay = sum(r["bytes"] for r in dma), sum(r["real_dur"] for r in dma), sum(r["payload_bytes"] or 0 for r in dma)
    return {"kernel": k, "join_rule": rule, "n_spans": len(spans), "n_envelope": sum(1 for s in spans if s.get("envelope")),
            "n_joined": sum(1 for r in rows if r["instr"]), "n_rows": len(rows), "n_per_family": fam_n, "pair_check": check, "t0_abs": lo,
            "n_unjoined": len(unj), "unjoined_spans": [{"order": r["order"], "name": r["name"], "family": r["family"], "real_begin": r["real_begin"], "real_dur": r["real_dur"]} for r in sorted(unj, key=lambda r: -r["real_dur"])],
            "n_unpaired_static": len(unpaired), "n_span_lines_unparsed": unparsed, "tu_split": tu_split, "sync_by_kind": sync_by_kind,
            "total_real": total, "cycles_line": cycles_line, "cycles_line_matches": (cycles_line == total) if cycles_line is not None else None,
            "union_busy": busy, "total_idle": total - busy, "sum_real": fam_sum, "union_real": fam_union,
            "engine_overlap": {"dma_only": fam_union["dma"] - both, "tu_only": fam_union["tu"] - both, "both": both,
                               "neither": busy - union_len([(r["real_begin"], r["real_end"]) for r in rows if r["family"] in ("dma", "tu")])},
            "median_ratio": fam_median, "static_makespan": makespan,
            "real_static_ratio": round(total / makespan, 3) if makespan else None,
            "dma_rows_with_bytes": len(dma), "dma_bytes_known": db or None, "dma_b_per_cycle": round(db / dc, 1) if db and dc else None,
            "dma_payload_bytes_known": dpay or None, "dma_payload_b_per_cycle": round(dpay / dc, 1) if dpay and dc else None,
            "top10_slowest_vs_static": [brief(r) for r in slow], "top10_longest_real": [brief(r) for r in sorted(rows, key=lambda r: -r["real_dur"])[:10]]}

def fmt(v): return "-" if v is None else f"{v:,}" if isinstance(v, int) else str(v)

def md_kernel(k, S, rows, unpaired, top, json_rel):
    pc = S["pair_check"]; eo = S["engine_overlap"]; U = S["union_real"]
    L = [f"### {k}", "",
         f"join rule: `{S['join_rule']}`; spans {S['n_spans']} (envelope {S['n_envelope']}), joined {S['n_joined']}/{S['n_rows']}; "
         f"total real {fmt(S['total_real'])} (harness cycles= {fmt(S['cycles_line'])}, match {S['cycles_line_matches']}); static makespan {fmt(S['static_makespan'])}; "
         f"real/static {S['real_static_ratio']}; union busy {fmt(S['union_busy'])}; idle {fmt(S['total_idle'])}; "
         f"sum real dma {fmt(S['sum_real']['dma'])} / tu {fmt(S['sum_real']['tu'])} / sync {fmt(S['sum_real']['sync'])} / other {fmt(S['sum_real']['other'])}; "
         f"union dma {fmt(U['dma'])} / tu {fmt(U['tu'])} / sync {fmt(U['sync'])} (dma only {fmt(eo['dma_only'])}, tu only {fmt(eo['tu_only'])}, both {fmt(eo['both'])}, neither {fmt(eo['neither'])}); "
         f"median real/static dma {S['median_ratio']['dma']} / tu {S['median_ratio']['tu']} / sync {S['median_ratio']['sync']}; "
         f"DMA B/cycle {S['dma_b_per_cycle']} over {S['dma_rows_with_bytes']} rows with bytes (payload {S['dma_payload_b_per_cycle']}); unpaired static instructions {len(unpaired)}; "
         f"unjoined spans {S['n_unjoined']} ({fmt(sum(x['real_dur'] for x in S['unjoined_spans']))} cycles); span lines unparsed {S['n_span_lines_unparsed']}; "
         f"TU sum real main {fmt(S['tu_split']['main']['sum'])} / sub {fmt(S['tu_split']['sub']['sum'])}; sync spans by kind {S['sync_by_kind']}.", "",
         f"pair check: dma real {pc['dma']['real']} / static {pc['dma']['static']} / paired {pc['dma']['paired']} (duration rank corr {pc['dma']['dur_rank_corr']}); "
         f"tu real {pc['tu']['real']} / static {pc['tu']['static']} / paired {pc['tu']['paired']} (rank corr {pc['tu']['dur_rank_corr']}), kind pairs {pc['tu']['kind_pairs']}; "
         f"sync real {pc['sync']['real']} / static {pc['sync']['static']} / paired {pc['sync']['paired']}, kind pairs {pc['sync']['kind_pairs']}; static Core excluded {pc['static_core_excluded']}.", "",
         f"Top {min(top, len(rows))} of {len(rows)} rows by real duration (order = decode position; full table: [{json_rel}]({json_rel})).", "",
         "| order | span | instr | tpe | src | static begin..end (dur) | real begin..end (dur) | real/static | gap | bytes | B/cycle |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["real_dur"])[:top]:
        st = f"{fmt(r['static_begin'])}..{fmt(r['static_end'])} ({fmt(r['static_dur'])})" if r["instr"] else "-"
        L.append(f"| {r['order']} | `{r['name']}` | {r['instr'] or '-'} | {r['tpe'] or '-'} | {r['src'] or '-'} | {st} | "
                 f"{fmt(r['real_begin'])}..{fmt(r['real_end'])} ({fmt(r['real_dur'])}) | {fmt(r['ratio'])} | {fmt(r['gap'])} | {fmt(r['bytes'])} | {fmt(r['b_per_cycle'])} |")
    L += ["", "Top-10 slowest vs static (real dur - static dur):", "", "| span | instr | tpe | src | static dur | real dur | excess | ratio |", "|---|---|---|---|---|---|---|---|"]
    for t in S["top10_slowest_vs_static"]:
        L.append(f"| `{t['name']}` | {t['instr']} | {t['tpe']} | {t['src'] or '-'} | {fmt(t['static_dur'])} | {fmt(t['real_dur'])} | {fmt(t['excess'])} | {fmt(t['ratio'])} |")
    return L + [""]

def run(logp, sched_dir, out, top):
    text = open(logp, errors="replace").read(); spans, cyc, unparsed = parse_log_full(text)
    for k, n in unparsed.items():
        if k not in spans: print(f"device_timeline.py: {k}: {n} `span kernel=` line(s) rejected by SPAN_RE and no parsable span: kernel dropped")
    if not spans: print(f"device_timeline.py: no `span kernel=` lines in {logp}"); return 1
    os.makedirs(out, exist_ok=True)
    res = {"log": rel(logp), "schedule_dir": rel(sched_dir), "spans_version": VERSION, "kernels": {}}
    md = ["# Real TUC span timeline vs static schedule", "", f"log `{rel(logp)}`; schedules `{rel(sched_dir)}`; produced by profiler/device_timeline.py.", ""]
    jrel = os.path.relpath(os.path.join(out, "timeline.json"), out)
    for k in K3 + sorted(x for x in spans if x not in K3):
        if k not in spans: continue
        sp = os.path.join(sched_dir, f"{k}.json")
        sched, mk = load_schedule(sp) if os.path.exists(sp) else ([], None)
        rows, rule, unpaired, check = join(spans[k], sched)
        S = summarize(k, spans[k], rows, mk, cyc.get(k), rule, check, unpaired, unparsed.get(k, 0))
        res["kernels"][k] = {"summary": S, "rows": rows, "unpaired_instructions": unpaired}
        md += md_kernel(k, S, rows, unpaired, top, jrel)
        pc = {f: (check[f]["real"], check[f]["static"], check[f]["paired"], check[f]["dur_rank_corr"]) for f in ("dma", "tu", "sync")}
        print(f"{k:28s} rule={rule} spans={S['n_spans']} joined={S['n_joined']}/{S['n_rows']} unjoined={S['n_unjoined']} unparsed={S['n_span_lines_unparsed']} real={S['total_real']:,} static={mk} ratio={S['real_static_ratio']} idle={S['total_idle']:,} dma={S['sum_real']['dma']:,} tu={S['sum_real']['tu']:,} (main {S['tu_split']['main']['sum']:,} / sub {S['tu_split']['sub']['sum']:,}) B/cycle={S['dma_b_per_cycle']} pair(real,static,paired,corr)={pc} kinds={check['tu']['kind_pairs']} sync={S['sync_by_kind']}")
    open(os.path.join(out, "spans.json"), "w").write(sanitize(json.dumps(spans, indent=1)))
    open(os.path.join(out, "timeline.json"), "w").write(sanitize(json.dumps(res, indent=1)))
    open(os.path.join(out, "timeline.md"), "w").write(sanitize("\n".join(md) + "\n"))
    print("out:", out); return 0

SELFTEST_HEAD = """NPU kernel tests -- 3 cases against a precomputed reference, with on-device cycle counts

==> sliding_project_qkv
[sliding_project_qkv q             ] max|Δ|=  0.03125 (   1.45% of expected)  mean|Δ|= 0.000996  within tol=100.00%  -> PASS
    cycles={cycles}
"""
def I(index, tpe, ctx, b, e, ins, outs, desc, util=None): return {"index": index, "tpe": tpe, "contexts": [ctx], "lifetime": {"begin": b, "end": e}, "input_tensors": ins, "output_tensors": outs, "description": desc, **({"util": util} if util else {})}
SELFTEST_SCHED = {"instructions": [
    I("7", "DmaLoad", "DmaEngine", 0, 100, [11], [10], "--> src/ops.rs:59:58\n\nx.to_dm(&mut ctx.tdma)", {"total_util": 0.5, "base_util": 0.5, "efficiency": 1.0}),
    I("28", "Main", "MainContext", 100, 400, [10], [12], "--> src/a.rs:1:1\n\nmatmul\n.commit()"),
    I("43", "DmaLoad", "DmaEngine", 400, 600, [13], [14], ""),
    I("99", "Sub", "Noop", 0, 0, [], [], ""),
    I("50", "Core", "PeCore", 100, 200, [], [], "")],
    # tensor 10: one live cluster of two, one live slice of 256 -> 7,680 of 3,932,160 bytes are written; 14: one live cluster, 16 broadcast copies
    "tensors": [{"index": "10", "size": 3932160, "shape": "MappingShape[Chip: 1, Cluster: 1 # 2, Slice: 1 # 256, Element: H]", "name": "vISA(T10)_x_dm", "buffer_type": "Sram"}, {"index": "12", "size": 8},
                {"index": "14", "size": 65536, "shape": "MappingShape[Chip: 1, Cluster: 1 # 2, Slice: (Dummy256 / 16, Ns), Element: Ds]"}, {"index": "13", "size": 32768}]}
# tie: 60 (Main) and 61 (Sub .to_vrf()) share static begin 600; 70 (Sub table) then 71 (Main) with the real order reversed and no tie;
# ExplicitSync(9) is the PeCore sync program behind the `Cluster` span
SELFTEST_SCHED_TIE = {"instructions": SELFTEST_SCHED["instructions"] + [
    I("60", "Main", "MainContext", 600, 700, [], [], "--> src/b.rs:2:2\n\nctx\n.main\n.commit()"),
    I("61", "Sub", "SubContext", 600, 650, [], [], "--> src/b.rs:3:3\n\nctx\n.sub\n.to_vrf()"),
    I("70", "Sub", "SubContext", 800, 900, [], [], ""),
    I("71", "Main", "MainContext", 900, 1000, [], [], "--> src/b.rs:4:4\n\n.commit()"),
    I("ExplicitSync(9)", "Core", "PeCore", 1000, 1050, [], [], "")], "tensors": SELFTEST_SCHED["tensors"]}

def selftest():
    global SPAN_TID
    two = "==> k\n span kernel=k name=a begin=0 end=10 tid=0 run=0\n span kernel=k name=b begin=5 end=12 tid=1 run=0\n span kernel=k name=c begin=20 end=30 run=0\n cycles=30 run=0\n"
    assert [x["name"] for x in parse_log_full(two)[0]["k"]] == ["a", "b", "c"]
    SPAN_TID = 1; assert [x["name"] for x in parse_log_full(two)[0]["k"]] == ["b", "c"], "--tid 1 keeps tid-1 rows and the tid-less rows"  # the per-tid timeline
    SPAN_TID = None
    t = tempfile.mkdtemp()
    def case(names, sched=SELFTEST_SCHED, cycles=1000, extra=()):
        sd = os.path.join(t, "schedule"); os.makedirs(sd, exist_ok=True); json.dump(sched, open(os.path.join(sd, "sliding_project_qkv.json"), "w"))
        base = [(5100, 5300), (5250, 5700), (5800, 6100)]
        lines = [f"    span kernel=sliding_project_qkv name={n} begin={b} end={e}" for n, (b, e) in zip(names, base + list(extra))]
        lp = os.path.join(t, "log.txt"); open(lp, "w").write(SELFTEST_HEAD.format(cycles=cycles) + "\n".join(lines) + "\n\nall 3 tests passed\n")
        assert run(lp, sd, os.path.join(t, "out"), 25) == 0
        R = json.load(open(os.path.join(t, "out", "timeline.json")))["kernels"]["sliding_project_qkv"]
        return R["summary"], {r["order"]: r for r in R["rows"]}, R["unpaired_instructions"]
    # 1. names carry instruction indexes -> name rule; bytes honour padding (7,680 live of 3,932,160) and broadcast (32,768 written, 2,048 payload)
    S, rows, un = case(["7", "tuc_28", "load#43"])
    assert S["join_rule"] == "name" and S["n_joined"] == 3 and not un, S
    assert S["total_real"] == 1000 and S["cycles_line"] == 1000 and S["cycles_line_matches"] is True, S
    assert S["union_busy"] == 900 and S["total_idle"] == 100, S  # [5100,5700] + [5800,6100]
    assert rows[0]["instr"] == "7" and rows[0]["ratio"] == 2.0 and rows[0]["real_begin"] == 0 and rows[0]["gap"] is None, rows[0]
    assert rows[0]["bytes"] == 7680 and rows[0]["footprint_bytes"] == 3932160 and rows[0]["padding"] == 512 and rows[0]["broadcast"] == 1 and rows[0]["bytes_source"] == "dst live", rows[0]
    assert rows[1]["instr"] == "28" and rows[1]["gap"] == -50 and rows[1]["ratio"] == 1.5 and rows[1]["kind"] == "main", rows[1]
    assert rows[2]["instr"] == "43" and rows[2]["gap"] == 100 and rows[2]["bytes"] == 32768 and rows[2]["payload_bytes"] == 2048 and rows[2]["broadcast"] == 16 and rows[2]["padding"] == 2, rows[2]
    assert rows[2]["b_per_cycle"] == round(32768 / 300, 1) and rows[2]["payload_b_per_cycle"] == round(2048 / 300, 1), rows[2]
    assert S["sum_real"] == {"dma": 500, "tu": 450, "sync": 0, "other": 0} and S["static_makespan"] == 600 and S["real_static_ratio"] == round(1000 / 600, 3), S
    assert S["union_real"]["dma"] == 500 and S["union_real"]["tu"] == 450 and S["engine_overlap"] == {"dma_only": 450, "tu_only": 400, "both": 50, "neither": 0}, S  # dma [0,200]+[700,1000], tu [150,600]
    assert S["median_ratio"] == {"dma": 1.75, "tu": 1.5, "sync": None, "tu_main": 1.5, "tu_sub": None}, S["median_ratio"]  # dma ratios 2.0 and 1.5 -> 1.75
    assert S["top10_slowest_vs_static"][0]["instr"] == "28" and S["top10_slowest_vs_static"][0]["excess"] == 150, S["top10_slowest_vs_static"]
    assert S["dma_b_per_cycle"] == round((7680 + 32768) / 500, 1) and S["dma_payload_b_per_cycle"] == round((7680 + 2048) / 500, 1), S
    # 1b. audit fields: absolute t0, unjoined / unpaired / unparsed counts, the compiler's util, tensor refs and names, the TU main/sub split, the version stamp
    assert S["t0_abs"] == 5100 and S["n_unjoined"] == 0 and S["unjoined_spans"] == [] and S["n_unpaired_static"] == 0 and S["n_span_lines_unparsed"] == 0, S
    assert rows[0]["static_util"] == 0.5 and rows[1]["static_util"] is None and rows[0]["in_tensors"] == [11] and rows[0]["out_tensors"] == [10], rows[0]
    assert rows[0]["dm_tensor"] == "vISA(T10)_x_dm" and rows[0]["dm_buffer"] == "Sram" and rows[2]["dm_tensor"] is None and rows[1]["dm_tensor"] is None and rows[1]["dm_buffer"] is None, (rows[0], rows[2])
    assert S["tu_split"]["main"] == {"n": 1, "sum": 450, "union": 450} and S["tu_split"]["sub"]["n"] == 0 and S["tu_split"]["sub"]["by_kind"] == {} and S["tu_split"]["unjoined"]["n"] == 0 and S["tu_split"]["main_sub_overlap"] == 0 and S["sync_by_kind"] == {}, S["tu_split"]
    assert json.load(open(os.path.join(t, "out", "timeline.json")))["spans_version"] == VERSION
    sp_, cy_, up_ = parse_log_full("==> k\n    cycles=5\n    span kernel=k name=DMA begin=1\n    span kernel=k name=DMA begin=1 end=2\n")
    assert len(sp_["k"]) == 1 and cy_ == {"k": 5} and up_ == {"k": 1}, (sp_, cy_, up_)  # a span line SPAN_RE rejects is counted, never silently dropped
    # 2. runtime family names -> order/type rule gives the same pairing; the Core instruction 50 is excluded, not unpaired
    S, rows, un = case(["DMA", "Renegade::TuExec", "DMA"])
    assert S["join_rule"] == "order/type" and [rows[i]["instr"] for i in range(3)] == ["7", "28", "43"] and not un, (S["join_rule"], rows)
    assert S["pair_check"]["static_core_excluded"] == 1 and S["pair_check"]["dma"] == {"real": 2, "static": 2, "paired": 2, "kind_pairs": {"DmaLoad->DMA": 2}, "dur_rank_corr": None}, S["pair_check"]
    assert S["pair_check"]["tu"]["kind_pairs"] == {"main->TuExec": 1}, S["pair_check"]
    # 3. opaque names -> order rule; the zero-lifetime instruction 99 never pairs
    S, rows, un = case(["a", "b", "c"])
    assert S["join_rule"] == "order" and [rows[i]["instr"] for i in range(3)] == ["7", "28", "43"] and not un, (S["join_rule"], rows)
    assert rank_corr([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0 and rank_corr([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0 and rank_corr([1, 1, 1], [1, 2, 3]) is None
    assert shape_factors("MappingShape[Chip: 1, Cluster: H / 1920 % 2, Slice: (H / 120 % 16, Qs / 256), Element: (H % 40, Qs % 256)]") == (1, 1, 1)
    assert shape_factors("MappingShape[Chip: 1, Cluster: L / 7680 % 2, Slice: (L / 120 % 64, 1 # 2, 1 # 2), Element: 1 # 8]") == (1, 32, 1)
    assert shape_factors("MappingShape[Chip: 1, Cluster: 1 # 2, Slice: Dummy256, Element: H]") == (1, 2, 256)
    nb = dma_bytes({"input_tensors": [1], "output_tensors": [2]}, "DmaLoad", {2: {"index": "2", "size": 4096}})  # shape-less destination
    assert nb["bytes"] is None and nb["payload_bytes"] is None and nb["footprint_bytes"] == 4096 and nb["bytes_source"] == "dst footprint only (no shape): bytes unknown", nb
    assert dma_bytes({"input_tensors": [1], "output_tensors": [2]}, "DmaLoad", {})["bytes_source"] is None  # HBM both sides: nothing listed
    # 4. an envelope span is excluded from the join and the sums; a surplus span stays unjoined and instruction 43 stays unpaired
    S, rows, un = case(["DmaLoad", "MainContext", "MainContext", "Task"], extra=[(5100, 6100)])
    assert S["n_spans"] == 4 and S["n_envelope"] == 1 and S["n_rows"] == 3 and S["total_real"] == 1000 and S["union_busy"] == 900, S
    assert rows[0]["instr"] == "7" and rows[2]["instr"] is None and [u["index"] for u in un] == ["43"], (rows, un)
    assert S["n_unjoined"] == 1 and S["unjoined_spans"] == [{"order": 2, "name": "MainContext", "family": "tu", "real_begin": 700, "real_dur": 300}] and S["n_unpaired_static"] == 1 and S["tu_split"]["unjoined"] == {"n": 1, "sum": 300, "union": 300}, S
    # 5. kind constraint: at the static-begin tie 600 the TuExec goes to the Main (60) and the StoVrf to the Sub .to_vrf() (61) although the
    #    Sub sorts first; at 800/900 the real order contradicts the static order without a tie, so one side stays unpaired instead of
    #    mismatching; the Cluster span pairs with the ExplicitSync program (sync family) and counts as `neither` engine
    S, rows, un = case(["DMA", "Renegade::TuExec", "DMA", "Renegade::TuExec", "Renegade::StoVrf", "Renegade::TuExec", "Renegade::StoTab", "Cluster"],
                       sched=SELFTEST_SCHED_TIE, cycles=1500, extra=[(6100, 6200), (6105, 6150), (6300, 6400), (6400, 6500), (6500, 6600)])
    assert S["join_rule"] == "order/type" and rows[3]["instr"] == "60" and rows[4]["instr"] == "61", (rows[3], rows[4])
    tu = S["pair_check"]["tu"]; assert tu["real"] == 5 and tu["static"] == 5 and tu["paired"] == 4, tu
    assert set(tu["kind_pairs"]) <= {"main->TuExec", "sub_vrf->StoVrf", "sub_vrf->TuExec", "sub_tab->StoTab"}, tu["kind_pairs"]
    assert len(un) == 1 and un[0]["index"] in ("70", "71") and sum(1 for r in rows.values() if r["family"] == "tu" and r["instr"] is None) == 1, (un, rows)
    assert rows[7]["instr"] == "ExplicitSync(9)" and rows[7]["family"] == "sync" and rows[7]["kind"] == "ExplicitSync" and rows[7]["ratio"] == 2.0, rows[7]
    assert S["pair_check"]["sync"] == {"real": 1, "static": 1, "paired": 1, "kind_pairs": {"ExplicitSync->Cluster": 1}, "dur_rank_corr": None}, S["pair_check"]["sync"]
    assert S["sum_real"]["sync"] == 100 and S["engine_overlap"]["neither"] == 100 and S["total_real"] == 1500 and S["cycles_line_matches"] is True, S
    assert S["sync_by_kind"] == {"Cluster": {"n": 1, "sum": 100}} and S["tu_split"]["main"]["n"] + S["tu_split"]["sub"]["n"] + S["tu_split"]["unjoined"]["n"] == 5 and S["tu_split"]["unjoined"]["n"] == 1 and set(S["tu_split"]["sub"]["by_kind"]) <= {"sub_vrf", "sub_tab"}, S["tu_split"]
    md = open(os.path.join(t, "out", "timeline.md")).read(); assert "### sliding_project_qkv" in md and "| order | span |" in md and "kind pairs" in md and "unjoined spans 1" in md
    assert not parse_log("==> x\n    cycles=5\n")[0]
    # 5b. the runtime's `Core` span is a sync program too (qkv traces): it pairs with an ExplicitSync program in static order
    #     ahead of the final `Cluster`, instead of staying unjoined as family `other` while the Cluster span takes the FIRST program
    S, rows, un = case(["DMA", "Renegade::TuExec", "DMA", "Core", "Cluster"], sched=dict(SELFTEST_SCHED, instructions=SELFTEST_SCHED["instructions"] + [I("ExplicitSync(9)", "Core", "PeCore", 1000, 1050, [], [], ""), I("ExplicitSync(12)", "Core", "PeCore", 1100, 1150, [], [], "")]),
                       cycles=1400, extra=[(6100, 6200), (6300, 6500)])
    assert rows[3]["instr"] == "ExplicitSync(9)" and rows[3]["family"] == "sync" and rows[3]["real_kind"] == "Core" and rows[4]["instr"] == "ExplicitSync(12)" and rows[4]["real_kind"] == "Cluster" and not un, (rows[3], rows[4], un)
    assert S["pair_check"]["sync"]["kind_pairs"] == {"ExplicitSync->Cluster": 1, "ExplicitSync->Core": 1} and S["sync_by_kind"] == {"Cluster": {"n": 1, "sum": 200}, "Core": {"n": 1, "sum": 100}} and S["n_unjoined"] == 0 and S["sum_real"]["sync"] == 300, S["pair_check"]["sync"]
    # 6. --diff: parent = case 2 (spans 7, 28, 43); candidate = the same kernel with the second DMA (43) 100 cycles shorter, the Main
    #    (28) 50 cycles longer, a new instruction 44 (DmaLoad, no tensors) with its own span, and an instruction index shift (28 -> 29,
    #    43 -> 47: indexes and source lines are not part of the match key; (family, kind, call, bytes) is)
    def mkrun(name, names, sched, cycles, spans_):
        d = os.path.join(t, name); os.makedirs(os.path.join(d, "arena"), exist_ok=True); os.makedirs(os.path.join(d, "schedule"), exist_ok=True)
        json.dump(sched, open(os.path.join(d, "schedule", "sliding_project_qkv.json"), "w"))
        lines = [f"    span kernel=sliding_project_qkv name={n} begin={b} end={e}" for n, (b, e) in zip(names, spans_)]
        open(os.path.join(d, "arena", f"{name}.log"), "w").write(SELFTEST_HEAD.format(cycles=cycles) + "\n".join(lines) + "\n\nall 3 tests passed\n"); return d
    pd = mkrun("p", ["DMA", "Renegade::TuExec", "DMA"], SELFTEST_SCHED, 1000, [(5100, 5300), (5250, 5700), (5800, 6100)])
    cs = {"instructions": [SELFTEST_SCHED["instructions"][0], I("29", "Main", "MainContext", 100, 400, [10], [12], "--> src/a.rs:9:9\n\nmatmul\n.commit()"),
                           I("47", "DmaLoad", "DmaEngine", 400, 600, [13], [14], ""), I("44", "DmaLoad", "DmaEngine", 600, 700, [], [], ""), SELFTEST_SCHED["instructions"][4]], "tensors": SELFTEST_SCHED["tensors"]}
    cd = mkrun("c", ["DMA", "Renegade::TuExec", "DMA", "DMA"], cs, 1050, [(5100, 5300), (5250, 5750), (5800, 6000), (6050, 6150)])
    assert diff(pd, cd, os.path.join(t, "c"), 25, {"43"}) == 0
    Dj = json.load(open(os.path.join(t, "c", "span_deltas.json"))); Dk = Dj["kernels"]["sliding_project_qkv"]
    assert Dj["parent"]["arena_job"] == "p" and Dj["candidate"]["arena_job"] == "c" and Dk["summary"] == {"n_rows": {"parent": 3, "candidate": 4}, "n_matched": 3, "n_removed": 0, "n_added": 1, "trace_total_delta": 50, "net_span_delta": 50, "join_rule": {"parent": "order/type", "candidate": "order/type"}}, Dk["summary"]
    m = {(x["parent"]["instr"], x["candidate"]["instr"]): x for x in Dk["matched"]}
    assert set(m) == {("7", "7"), ("28", "29"), ("43", "47")}, set(m)
    assert m[("43", "47")]["delta"]["real_dur"] == -100 and m[("43", "47")]["key"] == {"family": "dma", "kind": "DmaLoad", "call": "", "bytes": 32768} and m[("43", "47")]["k"] == 0, m[("43", "47")]
    assert m[("28", "29")]["delta"]["real_dur"] == 50 and m[("28", "29")]["delta"]["ratio"] == round(500 / 300 - 450 / 300, 3) and m[("28", "29")]["key"]["call"] == ".commit()", m[("28", "29")]
    assert m[("7", "7")]["delta"]["real_dur"] == 0 and m[("7", "7")]["delta"]["gap"] is None
    assert Dk["added"][0]["candidate"]["instr"] == "44" and Dk["added"][0]["key"] == {"family": "dma", "kind": "DmaLoad", "call": "", "bytes": None} and Dk["added"][0]["candidate"]["real_dur"] == 100, Dk["added"]
    assert Dk["targets"] == [m[("43", "47")]] and Dk["totals"]["total_real"] == {"parent": 1000, "candidate": 1050, "delta": 50} and Dk["totals"]["static_makespan"]["delta"] == 100
    assert Dk["family_delta"]["dma"] == {"matched_rows": 2, "sum_matched_delta": -100, "removed_rows": 0, "sum_removed": 0, "added_rows": 1, "sum_added": 100, "net_span_delta": 0}, Dk["family_delta"]["dma"]
    assert Dk["family_delta"]["tu"]["net_span_delta"] == 50 and Dk["family_delta"]["all"]["net_span_delta"] == 50
    dmd = open(os.path.join(t, "c", "span_deltas.md")).read(); assert "Targeted rows" in dmd and "| dma | DmaLoad |" in dmd and "Added rows (candidate only, 1" in dmd
    tjp = os.path.join(pd, "spans", "timeline.json"); old = json.load(open(tjp)); old["spans_version"] = 1; json.dump(old, open(tjp, "w"))
    assert load_timeline(pd)["spans_version"] == VERSION and json.load(open(tjp))["spans_version"] == VERSION  # a timeline of another device_timeline.py version is rebuilt from its log
    print("device_timeline.py selftest ok:", t)

def load_timeline(d, top=25):
    """spans/timeline.json of an `ec2_run.py --out` dir, built from its newest arena/*.log when missing or written by another device_timeline.py
    version (spans_version != VERSION; without a log the old timeline stands)."""
    tj = os.path.join(d, "spans", "timeline.json")
    cur = json.load(open(tj)) if os.path.exists(tj) else None
    if cur is None or cur.get("spans_version") != VERSION:
        logs = sorted(glob.glob(os.path.join(d, "arena", "*.log")), key=os.path.getmtime)
        if not logs:
            if cur is not None: return cur
            raise SystemExit(f"device_timeline.py --diff: no arena/*.log under {d}")
        if run(logs[-1], os.path.join(d, "schedule"), os.path.join(d, "spans"), top) != 0: raise SystemExit(f"device_timeline.py --diff: no span lines in {logs[-1]}")
    return json.load(open(tj))

def row_key(r):
    """Match key of a timeline row across two runs: (family, kind, call text, bytes); an unjoined span keys on its name."""
    if r.get("instr") is None: return (r["family"], r["real_kind"], None, None)
    return (r["family"], r["kind"], r.get("call") or "", r.get("bytes"))

def static_order(r): return (r["static_begin"] if r.get("static_begin") is not None else -1, r["real_begin"])
SIDE = ("instr", "src", "static_begin", "static_dur", "real_begin", "real_end", "real_dur", "ratio", "gap", "bytes", "b_per_cycle", "order")

def side(r): return {f: r.get(f) for f in SIDE}

def d_(a, b):
    return None if a is None or b is None else (round(b - a, 3) if isinstance(a, float) or isinstance(b, float) else b - a)

def diff_rows(P, C):
    """-> (matched, removed, added). Inside one key group the k-th parent row by static begin pairs with the k-th candidate row."""
    gp, gc = {}, {}
    for r in sorted(P, key=static_order): gp.setdefault(row_key(r), []).append(r)
    for r in sorted(C, key=static_order): gc.setdefault(row_key(r), []).append(r)
    matched, removed, added = [], [], []
    for k in sorted(set(gp) | set(gc), key=str):
        a, b = gp.get(k, []), gc.get(k, [])
        for i in range(min(len(a), len(b))):
            p, c = a[i], b[i]
            matched.append({"key": {"family": k[0], "kind": k[1], "call": k[2], "bytes": k[3]}, "k": i, "group_size": {"parent": len(a), "candidate": len(b)},
                            "parent": side(p), "candidate": side(c),
                            "delta": {f: d_(p.get(f), c.get(f)) for f in ("real_dur", "static_dur", "ratio", "gap", "real_begin", "b_per_cycle")}})
        removed += [{"key": {"family": k[0], "kind": k[1], "call": k[2], "bytes": k[3]}, "k": len(b) + j, "parent": side(r)} for j, r in enumerate(a[len(b):])]
        added += [{"key": {"family": k[0], "kind": k[1], "call": k[2], "bytes": k[3]}, "k": len(a) + j, "candidate": side(r)} for j, r in enumerate(b[len(a):])]
    return matched, removed, added

TOTALS = (("total_real", "total real (max end - min begin)"), ("cycles_line", "harness cycles="), ("static_makespan", "static makespan"),
          ("real_static_ratio", "real / static"), ("union_busy", "union busy"), ("total_idle", "total idle"), ("dma_b_per_cycle", "DMA B/cycle (rows with bytes)"))

def diff_kernel(k, Pk, Ck, targets):
    Sp, Sc = Pk["summary"], Ck["summary"]
    matched, removed, added = diff_rows(Pk["rows"], Ck["rows"])
    tot = {key: {"parent": Sp.get(key), "candidate": Sc.get(key), "delta": d_(Sp.get(key), Sc.get(key))} for key, _ in TOTALS}
    for f in FAMS:
        tot[f"sum_real.{f}"] = {"parent": Sp["sum_real"][f], "candidate": Sc["sum_real"][f], "delta": Sc["sum_real"][f] - Sp["sum_real"][f]}
        tot[f"union_real.{f}"] = {"parent": Sp["union_real"][f], "candidate": Sc["union_real"][f], "delta": Sc["union_real"][f] - Sp["union_real"][f]}
    for f in ("dma_only", "tu_only", "both", "neither"):
        tot[f"engine_overlap.{f}"] = {"parent": Sp["engine_overlap"][f], "candidate": Sc["engine_overlap"][f], "delta": Sc["engine_overlap"][f] - Sp["engine_overlap"][f]}
    fam = {}
    for f in FAMS:
        m = sum(x["delta"]["real_dur"] for x in matched if x["key"]["family"] == f)
        rm = sum(x["parent"]["real_dur"] for x in removed if x["key"]["family"] == f); ad = sum(x["candidate"]["real_dur"] for x in added if x["key"]["family"] == f)
        fam[f] = {"matched_rows": sum(1 for x in matched if x["key"]["family"] == f), "sum_matched_delta": m, "removed_rows": sum(1 for x in removed if x["key"]["family"] == f),
                  "sum_removed": rm, "added_rows": sum(1 for x in added if x["key"]["family"] == f), "sum_added": ad, "net_span_delta": m + ad - rm}
    fam["all"] = {kk: sum(fam[f][kk] for f in FAMS) for kk in fam["dma"]}
    tg = [x for x in matched if str(x["parent"]["instr"]) in targets] + [x for x in removed if str(x["parent"]["instr"]) in targets]
    return {"summary": {"n_rows": {"parent": len(Pk["rows"]), "candidate": len(Ck["rows"])}, "n_matched": len(matched), "n_removed": len(removed), "n_added": len(added),
                        "trace_total_delta": d_(Sp.get("total_real"), Sc.get("total_real")), "net_span_delta": fam["all"]["net_span_delta"], "join_rule": {"parent": Sp["join_rule"], "candidate": Sc["join_rule"]}},
            "totals": tot, "family_delta": fam, "targets": tg, "matched": matched, "removed": removed, "added": added}

def md_row(x):
    p, c = x.get("parent") or {}, x.get("candidate") or {}; kd = x["key"]; d = x.get("delta") or {}
    return (f"| {kd['family']} | {kd['kind']} | `{(kd['call'] or '')[:40]}` | {fmt(kd['bytes'])} | {x['k']} | {p.get('instr') or '-'} / {c.get('instr') or '-'} | "
            f"{p.get('src') or '-'} / {c.get('src') or '-'} | {fmt(p.get('real_begin'))} / {fmt(c.get('real_begin'))} | {fmt(p.get('real_dur'))} / {fmt(c.get('real_dur'))} | "
            f"{fmt(d.get('real_dur'))} | {fmt(p.get('ratio'))} / {fmt(c.get('ratio'))} | {fmt(p.get('gap'))} / {fmt(c.get('gap'))} | {fmt(d.get('gap'))} | {fmt(p.get('b_per_cycle'))} / {fmt(c.get('b_per_cycle'))} |")
MD_HEAD = ["| family | kind | call | bytes | k | instr p / c | src p / c | real begin p / c | real dur p / c | delta dur | ratio p / c | gap p / c | delta gap | B/cycle p / c |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

def diff(pdir, cdir, out, top, targets, only=None):
    P, C = load_timeline(pdir, top), load_timeline(cdir, top)
    job = lambda T: os.path.basename(T.get("log", "")).split(".")[0]
    res = {"parent": {"dir": rel(pdir), "log": P.get("log"), "arena_job": job(P)}, "candidate": {"dir": rel(cdir), "log": C.get("log"), "arena_job": job(C)},
           "match_rule": "rows match on (family, kind, call text, bytes); k-th parent row by static begin <-> k-th candidate row; surplus = removed / added; --target = parent instruction indexes",
           "targets": sorted(targets), "kernels": {}}
    md = ["# Span deltas: candidate minus parent", "", f"parent `{rel(pdir)}` (job {job(P)}, log `{P.get('log')}`); candidate `{rel(cdir)}` (job {job(C)}, log `{C.get('log')}`). {res['match_rule']}.", ""]
    for k in K3 + sorted(x for x in C.get("kernels", {}) if x not in K3):
        if k not in P.get("kernels", {}) or k not in C.get("kernels", {}) or (only and k != only): continue
        D = diff_kernel(k, P["kernels"][k], C["kernels"][k], targets); res["kernels"][k] = D; S = D["summary"]; T = D["totals"]; F = D["family_delta"]
        md += [f"## {k}", "", f"rows parent {S['n_rows']['parent']} / candidate {S['n_rows']['candidate']}: matched {S['n_matched']}, removed {S['n_removed']}, added {S['n_added']}; "
               f"trace-level total delta {fmt(S['trace_total_delta'])}; net span delta (matched + added - removed, overlapping spans) {fmt(S['net_span_delta'])}.", "",
               "| metric | parent | candidate | delta |", "|---|---|---|---|"]
        for key, label in TOTALS: md.append(f"| {label} | {fmt(T[key]['parent'])} | {fmt(T[key]['candidate'])} | {fmt(T[key]['delta'])} |")
        for f in ("dma", "tu", "sync"):
            md.append(f"| sum real {f} / union {f} | {fmt(T[f'sum_real.{f}']['parent'])} / {fmt(T[f'union_real.{f}']['parent'])} | {fmt(T[f'sum_real.{f}']['candidate'])} / {fmt(T[f'union_real.{f}']['candidate'])} | {fmt(T[f'sum_real.{f}']['delta'])} / {fmt(T[f'union_real.{f}']['delta'])} |")
        for f in ("dma_only", "tu_only", "both", "neither"): md.append(f"| engine overlap {f} | {fmt(T[f'engine_overlap.{f}']['parent'])} | {fmt(T[f'engine_overlap.{f}']['candidate'])} | {fmt(T[f'engine_overlap.{f}']['delta'])} |")
        md += ["", "| family | matched rows | sum matched delta | removed rows | sum removed | added rows | sum added | net span delta |", "|---|---|---|---|---|---|---|---|"]
        for f in ("dma", "tu", "sync", "other", "all"):
            x = F[f]; md.append(f"| {f} | {x['matched_rows']} | {fmt(x['sum_matched_delta'])} | {x['removed_rows']} | {fmt(x['sum_removed'])} | {x['added_rows']} | {fmt(x['sum_added'])} | {fmt(x['net_span_delta'])} |")
        if targets:
            md += ["", f"Targeted rows (parent instruction {', '.join(sorted(targets))}; a removed row has no candidate side):", ""] + MD_HEAD + [md_row(x) for x in D["targets"]]
        md += ["", f"Top {min(top, len(D['matched']))} matched rows by |delta real dur|:", ""] + MD_HEAD
        md += [md_row(x) for x in sorted(D["matched"], key=lambda x: -abs(x["delta"]["real_dur"]))[:top]]
        md += ["", f"Removed rows (parent only, {len(D['removed'])}; first {min(top, len(D['removed']))} by real dur):", ""] + MD_HEAD + [md_row(x) for x in sorted(D["removed"], key=lambda x: -x["parent"]["real_dur"])[:top]]
        md += ["", f"Added rows (candidate only, {len(D['added'])}; first {min(top, len(D['added']))} by real dur):", ""] + MD_HEAD + [md_row(x) for x in sorted(D["added"], key=lambda x: -x["candidate"]["real_dur"])[:top]] + [""]
        print(f"{k:28s} matched={S['n_matched']} removed={S['n_removed']} added={S['n_added']} total_real {fmt(T['total_real']['parent'])} -> {fmt(T['total_real']['candidate'])} ({fmt(S['trace_total_delta'])}) net_span_delta={fmt(S['net_span_delta'])} dma {fmt(F['dma']['net_span_delta'])} tu {fmt(F['tu']['net_span_delta'])} sync {fmt(F['sync']['net_span_delta'])}")
    if not res["kernels"]: print("device_timeline.py --diff: no kernel present in both runs"); return 1
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "span_deltas.json"), "w").write(sanitize(json.dumps(res, indent=1)))
    open(os.path.join(out, "span_deltas.md"), "w").write(sanitize("\n".join(md) + "\n"))
    print("out:", os.path.join(out, "span_deltas.json")); return 0

def main():
    if "--selftest" in sys.argv[1:]: selftest(); return 0
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("run_dir", nargs="?"); ap.add_argument("--log"); ap.add_argument("--schedule-dir"); ap.add_argument("--out"); ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--diff", nargs=2, metavar=("PARENT_DIR", "CANDIDATE_DIR")); ap.add_argument("--kernel"); ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--tid", type=int, help="one cluster's rows only (tid 0 / 1 of the 0.8.1 harness); same as SPANS_TID")
    a = ap.parse_args()
    if a.tid is not None:
        global SPAN_TID; SPAN_TID = a.tid
    if a.diff: return diff(a.diff[0], a.diff[1], a.out or a.diff[1], a.top, set(a.target), a.kernel)
    if a.run_dir:
        logs = sorted(glob.glob(os.path.join(a.run_dir, "arena", "*.log")), key=os.path.getmtime)
        if not logs: print(f"device_timeline.py: no arena/*.log under {a.run_dir}", file=sys.stderr); return 2
        logp, sd = logs[-1], os.path.join(a.run_dir, "schedule"); out = a.out or os.path.join(a.run_dir, "spans")
    elif a.log and a.schedule_dir: logp, sd = a.log, a.schedule_dir; out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.log)), "..", "spans")
    else: print(__doc__.split("\n\n")[0] + "\n" + __doc__.split("\n")[2], file=sys.stderr); return 2
    return run(logp, sd, os.path.normpath(out), a.top)

if __name__ == "__main__": sys.exit(main())
