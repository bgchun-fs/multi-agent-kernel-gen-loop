#!/usr/bin/env python3
"""static_schedule.py: the headless reader of a `cargo furiosa-opt compile --dump-schedule` JSON (the book's diagnosis protocol without the viewer).

  python3 profiler/static_schedule.py critical_path <k>.json [--top N] [--json]   # the static critical path walked BACK from the makespan row: each step names its predecessor as
                                                                    #   dependency (a producer of one of my input tensors ends at my begin), resource (a row on one of my
                                                                    #   contexts ends at my begin) or gap (idle cycles before my begin: the scheduler had nothing ready)
  python3 profiler/static_schedule.py footprint <k>.json [--json]            # DM footprint over time per buffer_type from tensors[].address / size / lifetime: peak bytes + cycle, the
                                                                    #   5 largest Sram tensors and their overlap with the weight-matrix streams; the bank64 audit rides along
  python3 profiler/static_schedule.py weights <k>.json [--json]              # weight-matrix classification by tensor SIZE and ROLE (matrix = a DmaLoad of at least MATRIX_FRACTION of the
                                                                    #   largest weight load and >= MATRIX_ABS_MIN; grid = 1 MB .. that; the rest as fifo_order): replaces the 8 / 7 MB
                                                                    #   constant; fifo_order.py imports matrix_threshold()
  python3 profiler/static_schedule.py bank64 <k>.json [--json]               # (a) Main / Sub rows whose contexts carry DmaEngine (the compiler's fallback: a lost main / DMA overlap);
                                                                    #   (b) Sram tensors co-live in the same bank (address bits 3-6): the max co-live count per bank
  python3 profiler/static_schedule.py util <k>.json                          # one line: makespan, busy fraction per context, DMA bytes, rows, tensors
  python3 profiler/static_schedule.py --selftest                             # the three r9 head schedules (testdata/schedules081/r9 next to this file, when present) with their known numbers

Rows come from measured_rate_estimate.load_rows (the byte rules of profiler/device_timeline.py). Everything here is STATIC (the compiler's model); the device disagrees on
small rows; the reader's job is the FIRST gate before any Arena job: see the chain, the footprint and the front before spending a line
position. Exit 0 always for a readable schedule; 2 = usage / unreadable file."""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
MATRIX_FRACTION = 0.25      # a weight load of at least this fraction of the LARGEST DmaLoad is a matrix stream (ffn grids 3.69 MB / 29.5 MB = 0.125 stay grids; qkv Wk / Wv 7.86 / 15.7 = 0.5 are matrices)
MATRIX_ABS_MIN = 2_000_000  # and at least this many bytes (a tiny kernel's largest load is not a "matrix")
GRID_MIN = 1_000_000
BANK_SHIFT, BANK_MASK = 3, 0xF  # bank = address bits 3-6


def load(path):
    import measured_rate_estimate as SR
    rows, mk = SR.load_rows(path); d = json.load(open(path))
    tens = {}
    for t in d.get("tensors") or []:
        try: tens[int(t["index"])] = t
        except (KeyError, TypeError, ValueError): pass
    return rows, mk, tens, d


# ---------------------------------------------------------------- weights
def matrix_threshold(rows):
    """The byte threshold above which a DmaLoad is a matrix stream for THIS schedule: MATRIX_FRACTION x the largest load, at least MATRIX_ABS_MIN; None without a load."""
    loads = [r.get("bytes") for r in rows if r.get("tpe") == "DmaLoad" and isinstance(r.get("bytes"), int)]
    if not loads: return None
    return max(MATRIX_ABS_MIN, int(max(loads) * MATRIX_FRACTION))


def weight_class(r, mm):
    b = r.get("bytes"); call = r.get("call") or ""; tpe = r.get("tpe") or ""
    if tpe in ("DmaStore", "DmaStos"): return "store"
    if isinstance(b, int) and mm and b >= mm: return "matrix"
    if isinstance(b, int) and b >= GRID_MIN: return "grid"
    if b is None and "to_dm_view" in call: return "stripe"
    if b is None and not call: return "staging"
    return "small"


