#!/usr/bin/env python3
"""Load the recorded trace timelines into one sqlite file and ask SQL questions about them.

  python3 profiler/trace_sql.py                      # iterations/*/trace/spans/timeline.json + state/attempts.jsonl -> state/trace.db
  python3 profiler/trace_sql.py --db /tmp/trace.db --root <another checkout> --rebuild
  python3 profiler/trace_sql.py --db state/trace.db --sql "SELECT * FROM gaps WHERE cycles > 500 ORDER BY cycles DESC LIMIT 20"
  python3 profiler/trace_sql.py --selftest           # a temp db from 3 iterations; row counts and the view invariants checked

Tables: traces (one row per timeline.json = one Arena trace job), spans (one row per joined span of a kernel),
kernel_summary (the profiler/device_timeline.py summary scalars), attempts (state/attempts.jsonl rows that carry an attempt id),
events (the other rows: strategy_change etc.), load_meta.
Views: phases, gaps, family_gaps, per_cluster_rate, waits, kernel_totals, trace_attempts (job -> attempt rows by
commit_sha), span_vs_parent (a candidate's spans next to the same instr of its recorded parent's trace).
cluster and pe are NULL on every span: the harness sink carries name / begin / end only. The views
group by cluster already, so a runtime cluster tag needs no schema change. instr is the static instruction index for
dma / tu rows (an integer) and the label for sync rows ('ExplicitSync(42)'). stdlib only; read with `sqlite3 -readonly`.
A lane worktree without the data symlinks falls back to the main worktree's iterations/ and state/.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(HERE)
PHASE_BREAK = 256  # idle cycles inside one family that end a phase (a weight stream, a pass chain)
WAIT_MIN_DUR, WAIT_MIN_RATIO = 1500, 5  # a wait is a span with real >= 1.5k and real/static > 5

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS traces(
  job INTEGER PRIMARY KEY, dir TEXT UNIQUE, attempt TEXT, commit_sha TEXT, parent_commit TEXT, lane TEXT, kernel TEXT,
  kind TEXT, decision TEXT, recorded_at TEXT, log TEXT, schedule_dir TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS trace_aliases(dir TEXT PRIMARY KEY, job INTEGER, path TEXT);  -- a 2nd dir with the same job
CREATE TABLE IF NOT EXISTS kernel_summary(
  job INTEGER, kernel TEXT, total_real INTEGER, cycles_line INTEGER, cycles_line_matches INTEGER, union_busy INTEGER,
  total_idle INTEGER, static_makespan INTEGER, real_static_ratio REAL, n_spans INTEGER, n_joined INTEGER, n_rows INTEGER,
  n_unpaired INTEGER, dma_only INTEGER, tu_only INTEGER, both_busy INTEGER, neither INTEGER,
  sum_dma INTEGER, sum_tu INTEGER, sum_sync INTEGER, union_dma INTEGER, union_tu INTEGER, union_sync INTEGER,
  dma_rows_with_bytes INTEGER, dma_bytes_known INTEGER, dma_b_per_cycle REAL, dma_payload_bytes_known INTEGER,
  dma_payload_b_per_cycle REAL, PRIMARY KEY(job, kernel));
CREATE TABLE IF NOT EXISTS spans(
  job INTEGER, kernel TEXT, cluster INTEGER, pe INTEGER, engine TEXT, name TEXT, instr INTEGER, begin INTEGER,
  end INTEGER, bytes INTEGER, dur INTEGER, excess INTEGER, static_begin INTEGER, static_end INTEGER, static_dur INTEGER,
  ratio REAL, gap_prev INTEGER, ord INTEGER, family TEXT, real_kind TEXT, kind TEXT, tpe TEXT, src TEXT, call TEXT,
  payload_bytes INTEGER, footprint_bytes INTEGER, broadcast INTEGER, padding INTEGER, b_per_cycle REAL,
  payload_b_per_cycle REAL, bytes_source TEXT);
CREATE INDEX IF NOT EXISTS spans_job_kernel_begin ON spans(job, kernel, begin);
CREATE INDEX IF NOT EXISTS spans_kernel_instr ON spans(kernel, instr, name);
CREATE TABLE IF NOT EXISTS attempts(
  seq INTEGER PRIMARY KEY, attempt TEXT, kind TEXT, dir TEXT, kernel TEXT, lane TEXT, commit_sha TEXT, parent_commit TEXT,
  parent_attempt TEXT, decision TEXT, compare_decision TEXT, decision_why TEXT, judge_verdict TEXT, p_verdict TEXT,
  accept_rule TEXT, levers TEXT, lever_note TEXT, round INTEGER, launch_id TEXT, recorded_at TEXT, arena_job INTEGER,
  trace_arena_job INTEGER, trace_dir TEXT, profile_level TEXT, real_cycles INTEGER, real_gain_pct REAL,
  real_delta_vs_parent_pct REAL, real_delta_vs_best_pct REAL, parent_real_mean REAL, parent_real_runs INTEGER,
  runs_have INTEGER, runs_required INTEGER, static_makespan INTEGER, static_gain_pct REAL,
  static_delta_vs_parent_pct REAL, compile_ok INTEGER, compile_error_class TEXT, model TEXT, wall_s REAL, note TEXT);
CREATE INDEX IF NOT EXISTS attempts_commit ON attempts(commit_sha);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, kind TEXT, recorded_at TEXT, what TEXT, why TEXT);
CREATE TABLE IF NOT EXISTS load_meta(key TEXT PRIMARY KEY, value TEXT);

CREATE VIEW IF NOT EXISTS kernel_totals AS
SELECT t.job, t.attempt, t.commit_sha, t.lane, k.kernel, k.total_real, k.static_makespan, k.union_busy, k.total_idle,
       k.dma_only, k.tu_only, k.both_busy, k.neither, k.dma_bytes_known, k.dma_b_per_cycle, k.n_rows
FROM kernel_summary k JOIN traces t USING(job);

-- idle windows of the whole kernel timeline: no span of any family runs; head (before the first span) and tail
-- (after the last span, up to total_real) included, so SUM(cycles) per (job, kernel) = kernel_summary.total_idle
CREATE VIEW IF NOT EXISTS gaps AS
WITH s AS (
  SELECT job, kernel, instr, name, family, begin, end,
         MAX(end) OVER (PARTITION BY job, kernel ORDER BY begin, end ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end,
         LAG(instr) OVER (PARTITION BY job, kernel ORDER BY begin, end) AS before_instr,
         LAG(name) OVER (PARTITION BY job, kernel ORDER BY begin, end) AS before_name
  FROM spans),
inner_gaps AS (
  SELECT job, kernel, COALESCE(prev_end, 0) AS gap_begin, begin AS gap_end, begin - COALESCE(prev_end, 0) AS cycles,
         before_instr, before_name, instr AS after_instr, name AS after_name, family AS after_family
  FROM s WHERE begin > COALESCE(prev_end, 0)),
tail_gaps AS (
  SELECT k.job, k.kernel, MAX(sp.end) AS gap_begin, k.total_real AS gap_end, k.total_real - MAX(sp.end) AS cycles,
         NULL AS before_instr, NULL AS before_name, NULL AS after_instr, NULL AS after_name, NULL AS after_family
  FROM kernel_summary k JOIN spans sp ON sp.job = k.job AND sp.kernel = k.kernel
  GROUP BY k.job, k.kernel HAVING k.total_real > MAX(sp.end))
SELECT g.*, ROUND(100.0 * g.cycles / k.total_real, 2) AS pct_of_total
FROM (SELECT * FROM inner_gaps UNION ALL SELECT * FROM tail_gaps) g
JOIN kernel_summary k ON k.job = g.job AND k.kernel = g.kernel;

-- idle windows of ONE family (the DMA engine idle between weight streams, the TU idle between passes); head and tail
-- included, so SUM(cycles) per (job, kernel, family) = total_real - kernel_summary.union_<family>
CREATE VIEW IF NOT EXISTS family_gaps AS
WITH s AS (
  SELECT job, kernel, family, instr, name, begin, end,
         MAX(end) OVER (PARTITION BY job, kernel, family ORDER BY begin, end
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end,
         LAG(instr) OVER (PARTITION BY job, kernel, family ORDER BY begin, end) AS before_instr,
         LAG(name) OVER (PARTITION BY job, kernel, family ORDER BY begin, end) AS before_name
  FROM spans),
inner_gaps AS (
  SELECT job, kernel, family, COALESCE(prev_end, 0) AS gap_begin, begin AS gap_end, begin - COALESCE(prev_end, 0) AS cycles,
         before_instr, before_name, instr AS after_instr, name AS after_name
  FROM s WHERE begin > COALESCE(prev_end, 0)),
tail_gaps AS (
  SELECT k.job, k.kernel, sp.family, MAX(sp.end) AS gap_begin, k.total_real AS gap_end, k.total_real - MAX(sp.end) AS cycles,
         NULL AS before_instr, NULL AS before_name, NULL AS after_instr, NULL AS after_name
  FROM kernel_summary k JOIN spans sp ON sp.job = k.job AND sp.kernel = k.kernel
  GROUP BY k.job, k.kernel, sp.family HAVING k.total_real > MAX(sp.end))
SELECT g.*, ROUND(100.0 * g.cycles / k.total_real, 2) AS pct_of_total
FROM (SELECT * FROM inner_gaps UNION ALL SELECT * FROM tail_gaps) g
JOIN kernel_summary k ON k.job = g.job AND k.kernel = g.kernel;

-- contiguous stretches of one family (spans that overlap or sit within PHASE_BREAK idle cycles of each other):
-- for dma a weight stream, for tu a pass chain; phase k counts from 1 in time order inside (job, kernel, family)
CREATE VIEW IF NOT EXISTS phases AS
WITH s AS (
  SELECT job, kernel, family, instr, kind, begin, end, dur, bytes, payload_bytes,
         MAX(end) OVER (PARTITION BY job, kernel, family ORDER BY begin, end
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
  FROM spans),
b AS (SELECT *, CASE WHEN prev_end IS NULL OR begin > prev_end + {PHASE_BREAK} THEN 1 ELSE 0 END AS brk FROM s),
g AS (SELECT *, SUM(brk) OVER (PARTITION BY job, kernel, family ORDER BY begin, end ROWS UNBOUNDED PRECEDING) AS k FROM b),
f AS (SELECT *, FIRST_VALUE(instr) OVER (PARTITION BY job, kernel, family, k ORDER BY begin, end) AS first_instr,
                FIRST_VALUE(instr) OVER (PARTITION BY job, kernel, family, k ORDER BY end DESC, begin DESC) AS last_instr
      FROM g)
SELECT job, kernel, family, k AS phase, MIN(begin) AS begin, MAX(end) AS end, MAX(end) - MIN(begin) AS cycles,
       SUM(dur) AS busy_sum, COUNT(*) AS n_spans, MIN(first_instr) AS first_instr, MIN(last_instr) AS last_instr,
       GROUP_CONCAT(DISTINCT kind) AS kinds, SUM(bytes) AS bytes, SUM(payload_bytes) AS payload_bytes,
       CASE WHEN MAX(end) > MIN(begin) THEN ROUND(1.0 * SUM(bytes) / (MAX(end) - MIN(begin)), 1) END AS b_per_cycle
FROM f GROUP BY job, kernel, family, k;

-- DMA bytes per cycle per (job, kernel, cluster): over the window from the first DMA begin to the last DMA end, and
-- over the whole kernel; cluster is NULL until the harness tags spans, so today this is one row per (job, kernel)
CREATE VIEW IF NOT EXISTS per_cluster_rate AS
SELECT s.job, s.kernel, s.cluster, COUNT(*) AS n_dma, MIN(s.begin) AS first_begin, MAX(s.end) AS last_end,
       SUM(s.bytes) AS bytes, SUM(s.payload_bytes) AS payload_bytes, SUM(s.dur) AS busy_sum,
       ROUND(1.0 * SUM(s.bytes) / (MAX(s.end) - MIN(s.begin)), 1) AS b_per_cycle_window,
       ROUND(1.0 * SUM(s.payload_bytes) / (MAX(s.end) - MIN(s.begin)), 1) AS payload_b_per_cycle_window,
       ROUND(1.0 * SUM(s.bytes) / k.total_real, 1) AS b_per_cycle_kernel
FROM spans s JOIN kernel_summary k ON k.job = s.job AND k.kernel = s.kernel
WHERE s.family = 'dma' AND s.bytes IS NOT NULL
GROUP BY s.job, s.kernel, s.cluster;

-- wait spans of any family (a TU span next to a barrier can be the wait)
CREATE VIEW IF NOT EXISTS waits AS
SELECT s.job, s.kernel, s.instr, s.name, s.kind, s.family, s.begin, s.end, s.dur, s.static_dur, s.ratio, s.excess,
       ROUND(100.0 * s.dur / k.total_real, 2) AS pct_of_total
FROM spans s JOIN kernel_summary k ON k.job = s.job AND k.kernel = s.kernel
WHERE s.dur >= {WAIT_MIN_DUR} AND s.ratio > {WAIT_MIN_RATIO};

CREATE VIEW IF NOT EXISTS trace_attempts AS
SELECT t.job, t.dir AS trace_iteration, t.commit_sha, a.attempt, a.kind, a.kernel AS attempt_kernel, a.lane, a.decision,
       a.profile_level, a.real_cycles, a.real_gain_pct, a.real_delta_vs_parent_pct, a.parent_commit, a.recorded_at,
       a.arena_job
FROM traces t JOIN attempts a ON a.commit_sha = t.commit_sha;

-- same kernel, same instr index and name in the candidate's trace and in a trace of its recorded parent commit;
-- renumbered instructions do not match here (profiler/device_timeline.py --diff does the order/type join)
CREATE VIEW IF NOT EXISTS span_vs_parent AS
SELECT c.job, tp.job AS parent_job, c.kernel, c.instr, c.name, c.kind, c.family, c.begin, c.dur, p.dur AS parent_dur,
       c.dur - p.dur AS delta, c.b_per_cycle, p.b_per_cycle AS parent_b_per_cycle
FROM spans c
JOIN traces tc ON tc.job = c.job
JOIN traces tp ON tp.commit_sha = tc.parent_commit
JOIN spans p ON p.job = tp.job AND p.kernel = c.kernel AND p.instr = c.instr AND p.name = c.name;
"""

