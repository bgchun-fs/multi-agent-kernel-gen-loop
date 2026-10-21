DONE (deliverables 1-4 logged; first strategist run = dry run: strategy/<utc>/PROPOSAL.md, DRY_DONE + LAST touched; watchdog timer passes in watchdog.log, no false alert)
# TASK: make the loop steer itself (approved by the human operator, who will be away and must not be needed)

Why: today every change of direction came from outside the loop (the redesigns, the lean-structure diagnosis, the fan-out size, the DMA
analysis in ANALYSIS.md). The loop optimizes "beat the head" but never asks "why are the kernels not near their calculated peak, and is the approach wrong?", has no
mandate to change strategy, and does not notice when it stalls (the main failed silently for hours).

## The envelope (the human operator's pre-approval of strategy, like the submission rules for submissions)
The human operator approved this proposal with: "at most 24 workers, free choice of lever, the fold ban and the submission rules stay as
they are". So a strategist run MAY: rewrite main/ANALYSIS.md (numbers, causes, priorities); write new worker briefs and
launch or stop workers (at most 24 alive, in the sandbox, the door only); choose any lever; change the device rules for workers. It may NOT:
touch the fold ban, the submission rules, the harness, secrets, organizer contact; rewrite the decider / family loop code without
a selftest, a `git commit -s` and a release; spend the device on anything but worker / gate / submission jobs.

## Deliverables (all under main/; no R commit needed except as stated)
1. main/STRATEGIST.md: the brief of a strategist run. Steps: run strategy_data.sh; read its output, the worker REPORT.md files and SCOUT
   tables, and ANALYSIS.md; decide the top levers by the gap to the calculated peak per kernel (score = geomean: ln share per kernel); rewrite
   ANALYSIS.md (keep its section structure; update numbers, causes, priorities); write briefs for new workers (main/briefs-auto/) and
   launch them with ec2_worker.sh inside the envelope; stop workers whose lever the data shows dead; append one LOG.md line.
2. main/scripts/strategy_data.sh (+ a --selftest): writes main/strategy/<utc>/: the per-kernel gap table to the calculated peak
   with ln shares, the ledger tail, HEAD_REF, the head's DMA numbers per kernel (the door's `dma` verb logic, main/scripts/dma_static.py), the
   device load of the last hour (the probe dirs, door.log verbs), the alive / ended workers with each REPORT's first lines, and the best
   pair readings of the last 6 h (logs/paired/*.run.log).
3. The cadence, with NO change to main-runner.sh and NO service restart (a restart kills every worker and chain in the service cgroup):
   add to BRIEF.md step 1: "if main/STRATEGY_NOW exists, or main/strategy/LAST is older than 3 h, this run is a strategist run: follow
   main/STRATEGIST.md (then remove STRATEGY_NOW and touch strategy/LAST); a submission in flight is still handled first".
4. main/scripts/watchdog.sh (+ --selftest) and a timer every 30 min (NOT in the
   main's cgroup). It only detects and flags, never edits code: (a) the last 2 main runs ended rc != 0 -> check the known causes (the claude
   CLI path / version, the API key file), apply the one-line fix if it is one of them, else record it in the run log; (b) no score gain in 6 h, or fewer than 18
   workers alive while any lever in ANALYSIS.md is open (the BRIEF standing fan-out floor), or the device idle over 30 min while workers exist -> touch main/STRATEGY_NOW; (c) disk under 15 % free -> run
   main/scripts/slim_targets.py; (d) the last ledger row failed and no chain_submit.sh / a30_submit.sh process runs -> write main/ALERT
   (the main reads it first). Log every pass to main/watchdog.log.

## Verification (before this counts as done)
- strategy_data.sh --selftest and watchdog.sh --selftest pass; one real strategy_data.sh pass writes a complete main/strategy/<utc>/.
- The FIRST strategist run is a DRY RUN: it writes main/strategy/<utc>/PROPOSAL.md (what it would change and why) and changes nothing else.
  The next strategist run acts on it inside the envelope.
- The watchdog timer runs one pass (systemctl list-timers shows it; watchdog.log has the pass) with no false alert.
- Record each deliverable in LOG.md; when all are done, mark this file DONE on its first line.

## Priority
After any submission in flight and ANALYSIS.md section 5 (the fan-out 2 launch), before new follow-up work. It may span two runs.

## Additions (the human operator): goals that update themselves, kernel owners
- The strategist also maintains workers/shared/GOALS.md by the rules in that file on every strategist run (reached -> ratchet to the next
  target with a new 48 h deadline; missed or projected to miss -> move workers to that kernel, add a fresh-design worker, new 24 h deadline).
- The strategist sizes the fan-out by BRIEF standing order 1 and keeps the 3 kernel owners (main/briefs-owners/*.md, 720 min) alive,
  relaunching an ended owner at once; it launches the experiments an owner lists in its NOTEBOOK's 'Experiments for others' section.
- The watchdog floor: fewer than 12 workers alive while any goal is open, or a kernel owner not alive -> touch main/STRATEGY_NOW.