def weights(path):
    rows, mk, tens, _ = load(path); mm = matrix_threshold(rows); out = []; n = 0
    for r in sorted([r for r in rows if r.get("family") == "dma"], key=lambda r: (r["static_begin"], r["pos"])):
        c = weight_class(r, mm); n += c == "matrix"
        out.append({"pos": r["pos"], "class": c, "name": f"m{n}" if c == "matrix" else (r.get("dm_tensor") or "-"), "bytes": r.get("bytes"), "static_begin": r["static_begin"], "static_dur": r["static_dur"], "src": r.get("src"), "role": role_of(r)})
    return {"matrix_threshold": mm, "matrices": [w for w in out if w["class"] == "matrix"], "rows": out, "makespan": mk}


def role_of(r):
    """weight (a load of >= GRID_MIN bytes: the matrix streams and the scale grids), activation (x / residual / rms / norm / hidden tokens), scale (small described
    loads of a scale), staging (the compiler's table), store, other."""
    import re as _re
    if r.get("tpe") in ("DmaStore", "DmaStos"): return "store"
    if r.get("bytes") is None and not r.get("call"): return "staging"
    if isinstance(r.get("bytes"), int) and r["bytes"] >= GRID_MIN: return "weight"
    toks = set(_re.split(r"[^a-z0-9]+", ((r.get("dm_tensor") or "") + " " + (r.get("call") or "")).lower())) - {""}
    if toks & {"x", "residual", "rms", "rms_weight", "norm", "hidden", "normalized"}: return "activation"
    if any("scale" in t for t in toks): return "scale"
    return "other"


# ---------------------------------------------------------------- critical path
def critical_path(path, top=200):
    """Walk back from the row that ends the schedule. -> {makespan, steps [{pos, tpe, ctx, begin, end, dur, src, pred: dependency | resource | gap | start, via, gap}], classes {..}}"""
    rows, mk, tens, _ = load(path)
    producer = {}
    for r in rows:
        for t in r.get("out") or []: producer.setdefault(t, []).append(r)
    by_ctx = {}
    for r in rows:
        for c in (r.get("ctx") or "").split(","):
            if c: by_ctx.setdefault(c, []).append(r)
    cur = max(rows, key=lambda r: (r["static_end"], r["static_begin"])); steps = []; seen = set()
    while cur is not None and len(steps) < top and cur["pos"] not in seen:
        seen.add(cur["pos"]); b = cur["static_begin"]
        step = {"pos": cur["pos"], "tpe": cur["tpe"], "ctx": cur.get("ctx"), "begin": b, "end": cur["static_end"], "dur": cur["static_dur"], "src": cur.get("src"), "call": (cur.get("call") or "")[:60], "bytes": cur.get("bytes")}
        if b <= 0: step["pred"] = "start"; steps.append(step); break
        deps = [p for t in (cur.get("in") or []) for p in producer.get(t, []) if p["static_end"] <= b and p["pos"] != cur["pos"]]
        dep = max(deps, key=lambda p: p["static_end"]) if deps else None
        res = None
        for c in (cur.get("ctx") or "").split(","):
            for p in by_ctx.get(c, []):
                if p["pos"] != cur["pos"] and p["static_end"] <= b and p["static_begin"] < b and (res is None or p["static_end"] > res["static_end"]): res = p
        if dep is not None and dep["static_end"] == b: step["pred"] = "dependency"; nxt = dep
        elif res is not None and res["static_end"] == b: step["pred"] = "resource"; nxt = res
        else:
            cands = [p for p in (dep, res) if p is not None]; nxt = max(cands, key=lambda p: p["static_end"]) if cands else None
            step["pred"] = "gap"; step["gap"] = b - (nxt["static_end"] if nxt else 0)
        if nxt is not None: step["via"] = {"pos": nxt["pos"], "tpe": nxt["tpe"], "end": nxt["static_end"], "src": nxt.get("src")}
        steps.append(step); cur = nxt
    classes = {}
    for s in steps: classes[s["pred"]] = classes.get(s["pred"], 0) + 1
    gap_total = sum(s.get("gap", 0) for s in steps)
    return {"makespan": mk, "steps": steps, "classes": classes, "gap_cycles": gap_total, "chain_rows": len(steps)}