SPAN_COLS = ("job", "kernel", "cluster", "pe", "engine", "name", "instr", "begin", "end", "bytes", "dur", "excess",
             "static_begin", "static_end", "static_dur", "ratio", "gap_prev", "ord", "family", "real_kind", "kind",
             "tpe", "src", "call", "payload_bytes", "footprint_bytes", "broadcast", "padding", "b_per_cycle",
             "payload_b_per_cycle", "bytes_source")
ATTEMPT_COLS = ("seq", "attempt", "kind", "dir", "kernel", "lane", "commit_sha", "parent_commit", "parent_attempt",
                "decision", "compare_decision", "decision_why", "judge_verdict", "p_verdict", "accept_rule", "levers",
                "lever_note", "round", "launch_id", "recorded_at", "arena_job", "trace_arena_job", "trace_dir",
                "profile_level", "real_cycles", "real_gain_pct", "real_delta_vs_parent_pct", "real_delta_vs_best_pct",
                "parent_real_mean", "parent_real_runs", "runs_have", "runs_required", "static_makespan",
                "static_gain_pct", "static_delta_vs_parent_pct", "compile_ok", "compile_error_class", "model", "wall_s",
                "note")


def main_worktree(checkout: str) -> str | None:
    """The main worktree of a linked worktree (its .git is a file 'gitdir: <main>/.git/worktrees/<name>')."""
    dotgit = os.path.join(checkout, ".git")
    if not os.path.isfile(dotgit):
        return None
    with open(dotgit) as f:
        line = f.read().strip()
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if "/.git/worktrees/" not in gitdir:
        return None
    return gitdir.split("/.git/worktrees/")[0]


