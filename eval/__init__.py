"""Evaluation of the prototype (plan 09): golden cases, attacks and chaos runs, scored with `evalkit` and gated on hard rules.

| Part | What it is |
|---|---|
| `datasets/` + `datasets.py` | The three data files and their loaders: `golden_cases.yaml`, `attacks.yaml`, `chaos_scenarios.yaml` |
| `harness.py` | Plays a case, an attack or a chaos scenario on the real app (`ScenarioRunner.play`, the session `chat` starts) and gathers the evidence |
| `metrics.py` | The checks, as `evalkit` metrics: invariants I1 to I6 (free, no reference) and the checks against a case's reference (free, no model) |
| `evaluate.py` | Scores the samples, applies the hard gates, and compares with a baseline (`baseline.py`) |
| `run_golden.py`, `run_attacks.py`, `run_chaos.py` | The commands: `python -m eval.run_golden --config config.local.yaml` |
| `report.py` | The PRD section 9 table, measured from a run's files |

The model and the data come from the config, exactly as for `chat`. Offline, the same harness runs with a scripted model (`tests/test_attack_offline.py`,
`tests/test_golden_cases.py`, `tests/test_chaos_offline.py`), so the machinery is proven without a network. What a real model does is a manual run.

Only the free layers of `evalkit` are used. The judge layers (a stronger model grading answers, human labels, Promptfoo, release gates) are the design of
plan 11 and are not built here. Nothing under `src/` imports `eval/`.
"""