def fmt_critpath(cp):
    out = [f"makespan {cp['makespan']:,}: {cp['chain_rows']} rows on the chain, {cp['classes']} , idle {cp['gap_cycles']:,} cycles"]
    for s in cp["steps"]:
        via = s.get("via"); v = f" <- {s['pred']}" + (f" of {via['tpe']}#{via['pos']} (ends {via['end']:,}, {via.get('src') or '-'})" if via else "") + (f" gap {s['gap']:,}" if s.get("gap") else "")
        out.append(f"{s['begin']:>8}-{s['end']:<8} {s['tpe']:<9} {s['ctx'] or '-':<26} {s['dur']:>7} {s.get('src') or '-'}{v}")
    return "\n".join(out)


# ---------------------------------------------------------------- footprint + bank64
def footprint(path, top=5):
    rows, mk, tens, d = load(path); res = {"makespan": mk, "total_memory": d.get("total_memory"), "page_size": d.get("page_size"), "buffers": {}}
    mm = matrix_threshold(rows); streams = [(r["static_begin"], r["static_end"], r["pos"]) for r in rows if r.get("tpe") == "DmaLoad" and isinstance(r.get("bytes"), int) and mm and r["bytes"] >= mm]
    by_type = {}
    for t in tens.values():
        bt = str(t.get("buffer_type")); lt = t.get("lifetime") or {}; sz = int(t.get("size") or 0)
        if sz <= 0: continue
        by_type.setdefault(bt, []).append((int(lt.get("begin", 0)), int(lt.get("end", 0)), sz, t))
    for bt, items in by_type.items():
        ev = []
        for b, e, sz, t in items: ev.append((b, sz)); ev.append((max(e, b), -sz))
        ev.sort(key=lambda x: (x[0], x[1])); cur = peak = 0; peak_at = 0
        for c, dsz in ev:
            cur += dsz
            if cur > peak: peak, peak_at = cur, c
        big = sorted(items, key=lambda x: -x[2])[:top]
        entry = {"peak_bytes": peak, "peak_cycle": peak_at, "tensors": len(items), "largest": []}
        for b, e, sz, t in big:
            ov = [p for sb, se, p in streams if sb < e and b < se]
            entry["largest"].append({"name": t.get("name"), "size": sz, "begin": b, "end": e, "address": t.get("address"), "overlaps_matrix_rows": ov})
        res["buffers"][bt] = entry
    res["bank64"] = bank64(rows, tens)
    return res


def bank64(rows, tens):
    """(a) Main / Sub rows on the DmaEngine context; (b) Sram tensors co-live in one bank (address bits 3-6): max co-live count per bank and the worst bank."""
    a = [{"pos": r["pos"], "tpe": r["tpe"], "ctx": r.get("ctx"), "begin": r["static_begin"], "src": r.get("src")} for r in rows if r.get("tpe") in ("Main", "Sub") and "DmaEngine" in (r.get("ctx") or "")]
    sram = [(int((t.get("lifetime") or {}).get("begin", 0)), int((t.get("lifetime") or {}).get("end", 0)), t) for t in tens.values() if str(t.get("buffer_type")) == "Sram" and isinstance(t.get("address"), int)]
    banks = {}
    for b, e, t in sram: banks.setdefault((int(t["address"]) >> BANK_SHIFT) & BANK_MASK, []).append((b, max(e, b + 1), t))
    worst = {"bank": None, "colive": 0}
    per = {}
    for bk, items in banks.items():
        ev = sorted([(b, 1) for b, e, t in items] + [(e, -1) for b, e, t in items], key=lambda x: (x[0], x[1])); cur = mx = 0
        for c, dz in ev:
            cur += dz; mx = max(mx, cur)
        per[bk] = {"tensors": len(items), "max_colive": mx}
        if mx > worst["colive"]: worst = {"bank": bk, "colive": mx}
    return {"main_sub_on_dma_engine": a, "flag_a": bool(a), "banks": {str(k): v for k, v in sorted(per.items())}, "worst": worst, "flag_colive": worst["colive"] >= 4}