def data_root() -> str:
    """The checkout that holds iterations/: this checkout, else its main worktree."""
    for r in (CHECKOUT, main_worktree(CHECKOUT)):
        if r and os.path.isdir(os.path.join(r, "iterations")):
            return r
    return CHECKOUT


def timelines(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "iterations", "*", "trace", "spans", "timeline.json")))


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


def as_int(v):
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    return v


def trace_header(tl_path: str, doc: dict) -> dict:
    trace_dir = os.path.dirname(os.path.dirname(tl_path))
    it_dir = os.path.dirname(trace_dir)
    job = None
    rp = os.path.join(trace_dir, "result.json")
    if os.path.exists(rp):
        try:
            job = load_json(rp).get("arena_job")
        except (OSError, ValueError):
            job = None
    if job is None:  # fall back to the Arena log name: .../arena/<job>.log
        stem = os.path.splitext(os.path.basename(doc.get("log") or ""))[0]
        job = int(stem) if stem.isdigit() else None
    meta = {}
    mp = os.path.join(it_dir, "meta.json")
    if os.path.exists(mp):
        try:
            meta = load_json(mp)
        except (OSError, ValueError):
            meta = {}
    return dict(job=job, dir=os.path.basename(it_dir), attempt=meta.get("attempt"), commit_sha=meta.get("commit"),
                parent_commit=meta.get("parent_commit"), lane=meta.get("lane"), kernel=meta.get("kernel"),
                kind=meta.get("kind"), decision=meta.get("decision"), recorded_at=meta.get("recorded_at"),
                log=doc.get("log"), schedule_dir=doc.get("schedule_dir"), path=tl_path)


