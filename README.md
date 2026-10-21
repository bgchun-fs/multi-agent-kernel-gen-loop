# multi-agent-kernel-gen-loop

Prompts and measurement tools from the LLM agent loop described in *A Case Study of a Multi-Agent Kernel-Optimization Loop That Learns a New AI Accelerator* (Byonggon Chun). The loop optimized three Gemma-4-12B decode kernels on the FuriosaAI RNGD NPU in Round 1 of the NPU Model Optimization Competition at MICRO 2026.

The goal statements in the prompts were edited to target each kernel's calculated peak cycles (the paper's Experimental Setup, "Peak").

## Contents

| Path | What it is | Paper section |
|---|---|---|
| `prompts/main.md` | Main, the management loop that spawns and stops agents and files submissions | Roles |
| `prompts/strategist.md` | The strategist run: why the kernels are not near their peak, and which levers to pursue | Roles |
| `prompts/architect.md` | The architect: whole-kernel designs, idea cards and variant generators | Roles; Design Trees |
| `prompts/architect_redesign.md` | A new architect iteration that redesigns a kernel from the hardware budget | Roles |
| `prompts/kernel_owner.md` | A kernel owner, who keeps one kernel's notebook and reviews workers' changes | Roles |
| `prompts/worker_rules.md` | The device rules every worker brief carries | Roles |
| `prompts/helpers/auditor.md`, `prompts/helpers/maintainer.md` | Main's helper agents | Roles |
| `prompts/task_files/` | Two task files from the human operator, including the end-of-run request to stop exploring and refine the best candidates | Roles |
| `profiler/device_timeline.py` | Each cluster's device timeline from a run log, joined with the compiler's static schedule; diffs two runs | Device-Grounded Judging |
| `profiler/trace_sql.py` | Loads the timelines into SQLite so agents can query them with SQL | Device-Grounded Judging |
| `profiler/critical_path.py` | The device critical path of one kernel from a timeline | Device-Grounded Judging |
| `profiler/static_schedule.py`, `profiler/static_makespan.py` | Readers of the compiler's `--dump-schedule` JSON | What Did Not Work: the compiler's static schedule |
| `profiler/measured_rate_estimate.py` | Re-times a static schedule at measured device rates (gate 3 of a Design Tree) | Design Trees |
| `acceptance_test/acceptance_test.py` | The acceptance test: the candidate and the head in pairs, the score-level z test and early stopping | Device-Grounded Judging; Measurement Noise |

Not included: competition data, the kernels, the loop's orchestration code and the run records. The prompts name tools of the original environment that are not in this repository.

## Running the tools

Python 3.10 or newer, standard library only. Each tool has a self-test:

```sh
cd profiler
python3 device_timeline.py --selftest
python3 trace_sql.py --selftest
python3 critical_path.py --selftest
python3 static_schedule.py --selftest
python3 measured_rate_estimate.py --selftest
cd ../acceptance_test
python3 acceptance_test.py --selftest
```

The profiler reads run logs with span lines and the JSON that `cargo furiosa-opt compile --dump-schedule` writes; the docstring at the top of each file gives its usage.

## License

Apache License 2.0. See `LICENSE`.