def fmt_footprint(fp):
    out = [f"makespan {fp['makespan']:,}; total_memory {fp.get('total_memory')}; page_size {fp.get('page_size')}"]
    for bt, e in sorted(fp["buffers"].items()):
        out.append(f"{bt:<5} peak {e['peak_bytes']:>12,} B at cycle {e['peak_cycle']:,} ({e['tensors']} tensors)")
        for L in e["largest"]: out.append(f"      {L['size']:>10,} B  {L['begin']:>8,}-{L['end']:<8,} addr {L['address']}  {L['name'] or '-'}" + (f"  overlaps matrix rows {L['overlaps_matrix_rows']}" if L["overlaps_matrix_rows"] else ""))
    b = fp["bank64"]; out.append(f"bank64: (a) Main/Sub rows on DmaEngine {len(b['main_sub_on_dma_engine'])}{' FLAG' if b['flag_a'] else ''}; (b) worst Sram bank {b['worst']['bank']} co-live {b['worst']['colive']}{' FLAG' if b['flag_colive'] else ''}")
    return "\n".join(out)


def util(path):
    rows, mk, tens, _ = load(path); busy = {}
    for r in rows:
        for c in (r.get("ctx") or "").split(","):
            if c and c != "Noop": busy[c] = busy.get(c, 0) + r["static_dur"]
    dma_b = sum(r.get("bytes") or 0 for r in rows if r.get("family") == "dma")
    parts = ", ".join(f"{c} {100.0 * v / mk:.0f} %" for c, v in sorted(busy.items(), key=lambda kv: -kv[1])) if mk else "-"
    return f"util.* makespan {mk:,} | busy: {parts} | DMA {dma_b:,} B in {sum(1 for r in rows if r.get('family') == 'dma')} rows | {len(rows)} rows, {len(tens)} tensors"


# ---------------------------------------------------------------- selftest

# ---------------------------------------------------------------- warnings (information only): rules M1 / M2 of the qkv two-term frame
VRF_CACHE_BYTES = 1024           # 0.8.1: VRF operands above about 2 x this poison later VRF operands of the slice
M1_LIMIT_BYTES = 2 * VRF_CACHE_BYTES
M4_LOW_BYTES = 4352              # an open device record: an operand DM buffer landing wholly below this per-slice address reads wrong in the two-term trees
M4_MIN_PER_SLICE = 2048          # the operand buffers are 4,096 B per slice (eight 512 B rows); smaller landings (the x load 512 B, the rope index 32 B) are not operand buffers
M2_SMALL_BYTES = 65536           # a "small operand row": a DMA load under this (or without a byte count) scheduled behind m2 reads as f8 bytes
_EXT = re.compile(r"%\s*(\d+)|Dummy(\d+)|#\s*(\d+)")


def _extent(field):
    """The element count of one MappingShape field: the product of its `% N`, `DummyN` and `# N` numbers (1 when none)."""
    n = 1
    for m in _EXT.finditer(field or ""):
        n *= int(next(g for g in m.groups() if g))
    return n