def span_tuples(job: int, kernel: str, rows: list[dict]):
    for r in rows:
        rd, sd = r.get("real_dur"), r.get("static_dur")
        excess = rd - sd if isinstance(rd, (int, float)) and isinstance(sd, (int, float)) else None
        yield (job, kernel, None, None, r.get("ctx"), r.get("name"), as_int(r.get("instr")), r.get("real_begin"),
               r.get("real_end"), r.get("bytes"), rd, excess, r.get("static_begin"), r.get("static_end"), sd,
               r.get("ratio"), r.get("gap"), r.get("order"), r.get("family"), r.get("real_kind"), r.get("kind"),
               r.get("tpe"), r.get("src"), r.get("call"), r.get("payload_bytes"), r.get("footprint_bytes"),
               r.get("broadcast"), r.get("padding"), r.get("b_per_cycle"), r.get("payload_b_per_cycle"),
               r.get("bytes_source"))


def summary_tuple(job: int, kernel: str, s: dict, n_unpaired: int):
    eo = s.get("engine_overlap") or {}
    sr = s.get("sum_real") or {}
    ur = s.get("union_real") or {}
    return (job, kernel, s.get("total_real"), s.get("cycles_line"),
            None if s.get("cycles_line_matches") is None else int(bool(s.get("cycles_line_matches"))),
            s.get("union_busy"), s.get("total_idle"), s.get("static_makespan"), s.get("real_static_ratio"),
            s.get("n_spans"), s.get("n_joined"), s.get("n_rows"), n_unpaired, eo.get("dma_only"), eo.get("tu_only"),
            eo.get("both"), eo.get("neither"), sr.get("dma"), sr.get("tu"), sr.get("sync"), ur.get("dma"), ur.get("tu"),
            ur.get("sync"), s.get("dma_rows_with_bytes"), s.get("dma_bytes_known"), s.get("dma_b_per_cycle"),
            s.get("dma_payload_bytes_known"), s.get("dma_payload_b_per_cycle"))


