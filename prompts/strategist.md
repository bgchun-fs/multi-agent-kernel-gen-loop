# STRATEGIST run

A strategist run is a main run that also asks: why are the kernels not yet near their calculated peak, is the approach wrong, and which levers can close the gap to the peak?
BRIEF.md step 1 makes a run a strategist run when main/STRATEGY_NOW exists or main/strategy/LAST is missing or older than 3 h. A submission
in flight under the submission rules, a chain to watch and the standing fan-out floor are still handled first, as in any run.

## The envelope (set by the human operator)
- MAY: rewrite main/ANALYSIS.md (numbers, causes, priorities; keep its section structure); write new worker briefs in main/briefs-auto/
  and launch them (`BASE_LANE=<lane> bash main/scripts/ec2_worker.sh <name> main/briefs-auto/<name>.md 240`); stop workers; choose any lever;
  change the device rules for workers.
- At most 36 workers alive (BRIEF step 2m; the sandbox + the GPT agents, the door only). The fleet target is 36 while
  the device runs < 150 jobs / h and loadavg < 120; the Claude floor is 12 and counts only workers that build or time a kernel lever.
- MAY NOT: touch the fold ban or the submission rules; the harness; secrets; contact with the organizers; rewrite the decider or
  family-loop code without a selftest, a `git commit -s` and a release (main/scripts/e3_head.sh); spend the device on anything but worker,
  gate and submission jobs.

## Steps
1. `bash main/scripts/strategy_data.sh` writes main/strategy/<utc>/ (LATEST points to it): SUMMARY.md, gap.md (per-kernel
   gap to the calculated peak with ln shares and the points per 1k cycles), ledger.txt, dma.txt (the head's static DMA numbers), device.txt (jobs per
   hour, minutes since the last device job), workers.txt (alive / ended, minutes left, each REPORT's first lines), pairs.txt (the best pair
   readings of the last 6 h). If a section failed, read the source by hand.
2. Read: that output; the REPORT.md of every worker that ended since the last strategist run (workers/<name>/REPORT.md) and the SCOUT-TABLE.md
   files of the scouts (workers/<scout>/); main/ANALYSIS.md; the last 30 lines of main/LOG.md; NOTES.md.
3. Decide the levers by the gap to the peak, not by the head: for each kernel, the ln share ln(ours / peak) and the points per 1k
   cycles (score = geomean of baseline / cycles; d score = score / (3 x cycles_k) per cycle). A lever is worth a worker when its projected
   saving x points per 1k is large AND the evidence says it is reachable (a static win with the DMA front kept, a device reading, a law).
   Mark a lever DEAD only when two DIFFERENT designs each ended by a compile refusal or a timed read (a twin pair on one brief is one
   attempt); a paper or trace argument gives LOW PRIORITY, never DEAD (workers/shared/RULES.md section 3).
   Ask each time: is the structure wrong? (The calculated peak sits at the lean floor of our own laws; incremental edits on the current structure cannot
   reach it where our zero-lag floor is above it.)
4. Act (inside the envelope):
   - Rewrite main/ANALYSIS.md: section 1 numbers from gap.md and dma.txt; section 2 what failed since (with the report paths);
     section 3 the ranked levers with targets; sections 4-5 how many workers, which briefs, the device rules. Keep a backup
     main/ANALYSIS.md.bak-<utc> and copy the new one to workers/shared/ANALYSIS.md.
   - Stop the workers whose lever is DEAD (find the pid: the `timeout ... claude -p` process whose `sudo -n readlink /proc/<pid>/cwd`
     is workers/<name>; `sudo -n kill <pid>`; never pkill on a pattern). Copy their REPORT.md (if any) and best diff to workers/shared/<name>/.
   - Write briefs for the new levers in main/briefs-auto/<name>.md from the template of an existing brief (the kernel, ONE lever with the
     evidence and target, the base lane, the door, the rules, the report format, an addendum with the current head, the 2 GB build bar, the
     build-time bar, the device rules) and launch them. Base = the lane that holds the next head's version of the files the lever edits.
   - Fill toward 36 while the device runs < 150 jobs / h and loadavg < 120: at least 2 non-duplicate BUILDERS per missed kernel and 2
     build-first structural workers on the largest-share kernel, each with a named next compile or timed discriminator; open levers include
     every compiled lane never timed or UNRESOLVED (workers/shared/RESOLVE.md). Never launch a worker only to hold the floor.
5. Record: append ONE LOG.md line ("strategist run": what the data showed, the levers chosen / dropped, workers started / stopped with names).
   Write main/strategy/<utc>/DECISION.md (the same, longer). Remove main/STRATEGY_NOW, `touch main/strategy/LAST`.

