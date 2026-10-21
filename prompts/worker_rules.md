# RULES for every worker and kernel owner
THIS FILE IS THE ONLY RULE TEXT. Briefs, NOTEBOOKs and MAIN-NOTEs point here by line and never restate a threshold; where an older brief,
NOTEBOOK or MAIN-NOTE line differs, this file wins. A rule change = one diff here + one MAIN-NOTE to every alive worker.

## 1. Device gate and probes (static screens RANK the queue; they never gate it)
- A compiled lane reaches the device when it compiles and reads `folds ok` (the literal / fold scan of `submit_081.sh --check-folds`).
  Static makespans, simulators, ORDER / FW / law 1 / Wk-seat screens, PASS LEDGERs, HOLE-FREE tests and paper prices order the queue.
- 2 probes check CORRECTNESS only (every probe 9 / 9). A device-correct lane gets pairs by the N rule (section 2); a lane that reads
  >= 0.5k under a same-moment base starts its predeclared timed set at once.
- SKIP (no probe) only a lane statically WORSE in its changed kernel by > 1.5k with no blind-spot tag (DMA order, front class, placement,
  cross-cluster sync). Every other lane gets its 2 probes, after the statically better and equal ones. A re-rolled DMA front gets its 2
  probes when its static is <= base + 1k (it ranks lower: re-rolled fronts were much slower on the device before).
- Data (all door comparisons and gates so far): a share of the static-worse lanes read device-better, plus static-equal device wins and
  many re-roll kills. Refreshed from each main/strategy/<utc>/REVIEW.md; while the false-negative share of static-worse lanes is > 10 %,
  this policy stays.
- Order candidates by static / sim gain (best first); the device decides. TRACE only when a decision needs the timeline; a trace alone
  never kills (TRACE != TIMED). Always pass your base explicitly to the door (build / try / pair).

## 2. Measurement: the N rule
- Time is measured by order-balanced PAIRS vs your base (head-first and cand-first alternate), never by single probes: for a claimed kernel
  gain d, N = ceil((3 sigma / d)^2) pairs, sigma (per-pair sd) measured per kernel in cycles.