def load_traces(con: sqlite3.Connection, roots: list[str], limit: int | None = None) -> dict:
    n = dict(files=0, loaded=0, skipped_loaded=0, skipped_no_job=0, aliases=0, spans=0)
    have = {r[0] for r in con.execute("SELECT dir FROM traces")} | {r[0] for r in con.execute("SELECT dir FROM trace_aliases")}
    jobs = {r[0] for r in con.execute("SELECT job FROM traces")}
    for root in roots:
        for tl in timelines(root):
            if limit is not None and n["files"] >= limit:
                break
            n["files"] += 1
            d = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(tl))))
            if d in have:
                n["skipped_loaded"] += 1
                continue
            doc = load_json(tl)
            h = trace_header(tl, doc)
            if h["job"] is None:
                n["skipped_no_job"] += 1
                continue
            if h["job"] in jobs:  # a second iteration dir recorded against the same Arena job (a copied trace dir)
                con.execute("INSERT OR REPLACE INTO trace_aliases VALUES (?,?,?)", (d, h["job"], tl))
                have.add(d)
                n["aliases"] += 1
                continue
            con.execute("INSERT INTO traces(job, dir, attempt, commit_sha, parent_commit, lane, kernel, kind, decision, "
                        "recorded_at, log, schedule_dir, path) VALUES (:job, :dir, :attempt, :commit_sha, :parent_commit, "
                        ":lane, :kernel, :kind, :decision, :recorded_at, :log, :schedule_dir, :path)", h)
            for kernel, kd in (doc.get("kernels") or {}).items():
                rows = kd.get("rows") or []
                con.execute("INSERT OR REPLACE INTO kernel_summary VALUES (" + ",".join("?" * 28) + ")",
                            summary_tuple(h["job"], kernel, kd.get("summary") or {}, len(kd.get("unpaired_instructions") or [])))
                con.executemany("INSERT INTO spans(" + ",".join(SPAN_COLS) + ") VALUES (" + ",".join("?" * len(SPAN_COLS)) + ")",
                                span_tuples(h["job"], kernel, rows))
                n["spans"] += len(rows)
            have.add(d)
            jobs.add(h["job"])
            n["loaded"] += 1
    return n


def load_attempts(con: sqlite3.Connection, path: str) -> dict:
    """attempts.jsonl -> attempts (rows with an attempt id) + events (the rest); rebuilt on every load, it is small."""
    con.execute("DELETE FROM attempts")
    con.execute("DELETE FROM events")
    n = dict(attempts=0, events=0, bad_lines=0)
    if not os.path.exists(path):
        return n
    with open(path) as f:
        for seq, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                n["bad_lines"] += 1
                continue
            if row.get("attempt") is None:
                con.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                            (seq, row.get("kind"), row.get("recorded_at"), row.get("what"), row.get("why")))
                n["events"] += 1
                continue
            kernel = row.get("kernel")
            rc = row.get("real_cycles")
            if isinstance(rc, dict):
                rc = rc.get(kernel)
            levers = row.get("levers")
            vals = dict(row)
            vals.update(seq=seq, commit_sha=row.get("commit"), real_cycles=rc,
                        levers=json.dumps(levers) if levers is not None else None,
                        compile_ok=None if row.get("compile_ok") is None else int(bool(row.get("compile_ok"))))
            row_vals = {c: vals.get(c) for c in ATTEMPT_COLS}
            for c, v in row_vals.items():  # newer rows nest dicts (accept_rule, p_verdict): keep them as JSON text
                if isinstance(v, (dict, list)):
                    row_vals[c] = json.dumps(v, sort_keys=True)
                elif isinstance(v, bool):
                    row_vals[c] = int(v)
            con.execute("INSERT INTO attempts(" + ",".join(ATTEMPT_COLS) + ") VALUES (" +
                        ",".join(":" + c for c in ATTEMPT_COLS) + ")", row_vals)
            n["attempts"] += 1
    return n


