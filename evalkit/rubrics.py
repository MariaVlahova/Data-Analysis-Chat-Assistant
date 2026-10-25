"""Rubric definitions for the judge-graded layers, loaded from a CSV or an Excel (.xlsx) file.

This is the other half of the plugin interface, alongside ``evalkit.registry``.
A static or computation metric is a function; a judge-graded metric is a row
of *text* plus a shared scoring routine (``evalkit.graded``) -- there is no
per-rubric code to write. Add a new judge-graded check by adding a row.

Rubric text lives in data rather than code so it can be reviewed, diffed, and
handed **verbatim** to human raters -- an inter-rater agreement study is only
meaningful if the raters and the judge saw exactly the same wording. The two
formats share one validation path (``_rows_from_csv`` / ``_rows_from_xlsx``
both yield plain ``dict[str, str]`` rows into ``_build_rubrics``): a CSV diffs
cleanly in git, an .xlsx is easier to hand to a non-technical reviewer for a
pass of comments and column widths. Pick either; both must have the header
row below, in this order.

Columns:

``registry``
    One of ``STANDARD_RUBRICS`` (generic quality axes every project can share
    -- helpfulness, tool use, hallucination, safety), ``JUDGE_RUBRICS`` (fixed
    rubrics specific to your system), or ``ADAPTIVE_RUBRICS`` (the judge
    generates per-sample criteria before scoring -- see ``evalkit.graded``).
``key``
    Unique metric name.
``name``, ``description``, ``passing_criteria``, ``failing_criteria``,
``applicability_hint``, ``score_interpretation``
    The prompt text, assembled by ``Rubric.as_prompt_block()``.
``category``
    A ``Focus`` value (``correctness``, ``security``, ``safety``,
    ``compliance``, ``robustness``, ``efficiency``) -- or any other string,
    mapped through the ``category_to_focus`` argument to ``load_rubrics`` if
    your CSV uses its own vocabulary.
``stage``
    Any string you use to name pipeline stages in this project.
``pipelines``
    Comma-separated pipeline names this rubric applies to.
``threshold``
    0-1 passing threshold on the normalized (0-1) score.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from evalkit.schema import Focus, Stage

EXPECTED_COLUMNS = [
    "registry",
    "key",
    "name",
    "description",
    "passing_criteria",
    "failing_criteria",
    "applicability_hint",
    "category",
    "score_interpretation",
    "stage",
    "pipelines",
    "threshold",
]

VALID_REGISTRIES = ("STANDARD_RUBRICS", "JUDGE_RUBRICS", "ADAPTIVE_RUBRICS")

_OVERFLOW = "__overflow__"

#: Default category vocabulary: the CSV's ``category`` column is read straight
#: as a ``Focus`` value. Pass your own mapping to ``load_rubrics`` if your CSV
#: uses different category names (e.g. domain-specific labels like
#: "extraction" or "retrieval" that should all roll up under CORRECTNESS).
DEFAULT_CATEGORY_TO_FOCUS: dict[str, Focus] = {focus.value: focus for focus in Focus}


class RubricError(ValueError):
    """The rubric CSV is malformed."""


@dataclass(frozen=True)
class Rubric:
    """One rubric: what it judges, what passes, and which stage it blames."""

    key: str
    registry: str
    name: str
    description: str
    passing_criteria: str
    failing_criteria: str
    applicability_hint: str
    category: str
    focus: Focus
    score_interpretation: str
    stage: Stage
    pipelines: tuple[str, ...]
    threshold: float

    @property
    def is_adaptive(self) -> bool:
        return self.registry == "ADAPTIVE_RUBRICS"

    def applies_to(self, pipeline: str) -> bool:
        return pipeline in self.pipelines

    def as_prompt_block(self) -> str:
        """The rubric exactly as both the judge and a human rater see it."""
        lines = [f"RUBRIC: {self.name}", f"WHAT IT JUDGES: {self.description}"]
        if self.passing_criteria:
            lines.append(f"A HIGH SCORE MEANS: {self.passing_criteria}")
        if self.failing_criteria:
            lines.append(f"A LOW SCORE MEANS: {self.failing_criteria}")
        if self.applicability_hint:
            lines.append(f"WHEN IT APPLIES: {self.applicability_hint}")
        lines.append(f"SCALE: {self.score_interpretation}")
        return "\n".join(lines)


def _rows_from_csv(path: Path) -> list[tuple[int, dict[str, str | None]]]:
    """Every data row of a rubric CSV, 1:1 with its line number."""
    with open(path, newline="", encoding="utf-8") as handle:
        # restkey turns an unquoted comma into a detectable error instead of a
        # silent column shift that would corrupt every rubric after it.
        reader = csv.DictReader(handle, restkey=_OVERFLOW, restval=None)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise RubricError(
                f"{path.name} header mismatch: expected {EXPECTED_COLUMNS}, got {reader.fieldnames}"
            )
        return list(enumerate(reader, start=2))


def _rows_from_xlsx(path: Path) -> list[tuple[int, dict[str, str | None]]]:
    """Every data row of the first sheet of a rubric workbook, 1:1 with its row number."""
    from openpyxl import load_workbook  # heavy import, kept out of the CSV-only path

    sheet = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    header = [str(cell).strip() if cell is not None else None for cell in header or []]
    if header != EXPECTED_COLUMNS:
        raise RubricError(f"{path.name} header mismatch: expected {EXPECTED_COLUMNS}, got {header}")
    out: list[tuple[int, dict[str, str | None]]] = []
    for line_no, raw in enumerate(rows, start=2):
        if raw is None or all(cell is None for cell in raw):
            continue  # a blank row (Excel often pads a sheet with empty trailing rows)
        cells = list(raw) + [None] * (len(EXPECTED_COLUMNS) - len(raw))
        row: dict[str, str | None] = {
            column: (str(cell) if cell is not None else None)
            for column, cell in zip(EXPECTED_COLUMNS, cells)
        }
        if len(raw) > len(EXPECTED_COLUMNS) and any(cell is not None for cell in raw[len(EXPECTED_COLUMNS):]):
            row[_OVERFLOW] = ", ".join(str(cell) for cell in raw[len(EXPECTED_COLUMNS):] if cell is not None)
        out.append((line_no, row))
    return out


def load_rubrics(
    path: str | Path,
    *,
    category_to_focus: dict[str, Focus] | None = None,
) -> dict[str, Rubric]:
    """Read every rubric from ``path`` (``.csv`` or ``.xlsx``), keyed by its key.

    Raises :class:`RubricError` on any structural problem -- a malformed
    rubric file should fail loudly at startup, not silently drop a check.
    """
    path = Path(path)
    category_map = category_to_focus if category_to_focus is not None else DEFAULT_CATEGORY_TO_FOCUS
    rubrics: dict[str, Rubric] = {}

    if path.suffix.lower() == ".xlsx":
        rows = _rows_from_xlsx(path)
    elif path.suffix.lower() == ".csv":
        rows = _rows_from_csv(path)
    else:
        raise RubricError(f"{path.name}: unsupported rubric file type (expected .csv or .xlsx)")

    for line_no, row in rows:
        if row.get(_OVERFLOW):
            raise RubricError(
                f"{path.name} line {line_no} has too many columns (unquoted comma?): {row[_OVERFLOW]!r}"
            )
        missing = [column for column in EXPECTED_COLUMNS if row.get(column) is None]
        if missing:
            raise RubricError(f"{path.name} line {line_no} is missing columns: {missing}")

        registry = row["registry"].strip()
        if registry not in VALID_REGISTRIES:
            raise RubricError(
                f"{path.name} line {line_no}: registry {registry!r} is not one of {list(VALID_REGISTRIES)}"
            )
        key = row["key"].strip()
        if not key:
            raise RubricError(f"{path.name} line {line_no}: key is empty")
        if key in rubrics:
            raise RubricError(f"{path.name} line {line_no}: duplicate key {key!r}")

        stage = row["stage"].strip()
        if not stage:
            raise RubricError(f"{path.name} line {line_no}: stage is empty")

        pipelines = tuple(p.strip() for p in row["pipelines"].split(",") if p.strip())
        if not pipelines:
            raise RubricError(f"{path.name} line {line_no}: no pipelines declared")

        try:
            threshold = float(row["threshold"])
        except ValueError as exc:
            raise RubricError(
                f"{path.name} line {line_no}: threshold {row['threshold']!r} is not a number"
            ) from exc
        if not 0.0 <= threshold <= 1.0:
            raise RubricError(f"{path.name} line {line_no}: threshold {threshold} is outside [0, 1]")

        category = row["category"].strip()
        focus = category_map.get(category)
        if focus is None:
            raise RubricError(
                f"{path.name} line {line_no}: category {category!r} is not one of {sorted(category_map)}"
            )

        rubrics[key] = Rubric(
            key=key,
            registry=registry,
            name=row["name"].strip(),
            description=row["description"].strip(),
            passing_criteria=row["passing_criteria"].strip(),
            failing_criteria=row["failing_criteria"].strip(),
            applicability_hint=row["applicability_hint"].strip(),
            focus=focus,
            category=category,
            score_interpretation=row["score_interpretation"].strip(),
            stage=stage,
            pipelines=pipelines,
            threshold=threshold,
        )

    if not rubrics:
        raise RubricError(f"{path} defines no rubrics")
    return rubrics


def by_registry(rubrics: dict[str, Rubric], registry: str) -> dict[str, Rubric]:
    return {key: r for key, r in rubrics.items() if r.registry == registry}


def write_template(path: str | Path) -> Path:
    """Write an empty rubric CSV with just the header -- a starting point for a new project."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXPECTED_COLUMNS)
    return path


__all__ = [
    "DEFAULT_CATEGORY_TO_FOCUS",
    "EXPECTED_COLUMNS",
    "VALID_REGISTRIES",
    "Rubric",
    "RubricError",
    "by_registry",
    "load_rubrics",
    "write_template",
]