def vrf_per_slice(t):
    """(per-slice bytes, cluster extent, slice extent) of a Vrf tensor from its MappingShape[Chip: .., Cluster: .., Slice: .., Element: ..] and size."""
    sh = str(t.get("shape") or ""); f = {}
    for m in re.finditer(r"(Chip|Cluster|Slice|Element):\s*(.*?)(?=,\s*(?:Chip|Cluster|Slice|Element):|\]$)", sh): f[m.group(1)] = m.group(2)
    cl, sl = max(1, _extent(f.get("Cluster"))), max(1, _extent(f.get("Slice"))); size = int(t.get("size") or 0)
    return size // (cl * sl), cl, sl


def is_qkv(path, d):
    return "sliding_project_qkv" in os.path.basename(path) or "sliding_project_qkv" in str(d.get("title") or d.get("name") or "")


_DEST = re.compile(r"to_dm_view\(\s*&mut\s*ctx\.\w+\s*,\s*(\w+)")


def dm_copies(d):
    """'; DM->DM copies: sin -> a [5500-6041], cos -> a [..]' for the DmaStos rows of a schedule (the RoPE cos / sin gather images copied into the
    operand buffers), from each row's description (`<src>.view().to_dm_view(&mut ctx.tdma, <dest>.view_mut()...`); '' without one. Read with M4 so the
    dead-form call is one read: where the copies land, and when, next to the buffer address."""
    items = []
    for i in d.get("instructions") or []:
        if i.get("tpe") != "DmaStos": continue
        desc = str(i.get("description") or ""); body = desc.split("\n", 2)[-1] if "\n" in desc else desc
        src = body.split(".view()")[0].strip().split()[-1] if ".view()" in body else "?"
        m = _DEST.search(body); lt = i.get("lifetime") or {}
        items.append(f"{src} -> {m.group(1) if m else '?'} [{lt.get('begin', '?')}-{lt.get('end', '?')}]")
    return "; DM->DM copies: " + ", ".join(items) if items else ""


def warnings(path, all_kernels=False):
    """[str] M1 / M2 lines for a schedule (empty = none); information only. Both laws were proven on the qkv two-term trees, so
    they apply to sliding_project_qkv schedules unless all_kernels (the attn_out / ffn heads lift 15-16 KB VRF operands and pass: the law is not general).
    M1: a Vrf operand whose bytes (size over the Cluster x Slice extents = the Element extent x 4 B) exceed M1_LIMIT_BYTES (2 x VRF_CACHE_BYTES).
    M2: a small DMA load (< M2_SMALL_BYTES or byte-less) whose static begin lies behind m2 (the second matrix load)."""
    rows, mk, tens, d = load(path); out = []
    if not all_kernels and not is_qkv(path, d): return out
    for i, t in sorted(tens.items()):
        if str(t.get("buffer_type") or "").lower() != "vrf" or "Slice:" not in str(t.get("shape") or ""): continue  # a Vrf entry without a mapping shape is a region, not an operand lift
        per, cl, sl = vrf_per_slice(t)
        if per > M1_LIMIT_BYTES: out.append(f"M1: Vrf tensor {i} {t.get('name') or '?'}: {per:,} B per slice (> {M1_LIMIT_BYTES:,}; size {int(t.get('size') or 0):,} over {cl} x {sl} slices): a VRF operand lift above 2 KB poisons later VRF operands of the slice (ED102)")
    low = {}
    for i, t in sorted(tens.items()):  # M4: an operand DM buffer (>= M4_MIN_PER_SLICE per slice) landing wholly below M4_LOW_BYTES per slice
        if str(t.get("buffer_type") or "") != "Sram" or not isinstance(t.get("address"), int) or "Slice:" not in str(t.get("shape") or ""): continue
        per, cl, sl = vrf_per_slice(t)
        if per >= M4_MIN_PER_SLICE and t["address"] + per <= M4_LOW_BYTES: low.setdefault((t["address"], per), []).append(str(t.get("name") or i))
    cp = dm_copies(d) if low else ""
    for (addr, per), names in sorted(low.items()):
        nm = [n for n in names if not n.startswith("vISA(")] or names  # the buffer's own name first (operands_q), the vISA views of it after
        out.append(f"M4: operand buffer {', '.join(nm[:3])}{' (+%d views)' % (len(names) - len(nm[:3])) if len(names) > len(nm[:3]) else ''} lands at [{addr:,}, {addr + per:,}) per slice (below {M4_LOW_BYTES:,}): the two-term trees whose A / B operand buffers landed there read wrong (ED111 open record; SEPNL / UP-3T), the right-reading trees sit at B [8,960, 13,056) / A [13,056, 17,152) or r10 B [1,280, 5,376) / A [5,376, 9,472){cp}")
    w = weights(path); m2 = w["matrices"][1]["static_begin"] if len(w["matrices"]) > 1 else None
    if m2 is not None:
        for r in rows:
            if r.get("family") != "dma" or r.get("tpe") != "DmaLoad" or r["static_begin"] <= m2: continue
            b = r.get("bytes")
            if b is None or b < M2_SMALL_BYTES: out.append(f"M2: small operand row pos {r['pos']} ({'no byte count' if b is None else f'{b:,} B'}) at static {r['static_begin']:,} behind m2 @{m2:,} ({r.get('src') or '?'}): reads as f8 bytes on the device (ED103); keep every small row in the gate's version chain in front of m1")
    return out

