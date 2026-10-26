"""evalkit -- a reusable, layered, pluggable evaluation framework for LLM/agent pipelines.

Extracted from a production evaluation harness and generalized so it carries
no dependency on any one project's code, models, or metrics. The pieces:

* ``schema`` -- ``MetricResult`` / ``SampleResult`` / ``MetricSummary``: the
  one result shape every metric, from any layer, normalizes into.
* ``session`` -- ``EvalSession`` / ``EvalEvent`` / ``EvalTurn``: the one input
  shape every metric reads. Write a small adapter for your agent framework
  to produce these; nothing downstream needs to know where they came from.
* ``context`` -- ``ValidationContext``: the read-only view a metric function
  scores against.
* ``registry`` -- ``MetricRegistry``: the plugin interface for deterministic
  (static/computation) metrics. Register a function with a decorator; that is
  the entire extension mechanism.
* ``rubrics`` + ``graded`` -- the plugin interface for judge-graded metrics:
  add a row to a CSV, and only write code for rubrics that do not always apply.
* ``judge`` -- ``JudgeClient``: provider-neutral judge calling, with repeats,
  caching, and cost accounting. Bring your own model via a ``judge_factory``.
* ``config`` + ``runner`` -- ``EvaluationConfig`` / ``EvaluationRunner``: wires
  your registries and rubrics together, runs the layers in cost order, gates
  the expensive ones behind the free ones, aggregates with bootstrap
  confidence intervals, and evaluates a pass/fail gate.
* ``report`` -- writes a run to JSON/CSV artifacts.
* ``testing`` -- scripted judge doubles for hermetic tests.

In this project nothing under ``src/`` imports it except ``src/evaluation/`` (the chat's ``/eval`` command, which scores real turns with the same
rubrics), and it imports nothing from ``src/`` (``tests/test_evalkit.py`` checks both). The project's own metrics and adapters live in ``eval/``
(plan 09).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
