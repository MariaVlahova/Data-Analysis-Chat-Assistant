"""Evaluation of the prototype (plan 09): golden cases, attacks, chaos runs and trajectory cases, scored with `evalkit` and gated on hard rules.

| Part | What it is |
|---|---|
| `datasets/` + `datasets.py` | The four data files and their loaders: `golden_cases.yaml`, `attacks.yaml`, `chaos_scenarios.yaml`, `trajectories.yaml` |
| `harness.py` | Plays a case, an attack, a chaos scenario or a trajectory case on the real app (`ScenarioRunner.play`, the session `chat` starts) and gathers the evidence |
| `metrics.py` | The checks, as `evalkit` metrics: invariants I1 to I6 (free, no reference), the path and memory checks of `trajectory.py` (free, no reference), and the checks against a case's reference (free, no model) |
| `trajectory.py` | Sequence and handoff accuracy, and state drift and memory consistency: pure functions of a turn's spans and of the conversation's checkpoint, against the design written out by hand |
| `evaluate.py` | Scores the samples, applies the hard gates, and compares with a baseline (`baseline.py`) |
| `run_golden.py`, `run_attacks.py`, `run_chaos.py`, `run_trajectory.py` | The commands: `python -m eval.run_golden --config config.local.yaml` |
| `report.py` | The PRD section 9 table, measured from a run's files, and a second table of the two rows beyond it (execution path, state drift) |

The model and the data come from the config, exactly as for `chat`. Offline, the same harness runs with a scripted model (`tests/test_attack_offline.py`,
`tests/test_golden_cases.py`, `tests/test_chaos_offline.py`, `tests/test_trajectory_offline.py`), so the machinery is proven without a network. What a real model does is a manual run.

Only the free layers of `evalkit` are used. The judge layers (a stronger model grading answers, human labels, Promptfoo, release gates) are the design of
plan 11 and are not built here. Nothing under `src/` imports `eval/`.
"""