def selftest():
    # the inline checks need no files; the schedule checks read compiler schedules of the competition kernels,
    # which this release does not ship, so they run only when testdata/schedules081/ is present
    assert vrf_per_slice({"shape": "MappingShape[Chip: 1, Cluster: Qs / 2048 % 2, Slice: (Qs / 512 % 4, 1 # 64), Element: Qs % 512]", "size": 1048576}) == (2048, 2, 256)
    assert _extent("Dummy256") == 256 and _extent("Ns / 4 % 2") == 2 and _extent("(Ns % 4, 1 # 64)") == 256 and _extent("1") == 1
    if not os.path.isdir(os.path.join(HERE, "testdata", "schedules081")):
        print("static_schedule selftest ok (inline checks; schedule fixtures not present, file checks skipped)")
        return 0
    lanes = os.path.join(HERE, "testdata", "schedules081", "lanes")
    w_sep = warnings(os.path.join(lanes, "2T-SEPNL-sliding_project_qkv.json")); w_oct = warnings(os.path.join(lanes, "2T-OCTET-sliding_project_qkv.json"))
    assert len([w for w in w_sep if w.startswith("M1")]) >= 4 and "7,680 B per slice" in w_sep[0] and len([w for w in w_sep if w.startswith("M2")]) == 2 and "pos 207" in " ".join(w_sep) and "pos 208" in " ".join(w_sep), w_sep  # x_hi / weight lifts 7,680 B per slice; two byte-less rows behind m2
    assert w_oct == [], w_oct  # no VRF operand above 1,920 B, nothing behind m2, operand buffers above 4,352
    m4 = [w for w in w_sep if w.startswith("M4")]; assert len(m4) >= 1 and "operands_q" in " ".join(m4) and "[0, 4,096)" in m4[0], m4  # operands_q / operands_kv_a at [0, 4,096)
    assert "DM->DM copies: sin -> a [5500-6041]" in m4[0] and "cos -> " in m4[0], m4[0][-160:]  # the RoPE copy destinations ride on the M4 line
    assert warnings(os.path.join(lanes, "2T-BOTHMAIN-sliding_project_qkv.json")) == [], "BOTHMAIN 72316: the right-reading parent (A / B at 8,960 / 13,056) is clean on M1 / M2 / M4"
    assert vrf_per_slice({"shape": "MappingShape[Chip: 1, Cluster: Qs / 2048 % 2, Slice: (Qs / 512 % 4, 1 # 64), Element: Qs % 512]", "size": 1048576}) == (2048, 2, 256)
    assert _extent("Dummy256") == 256 and _extent("Ns / 4 % 2") == 2 and _extent("(Ns % 4, 1 # 64)") == 256 and _extent("1") == 1
    assert warnings(os.path.join(HERE, "testdata", "schedules081", "r9", "sliding_attention_output.json")) == [] and warnings(os.path.join(HERE, "testdata", "schedules081", "r9", "sliding_project_qkv.json")) == [], "the heads are clean: qkv r9 has no lift above 2 KB; attn_out / ffn are out of the law's scope"
    assert len(warnings(os.path.join(HERE, "testdata", "schedules081", "r9", "sliding_attention_output.json"), all_kernels=True)) == 4, "--all prices the attn_out head by the qkv law (information only)"
    FX = os.path.join(HERE, "testdata", "schedules081", "r9")
    ao, qk, ff = (os.path.join(FX, f"{k}.json") for k in ("sliding_attention_output", "sliding_project_qkv", "decoder_feedforward"))
    # attn_out r9: the two Wo halves (7,864,320 B each) are m1 / m2 (threshold max(2 MB, 0.25 x 7.86 MB) = 2,000,000); the x load (262,144 B) is an activation; the chain ends at the final Core row
    w = weights(ao); assert w["makespan"] == 24358 and w["matrix_threshold"] == 2000000 and [m["bytes"] for m in w["matrices"]] == [7864320, 7864320] and [m["name"] for m in w["matrices"]] == ["m1", "m2"] and all(m["static_dur"] == 6966 and m["role"] == "weight" for m in w["matrices"]), w["matrices"]
    x = [r for r in w["rows"] if r["bytes"] == 262144][0]; assert x["class"] == "small" and x["static_dur"] == 933 and x["role"] == "activation", x
    cp = critical_path(ao); assert cp["makespan"] == 24358 and cp["steps"][0]["end"] == 24358 and cp["steps"][0]["tpe"] == "Core" and cp["steps"][-1]["pred"] in ("start", "gap") and "via" not in cp["steps"][-1] and cp["chain_rows"] >= 5, (cp["chain_rows"], cp["steps"][0], cp["steps"][-1])
    assert cp["steps"][-1]["bytes"] == 262144 and cp["steps"][-1]["begin"] == 1003, "the chain starts at the x load (262,144 B @1,003)"  # the head's first row waits for nothing: the launch gap
    assert set(cp["classes"]) <= {"dependency", "resource", "gap", "start"} and cp["gap_cycles"] >= 0 and any(s["tpe"] == "Core" and s["dur"] == 13913 for s in cp["steps"]) and cp["classes"].get("gap", 0) >= 1, (cp["classes"], [(s["tpe"], s["dur"]) for s in cp["steps"]])  # the long PeCore span (the contraction under the Wo stream) sits on the chain; the idle before the final Core row is the sync tail
    txt = fmt_critpath(cp); assert txt.startswith("makespan 24,358:") and "<- " in txt
    fp = footprint(ao); assert "Sram" in fp["buffers"] and 0 < fp["buffers"]["Sram"]["peak_bytes"] <= fp["total_memory"] and fp["buffers"]["Sram"]["largest"][0]["size"] >= fp["buffers"]["Sram"]["largest"][-1]["size"], fp["buffers"]["Sram"]
    assert fp["bank64"]["flag_a"] is False and fp["bank64"]["main_sub_on_dma_engine"] == [] and isinstance(fp["bank64"]["worst"]["colive"], int), fp["bank64"]  # no lost main / DMA overlap on the head
    assert "bank64: (a) Main/Sub rows on DmaEngine 0" in fmt_footprint(fp)
    u = util(ao); assert u.startswith("util.* makespan 24,358 | busy: DmaEngine 78 %") and "DMA 16,021,504 B in 8 rows" in u, u
    # qkv r9: Wq 15,728,640 + Wk / Wv 7,864,320 = three matrices (threshold 3,932,160)
    wq = weights(qk); assert wq["makespan"] == 41660 and wq["matrix_threshold"] == 3932160 and [m["bytes"] for m in wq["matrices"]] == [15728640, 7864320, 7864320], wq["matrices"]
    # ffn r9: m1 / m2 29,491,200; the four 3,686,400 scale grids stay grids (0.125 of m1); the front up to m2 = 11 rows as fifo_order reads it
    wf = weights(ff); assert wf["makespan"] == 123782 and wf["matrix_threshold"] == 7372800 and [m["bytes"] for m in wf["matrices"]] == [29491200, 29491200] and sum(1 for r in wf["rows"] if r["class"] == "grid") >= 2 and all(r["bytes"] == 3686400 for r in wf["rows"] if r["class"] == "grid"), (wf["matrix_threshold"], [(r["class"], r["bytes"]) for r in wf["rows"] if r["class"] in ("grid", "matrix")])
    import fifo_order as FO
    hf, hm = FO.front(ff); assert hm and hm["bytes"] == 29491200 and len(hf) == 11 and [r["class"] for r in hf].count("grid") == 1, (len(hf), hm and hm["bytes"])  # fifo_order reads the same classes through matrix_threshold
    cpf = critical_path(ff); assert cpf["makespan"] == 123782 and cpf["steps"][0]["end"] == 123782 and cpf["chain_rows"] >= 5 and cpf["steps"][-1]["pred"] in ("start", "gap"), (cpf["chain_rows"], cpf["steps"][0])  # the ffn chain walks back to the first row
    assert matrix_threshold([]) is None and weight_class({"tpe": "DmaLoad", "bytes": 5_000_000}, None) == "grid" and weight_class({"tpe": "DmaLoad", "bytes": None, "call": ""}, 1) == "staging"
    print(f"static_schedule selftest ok: r9 attn_out m1 / m2 7,864,320 B @6,966 (threshold 2,000,000), x 262,144 @933 activation, chain {cp['chain_rows']} rows ({cp['classes']}, idle {cp['gap_cycles']:,}), Sram peak {fp['buffers']['Sram']['peak_bytes']:,} B, bank64 (a) 0 / worst co-live {fp['bank64']['worst']['colive']}; qkv 3 matrices; ffn m1 / m2 29,491,200 + grids, front 11 rows via fifo_order")