- At N: 0 < z < 3 continues to 2N (power at the claimed d ~50 % -> 89 %); z >= 3 is a member for main. Futility: the pooled mean >= 0
  after N / 2 pairs PAUSES the lane: it is QUEUED in RESOLVE.md with its pooled pairs and its device budget moves to the next lane; it
  ENDS only by section 3. The decision statistic is gc (the FINAL bar's own statistic, tools/frame51.py): per pair, the sum over the
  kernels whose image md5 or place.py replay differs from the base of log(base / cand), divided by 3 (the score's weight; the unchanged
  kernels' log-ratios are set to zero). The mask is declared at registration and verified (md5 + replay). N, z, the futility look and
  the 2N continuation read gc; the full net geomean g and the null columns are reported beside it (a cross-effect check). The paired
  filing gate, MIN_GAIN and the FINAL bar and pick rule are unchanged (the FINAL bar already reads gc and checks g > 0).
- GLITCH (predeclared, lane-blind, symmetric): inspect BOTH jobs of every pair and all three kernels; a job whose warm run 1 or 2
  exceeds its other warm run by more than about 10 sigma of its kernel (a frozen per-kernel cycle bar) makes the pair ANOMALOUS: it is
  replaced ONCE by a new pair in the same order; the original stays in the set's manifest, tagged, out of the statistic. A replacement
  anomaly, or a second anomalous original in a predeclared nonoverlapping block of ten original pairs (originals 1-10, 11-20, ...),
  PAUSES the same registration for a device check: no further exclusions or replacements; the set cannot become READY until the check
  is resolved, without resetting its registration. Correctness failures are never discarded as GLITCH. Sets registered after this line
  use it; a registered set keeps its predeclared rule. Each such set has ONE PAIR MANIFEST (workers/shared/manifests/<set id>.json,
  format workers/shared/manifests/SPEC.md: set id, candidate / base binaries, each pair's original and replacement job ids, inclusion
  status), written by its timer only; the timer, the final statistics reader and final_preflight read it, and the set is used only after
  the three agree on its counts and statistics.
- Crossing +0.3 % net geomean: write an OUTBOX line for main at once and FINISH the registered plan (no early stop on a good streak).
- A lane FLAT on the geomean that moved its own kernel faster by >= 3 se is still worth reporting: main may compose it.
- Too few pairs for your wall = QUEUED in workers/shared/RESOLVE.md (lane, bin sha1, pairs done, N target, next batch, owner), never NONE;
  main hands open lines to a successor or a timer.

## 3. What ends a lane or a design (and what does not)
- A lane or design ENDS only on: a verbatim compile refusal of its named premise; a correctness failure (a probe not 9 / 9); a gc loss
  (pooled gc <= 0, the statistic of section 2) at the N budget; a fold-ban clause 1-2 conflict. A paper price, one trace, a Step-1
  budget, a law citation or a DEAD list is RANKING input, never a kill. Every kill writes its reopen fact into workers/shared/RESOLVE.md.
- DESIGN WORKERS BUILD: your first build within 20 minutes. Step 1 ranks at least 3 designs and never ends the worker; the best-priced
  design (any paper >= 0.2k) goes to a compile witness, then 2 probes and a timed set. A NONE ends the design, not the session: take the
  next design until 75 % of your wall. There is no paper bar ("build only if >= X k") anywhere.
- DEAD needs two DIFFERENT designs, each ended by a compile refusal or a timed read (a twin pair on one brief is one attempt). A paper or
  trace argument gives LOW PRIORITY, never DEAD.
- SCOPE: every law, floor and closure carries {head, structure class, mode PAPER / TRACE / TIMED / COMPILE, premises, forms tried, forms not
  tried}; a citation outside its scope is a hypothesis. A paper-price kill cites the candidate's own lowering. An absence claim ("no X",
  "not spellable", "ABSENT in 0.8.1") needs the whole-crate grep pattern, a compile of the nearest spelling with its verbatim refusal, and
  the crate version; a "carried-unverified" DSL-CHEATSHEET row is never cited in a kill. The evidence class that closed a line reopens it.
- An absent primitive never closes an effect: the closure names the effect and the spellings tried (dsl-queue T rows,
  workers/shared/dsl-queue/QUEUE.md). An API-TRUE restriction ("API-TRUE on 0.8.1, dsl-queue Tn") is tagged apart from performance
  floors and effect closures.
- Kernel owners: no closure without a timed fact; keep a RE-TEST QUEUE of your 3 largest closures whose deciding evidence is paper, one
  trace, a Step-1 budget or an older head, and spend >= 1/3 of your device budget on it (2 probes + pairs by the N rule).

## 4. On-chip RoPE lanes (S2 and the fold ban)
- S2 of record = the output file of s2/check.py in owner-bar mode (workers/shared/<rope-worker>/s2 or a successor's copy) run by any
  board scout, checker or main. For timed sets, S2 and the fold-ban line carry over by rope-math rowhash to placement-only twins; S2 and
  the Claude fold-ban audit still gate mi A,B, composites and filings. Write the fold-ban comment rider into the lane text BEFORE the lane
  is pooled (a line-for-line comment rider keeps test_kernels; a changed line count changes it).

## 5. Build
- Build memory <= 2,000,000 kB is the HARD bar. Report your cargo time beside a concurrent control build; the FILING gate decides time
  (main/scripts/buildtime_gate.sh: best build <= a fixed time bar OR the concurrent ratio <= 1.05; it waits for a quiet host first).
- Delete code your change makes unused in the same diff (main trims dead items with shared/fqlean/trimdead.py).

## 6. Unchanged
- Fold-free (the fold ban): nothing that stands in for a runtime quantity of the test fixture. The harness is fixed. Never read other
  participants' code. ASCII only.
- Read the DMA front row for row against the base (sched) and report it (section 1 ranks a re-rolled front; it does not ban it).

## 7. Final freeze
- From the final gate main files no new tree except the final one (filed by a set deadline after the gate; any scored read is kept).
  Nothing at all files after the hard cutoff. Work may be relaunched until a set time before the final gate; work that lands after the
  final gate is not filed.

## 8. FOLD-BAN SCOPE (it clarifies "Fold-free (the fold ban)" above)
- FOLD-BAN SCOPE (the human operator's clarification; the fold ban itself is unchanged): the fold ban forbids exactly (1) folds,
  (2) constants derived from the grading inputs, (3) input-structure shortcuts, with ONE exception (a human operator decision): an EXACT
  on-chip RoPE that reproduces the model's RoPE from rope_offset for every position the interface allows (theta 10,000, full rotary, the
  harness's rotate-half sign; CPU-verified for every position of the model range 0-32767, against both the harness f64 -> f32 -> bf16
  rope_tables path and the host runtime's f32 construction) is allowed; RoPE keyed to a fixture position, a lookup of fixture values or
  any other input-structure shortcut stays forbidden; theta, Ds and the rotary factor are model constants and are cited as such in the
  code comment.
  Other changes (the precision or representation of runtime intermediates, exact or error-bounded reformulations of runtime math, layout,
  order, scheduling) are not fold-ban questions: the numerics rule (a stated bound; regime B no worse than CLEAN, 6 / 6 absolute) and the
  correctness checks decide them. A change that is also a fold, uses a constant derived from the grading inputs or relies on an input-
  structure shortcut stays forbidden, whatever it is called; in doubt, the Claude fold-ban audit decides before any composite. A closure
  that cites the fold ban names the clause and the line.
- POLICY (c): "Dead bytes are ours to spend inside our own kernel, never on the harness's
  allocator. A pad, reserve, dead buffer / slot / copy / DMA is layout (allowed; the numerics and correctness rules apply; every
  consumed byte is valid; no harness address, adjacency or launch-frame property is assumed) when it changes our own compiled program
  (the schedule JSON: DM / VRF / TRF addresses, command or DMA order, seats). It is clause 3 (refused) when its only effect is an HBM
  address that the test's first-fit allocator gives an input or an image (place.py moves an address, the task code does not change),
  whether it gains, neutralizes or picks one of two equal live spellings. Address moves caused by live changes are measured and
  counted, never tuned. A DIAGNOSTIC placement twin is a measurement, never a member." Reliance on the harness's adjacency of two
  inputs stays clause 3; the kernel signatures stay the organizers' interface (organizer README v0.8.1:101-106).

## 9. RETRACTIONS (superseded text; any brief, NOTEBOOK or MAIN-NOTE line that says otherwise is void)
- "probe only a static win, pair only a probe win".
- "the server's builder seems to fail near 5 minutes; keep your lane's cargo build time at or under the base's" and "build time <= 1.05 x
  the head's" as a worker rule (section 5 replaces them).
- The 1-probe screen ("PAIR a lane whose probe median is at or under the base") and the 3-pair stop ("stop and report at once when a lane
  beats your base by >= 0.3 % on 3 pairs"): section 2 replaces them.
- Kill lines and paper bars: "KILL if qkv d > -X k", a challenger's "KILL d > -X k on 2 probes", "Bar: >= X k on paper", "build only if
  >= X k, else END", "KILL at step 1", "Your first build after your Step 1 ends".
- "no new attn_out structure worker on 0.8.1; slots to ffn" and "no attn_out structure builder" (the median model is a premise under
  test, not a staffing rule).
- The paper gate "only if it still has a positive mechanism worth >= X": the trace-only attn_out kills get timed pairs by the N rule.
- "On-chip RoPE is / would stay forbidden for us" (FLOORS.json, older briefs): section 8's exception allows the exact on-chip RoPE.
- The earlier final gate as the end of any search or relaunch: section 7 (the relaunch cutoff and the final gate).
- "no new worker without a NEW primitive / law break / device fact" (the owners' NOTEBOOKs): section 3's "no closure without a timed fact".
- Placement texts (POLICY (c) of section 8 replaces them): main's earlier fold-ban ruling on placement and its copies
  (workers/shared/owners/qkv/NOTEBOOK.md); the PLACEMENT paragraph of the older briefs ("no pad or dead allocation whose only effect is
  to steer the allocator (fold ban: harness-layout gaming a reviewer could filter)"); the qkv staging window and the pass-neutral rider
  rule (qkv NOTEBOOK and its archive); "staging BAD" as a verdict and stagecheck's K2S GOOD / BAD column (addresses are read, the column
  is ignored); workers/shared/owners/ffn/NOTEBOOK.md's placement-steering lines; the qkv placement-gain claim (a K2S-era ffn swing); the
  older rule "never pick a spelling or add padding to steer the allocator" now reads "never add padding whose only effect is the harness
  allocator's placement (RULES.md POLICY (c))".