## Also on every strategist run (BRIEF standing orders 1 and 5)
- GOALS: apply the rules in workers/shared/GOALS.md (reached: ratchet to the next target, deadline now + 48 h; missed or projected to miss:
  move workers to that kernel, add a fresh-design worker, deadline now + 24 h); one LOG.md line + the file's change log per change.
- FAN-OUT: target alive = min(36, open levers + the 3 kernel owners + the scouts), never under 12 while a goal is open; static scouts
  instead of device workers when more than 8 probes wait; stop duplicates (two workers on one lever) and dead levers.
- KERNEL OWNERS: one owner per kernel (ffn, qkv, attn_out; main/briefs-owners/*.md, 720 min, BASE_LANE = the lane holding the next head's files) stay
  alive: relaunch an ended owner at once under the next free name (ec2_worker.sh refuses an existing dir) with the same
  brief; launch the experiments listed in each owner's shared/owners/<kernel>/NOTEBOOK.md 'Experiments for others' section.

## The first strategist run is a DRY RUN
If main/strategy/DRY_DONE does not exist: do steps 1-3, then write main/strategy/<utc>/PROPOSAL.md (what you would change and why: the
ANALYSIS edits, the workers to stop, the briefs to write, with the numbers), `touch main/strategy/DRY_DONE main/strategy/LAST`, remove
main/STRATEGY_NOW, append the LOG.md line, and change nothing else in the strategy (the standing orders still apply: keep the floor of 12
alive with the levers already in ANALYSIS.md). The next strategist run acts on the proposal inside the envelope.

## Self-review
- Rule 1 (in force): the probe policy in workers/shared/RULES.md (static / sim ORDER the probe queue, never gate it: probe static-better OR
  |delta static| < 150; skip only static-worse). Every new worker brief carries it; ANALYSIS.md section 5 too.
- Rule 5 (in force): FINAL FREEZE from the final gate (nothing at all after the final cutoff): file no new tree except the final one. Before the freeze,
  pick the top 2-3 candidates, gate them against each other (paired_gate), write the winner's lane name to main/FINAL_LANE and file it once
  (after_gate_lane.sh refuses everything after the freeze). AMENDED (approved by the human operator): the FINAL filing wins over this line
  and over the submission rules' 'resubmit the previous best tree at once': file it by hand with the a30_submit.sh steps (MIN_GAIN 0.2,
  >= 30 pooled pairs, pooled z >= 2), NOT through chain_submit.sh; a SCORED lower read is a noise draw and is KEPT; no score -> re-file the
  same tree once, then restore HEAD_REF's tree once and record it in the run log; file it before the filing deadline.
- Rule 7(a) (in force): the watchdog's `review` check writes main/review/health.txt every 90 min (main/scripts/review.py --health: tool
  selftests, session sizes, workers alive and yield, the device queue). Read it on every run; act on a failed selftest or a kernel owner
  session that grew too large (see rule 3 when armed).
- Rule 7(b) (every strategist run at least every ~6 h): `python3 main/scripts/review.py > <strategy dir>/REVIEW.md` input, re-validate rules
  1 and 2; change them only with >= 20 new door comparisons and >= 5 new gates since the last change; log the numbers.
- CONDITIONAL rules 2 (gate early stop), 3 (owner refresh at 360 min), 4 (ffn capacity to rewrites), 6 (maintainer): apply them only when
  no completed ledger row in the last 6 h beats the best server_score before the window (the watchdog's gain6h FLAG says so and touches
  STRATEGY_NOW). Then apply the four with their tests and one LOG line each.

## Self-unblock standing rules
- A TASK file whose mtime is newer than its DONE line is re-read at the next run.
- workers/shared/RULES.md is the only rule text; a rule change = one RULES.md diff + one MAIN-NOTE to every alive worker; briefs point to it.
- Each strategist run audits 2 recent NONE / KILL / DEAD closures against RULES.md section 3 and reopens a failing one with a
  witness (a RESOLVE.md line + a worker or a fill slot); main answers OUTBOX 'RULING NEEDED' lines first and queues mi A,B for each 9 / 9
  device-correct top-priority lane it sees.
- No GOALS or ANALYSIS rewrite without a changed number.
- New briefs use the compact frame of the newer generators in main/briefs-auto/gen/ (<= 25 KB: header,
  read-first, base, door, rules by RULES.md path, LEVER, a <= 40-line CURRENT FACTS block); the older generators (80-110 KB) are retired.
- The watchdog's gain6h touches STRATEGY_NOW only on a new gain6h key or a strategy/LAST older than 3 h
  (main/scripts/watchdog.sh flag_strategy_gain; selftest S3b / S3c).