def build(db: str, roots: list[str], attempts_path: str | None, rebuild: bool = False, limit: int | None = None) -> dict:
    t0 = time.time()
    if rebuild and os.path.exists(db):
        os.remove(db)
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(SCHEMA)
    with con:
        nt = load_traces(con, roots, limit)
        na = load_attempts(con, attempts_path) if attempts_path else dict(attempts=0, events=0, bad_lines=0)
        for k, v in (("loaded_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())), ("roots", json.dumps(roots)),
                     ("attempts_path", attempts_path or ""), ("phase_break", str(PHASE_BREAK))):
            con.execute("INSERT OR REPLACE INTO load_meta VALUES (?,?)", (k, v))
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("traces", "trace_aliases", "kernel_summary", "spans", "attempts", "events")}
    con.close()
    counts.update(files=nt["files"], new_traces=nt["loaded"], new_aliases=nt["aliases"], skipped_loaded=nt["skipped_loaded"],
                  skipped_no_job=nt["skipped_no_job"], bad_lines=na["bad_lines"], seconds=round(time.time() - t0, 2),
                  db=db, bytes=os.path.getsize(db))
    return counts


def query(db: str, sql: str, out=sys.stdout) -> int:
    con = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description] if cur.description else []
    if cols:
        print("\t".join(cols), file=out)
    n = 0
    for row in cur:
        print("\t".join("" if v is None else str(v) for v in row), file=out)
        n += 1
    con.close()
    return n


# ---------------------------------------------------------------- selftest -----------------------------------------

def _row(order, name, family, instr, kind, tpe, ctx, sb, se, rb, re_, **kw):
    r = dict(order=order, name=name, real_kind=name, family=family, instr=str(instr), tpe=tpe, kind=kind, ctx=ctx,
             src="src/x.rs:1:1", call="x()", static_begin=sb, static_end=se, static_dur=se - sb, real_begin=rb,
             real_end=re_, real_dur=re_ - rb, ratio=round((re_ - rb) / max(1, se - sb), 3), gap=None, bytes=None,
             bytes_source=None, footprint_bytes=None, padding=None, broadcast=None, payload_bytes=None,
             b_per_cycle=None, payload_b_per_cycle=None)
    r.update(kw)
    return r


def _summary(kernel, rows, total):
    fam = {}
    for r in rows:
        fam.setdefault(r["family"], []).append((r["real_begin"], r["real_end"]))
    ivs = sorted(iv for lst in fam.values() for iv in lst)
    busy, cur_b, cur_e = 0, None, None
    for b, e in ivs:
        if cur_e is None or b > cur_e:
            if cur_e is not None:
                busy += cur_e - cur_b
            cur_b, cur_e = b, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        busy += cur_e - cur_b
    return dict(kernel=kernel, join_rule="order/type", n_spans=len(rows), n_joined=len(rows), n_rows=len(rows),
                total_real=total, cycles_line=total, cycles_line_matches=True, union_busy=busy, total_idle=total - busy,
                static_makespan=max(r["static_end"] for r in rows), real_static_ratio=1.0,
                engine_overlap=dict(dma_only=0, tu_only=0, both=0, neither=0),
                sum_real={k: sum(e - b for b, e in v) for k, v in fam.items()}, union_real={},
                dma_rows_with_bytes=sum(1 for r in rows if r.get("bytes")),
                dma_bytes_known=sum(r.get("bytes") or 0 for r in rows), dma_b_per_cycle=None,
                dma_payload_bytes_known=0, dma_payload_b_per_cycle=None)