def main(argv):
    if len(argv) >= 2 and argv[0] == "warn":
        ws = warnings(argv[1], all_kernels="--all" in argv); print("\n".join(ws) if ws else "static_schedule warn: no M1 / M2 finding (information only, A9; the laws are qkv two-term laws, --all applies them anyway)"); return 0
    if not argv or argv[0] in ("-h", "--help"): print(__doc__); return 2
    if argv[0] == "--selftest": selftest(); return 0
    cmd = argv[0]; args = [a for a in argv[1:] if not a.startswith("--")]; js = "--json" in argv
    if not args or not os.path.exists(args[0]): print(f"static_schedule {cmd}: a schedule JSON path is required", file=sys.stderr); return 2
    p = args[0]
    if cmd == "critical_path":
        top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 200; cp = critical_path(p, top); print(json.dumps(cp, indent=1) if js else fmt_critpath(cp)); return 0
    if cmd == "footprint" or cmd == "bank64":
        fp = footprint(p); print(json.dumps(fp["bank64"] if cmd == "bank64" else fp, indent=1) if js else (fmt_footprint(fp) if cmd == "footprint" else fmt_footprint(fp).splitlines()[-1])); return 0
    if cmd == "weights":
        w = weights(p); print(json.dumps(w, indent=1) if js else "\n".join([f"matrix threshold {w['matrix_threshold']:,} B; {len(w['matrices'])} matrix stream(s)"] + [f"{r['static_begin']:>8} {r['class']:<8} {(r['bytes'] or 0):>12,} {r['role']:<11} {r['name']:<22} {r['src'] or '-'}" for r in w["rows"]])); return 0
    if cmd == "util": print(util(p)); return 0
    print(f"static_schedule: unknown command {cmd!r}", file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