def synth_root(tmp: str) -> tuple[str, dict]:
    """Three synthetic iterations with known counts; returns (root, expected)."""
    QKV, ATT, FFN = "sliding_project_qkv", "sliding_attention_output", "decoder_feedforward"
    its = [
        ("0001-qkv-synth-a", "aaaa1111", "pppp0000", "qkv", QKV, 1001, True, {
            QKV: ([_row(1, "DMA", "dma", 1, "DmaLoad", "DmaLoad", "DmaEngine", 0, 50, 0, 100, bytes=1000, payload_bytes=500),
                   _row(2, "Renegade::TuExec", "tu", 2, "main", "Main", "MainContext", 50, 100, 150, 300),
                   _row(3, "Renegade::TuExec", "tu", 3, "main", "Main", "MainContext", 100, 150, 280, 400)], 450)}),
        ("0002-attn_out-synth-b", "bbbb2222", "aaaa1111", "attn_out", ATT, 1002, True, {
            ATT: ([_row(1, "DMA", "dma", 1, "DmaLoad", "DmaLoad", "DmaEngine", 0, 50, 10, 110, bytes=2000),
                   _row(2, "DMA", "dma", 2, "DmaLoad", "DmaLoad", "DmaEngine", 50, 100, 500, 600, bytes=2000),
                   _row(3, "Cluster", "sync", 3, "ExplicitSync", "Sub", "SubContext", 100, 110, 600, 700)], 700)}),
        ("0003-ffn-synth-c", "cccc3333", "aaaa1111", "ffn", FFN, 1003, False, {
            FFN: ([_row(1, "Renegade::TuExec", "tu", 1, "main", "Main", "MainContext", 0, 900, 0, 1000),
                   _row(2, "DMA", "dma", 2, "DmaLoad", "DmaLoad", "DmaEngine", 0, 1000, 0, 1000, bytes=675000)], 1000),
            QKV: ([_row(1, "DMA", "dma", 1, "DmaLoad", "DmaLoad", "DmaEngine", 0, 50, 100, 200, bytes=1000)], 300)}),
    ]
    root = os.path.join(tmp, "synth")
    for d, commit, parent, lane, kernel, job, with_result, kernels in its:
        sp = os.path.join(root, "iterations", d, "trace", "spans")
        os.makedirs(sp)
        doc = dict(log=f"work/measure/{d}/arena/{job}.log", schedule_dir=f"work/measure/{d}/schedule",
                   kernels={k: dict(summary=_summary(k, rows, total), rows=rows, unpaired_instructions=[])
                            for k, (rows, total) in kernels.items()})
        with open(os.path.join(sp, "timeline.json"), "w") as f:
            json.dump(doc, f)
        if with_result:
            with open(os.path.join(root, "iterations", d, "trace", "result.json"), "w") as f:
                json.dump(dict(arena_job=job, profile_level="trace"), f)
        with open(os.path.join(root, "iterations", d, "meta.json"), "w") as f:
            json.dump(dict(attempt=d[:4], dir=d, kind="attempt", kernel=kernel, lane=lane, commit=commit,
                           parent_commit=parent, decision="reject", recorded_at="2026-09-19T00:00:00Z"), f)
    os.makedirs(os.path.join(root, "state"))
    rows = [dict(attempt="0001", kind="attempt", kernel=QKV, lane="qkv", commit="aaaa1111", decision="accept",
                 real_cycles=450, compile_ok=True, levers=["lever-a"], round=1),
            dict(attempt="0002", kind="attempt", kernel=ATT, lane="attn_out", commit="bbbb2222", decision="reject",
                 real_cycles={QKV: 1, ATT: 700, FFN: 2}, compile_ok=True, levers=[]),
            dict(attempt="0003", kind="attempt", kernel=FFN, lane="ffn", commit="cccc3333", decision="reject"),
            dict(attempt="0004", kind="measurement", kernel=QKV, lane="qkv", commit="aaaa1111", decision="measurement"),
            dict(kind="strategy_change", recorded_at="2026-09-19T00:00:01Z", what="park lever:x", why="test")]
    with open(os.path.join(root, "state", "attempts.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write("not json\n")
    expected = dict(traces=3, kernel_summary=4, spans=9, attempts=4, events=1, bad_lines=1, trace_attempts=4, gaps=6,
                    gaps_cycles=700, phases=8, ffn_rate=675.0, span_vs_parent=1, job_fallback=1003)
    return root, expected


def selftest() -> int:
    t0 = time.time()
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'ok ' if ok else 'BAD'} {name}: got {got!r}, want {want!r}")
        if not ok:
            fails.append(name)

    with tempfile.TemporaryDirectory(prefix="trace-db-selftest-") as tmp:
        root, exp = synth_root(tmp)
        db = os.path.join(tmp, "synth.db")
        c = build(db, [root], os.path.join(root, "state", "attempts.jsonl"))
        print(f"synthetic: {c}")
        for t in ("traces", "kernel_summary", "spans", "attempts", "events", "bad_lines"):
            check(f"synthetic {t}", c[t], exp[t])
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        q = lambda sql: con.execute(sql).fetchall()
        check("synthetic trace_attempts rows", q("SELECT COUNT(*) FROM trace_attempts")[0][0], exp["trace_attempts"])
        check("synthetic gaps rows", q("SELECT COUNT(*) FROM gaps")[0][0], exp["gaps"])
        check("synthetic gaps cycles = total_idle", q("SELECT SUM(cycles) FROM gaps")[0][0], exp["gaps_cycles"])
        check("synthetic phases rows", q("SELECT COUNT(*) FROM phases")[0][0], exp["phases"])
        check("synthetic family_gaps rows, cycles", q("SELECT COUNT(*), SUM(cycles) FROM family_gaps")[0], (9, 1850))
        check("synthetic per_cluster_rate ffn", q("SELECT b_per_cycle_window FROM per_cluster_rate WHERE job = 1003 "
                                                  "AND kernel = 'decoder_feedforward'")[0][0], exp["ffn_rate"])
        check("synthetic span_vs_parent rows", q("SELECT COUNT(*) FROM span_vs_parent")[0][0], exp["span_vs_parent"])
        check("synthetic job from log name", q("SELECT COUNT(*) FROM traces WHERE job = 1003")[0][0], 1)
        check("synthetic real_cycles picked per kernel",
              q("SELECT real_cycles FROM attempts WHERE attempt = '0002'")[0][0], 700)
        check("synthetic bare begin/end columns", q("SELECT begin, end FROM spans ORDER BY job, begin LIMIT 1")[0], (0, 100))
        check("synthetic waits rows", q("SELECT COUNT(*) FROM waits")[0][0], 0)
        con.close()
        c2 = build(db, [root], os.path.join(root, "state", "attempts.jsonl"))  # incremental re-load: nothing doubles
        check("synthetic reload idempotent", (c2["traces"], c2["spans"], c2["attempts"], c2["new_traces"]), (3, 9, 4, 0))
        c3 = build(db, [root], os.path.join(root, "state", "attempts.jsonl"), rebuild=True)
        check("synthetic rebuild", (c3["traces"], c3["spans"]), (3, 9))

        # real data when the checkout (or its main worktree) holds iterations/: first 3 timelines
        root_r = data_root()
        tls = timelines(root_r)[:3]
        if tls:
            want_spans = want_kernels = no_job = 0
            seen = set()  # one iteration dir can carry a copy of another dir's trace job: counted once, aliased
            for tl in tls:
                doc = load_json(tl)
                job = trace_header(tl, doc)["job"]
                if job is None:
                    no_job += 1
                    continue
                if job in seen:
                    continue
                seen.add(job)
                want_kernels += len(doc["kernels"])
                want_spans += sum(len(k["rows"]) for k in doc["kernels"].values())
            att = os.path.join(root_r, "state", "attempts.jsonl")
            dbr = os.path.join(tmp, "real.db")
            cr = build(dbr, [root_r], att if os.path.exists(att) else None, limit=3)
            print(f"real ({root_r}): {cr}")
            check("real traces = distinct jobs of the 3 files", cr["traces"], len(seen))
            check("real trace_aliases = files sharing a job", cr["trace_aliases"], len(tls) - len(seen) - no_job)
            check("real kernel_summary", cr["kernel_summary"], want_kernels)
            check("real spans = sum(len(rows)) over distinct jobs", cr["spans"], want_spans)
            con = sqlite3.connect(f"file:{dbr}?mode=ro", uri=True)
            bad = con.execute("SELECT COUNT(*) FROM (SELECT k.job, k.kernel, k.total_idle, COALESCE(SUM(g.cycles), 0) AS idle "
                              "FROM kernel_summary k LEFT JOIN gaps g ON g.job = k.job AND g.kernel = k.kernel "
                              "GROUP BY k.job, k.kernel) WHERE idle != total_idle").fetchone()[0]
            check("real gaps cycles = total_idle for every (job, kernel)", bad, 0)
            bad_f = con.execute(
                "SELECT COUNT(*) FROM (SELECT k.job, k.kernel, f.family, k.total_real - CASE f.family WHEN 'dma' THEN k.union_dma "
                "WHEN 'tu' THEN k.union_tu WHEN 'sync' THEN k.union_sync END AS want, SUM(f.cycles) AS got FROM family_gaps f "
                "JOIN kernel_summary k ON k.job = f.job AND k.kernel = f.kernel WHERE f.family IN ('dma', 'tu', 'sync') "
                "GROUP BY k.job, k.kernel, f.family) WHERE want IS NOT NULL AND got != want").fetchone()[0]
            check("real family_gaps cycles = total_real - union_<family> for every (job, kernel, family)", bad_f, 0)
            n_views = {v: con.execute(f"SELECT COUNT(*) FROM {v}").fetchone()[0]
                       for v in ("phases", "gaps", "family_gaps", "per_cluster_rate", "waits", "kernel_totals", "trace_attempts")}
            print(f"  real view rows: {n_views}")
            check("real phases non-empty", n_views["phases"] > 0, True)
            if os.path.exists(att):
                with open(att) as f:
                    want_att = sum(1 for line in f if line.strip() and '"attempt"' in line and json.loads(line).get("attempt") is not None)
                check("real attempts rows = rows with an attempt id", cr["attempts"], want_att)
            con.close()
        else:
            print("real: skipped (no iterations/ under this checkout or its main worktree)")
    print(f"trace_sql selftest {'OK' if not fails else 'FAILED: ' + ', '.join(fails)} ({time.time() - t0:.1f} s)")
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="sqlite file (default <data root>/state/trace.db)")
    ap.add_argument("--root", action="append", default=[], help="extra checkout(s) with iterations/")
    ap.add_argument("--attempts", help="attempts.jsonl (default <data root>/state/attempts.jsonl)")
    ap.add_argument("--rebuild", action="store_true", help="drop the db first")
    ap.add_argument("--limit", type=int, help="load at most N timeline files (trials)")
    ap.add_argument("--sql", help="run one read-only query against --db and print it tab-separated; no load")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    root = data_root()
    db = a.db or os.path.join(root, "state", "trace.db")
    if a.sql:
        n = query(db, a.sql)
        print(f"-- {n} row(s)", file=sys.stderr)
        return 0
    roots = [root] + [os.path.expanduser(r) for r in a.root]
    attempts = a.attempts or os.path.join(root, "state", "attempts.jsonl")
    c = build(db, roots, attempts if os.path.exists(attempts) else None, rebuild=a.rebuild, limit=a.limit)
    print(json.dumps(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
