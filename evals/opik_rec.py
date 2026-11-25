"""Recording a run as an Opik experiment (handout R2/R3).

Everything here is optional and never fatal: with no `OPIK_API_KEY` the
runner simply writes `opik: disabled (no OPIK_API_KEY)` into summary.md and
the evals still run and score. With a key, each suite:

* keeps its golden set as an Opik dataset (rows refreshed when the CSV
  changes), so every experiment links to the rows it ran, and
* records the run through `opik.evaluate`. The rows have already been run
  and scored locally, so the task just replays the cached output and the
  scorers replay the cached scores — no extra LLM calls, and the numbers in
  Opik are exactly the numbers in summary.md.

The app's own traces still land in `netracare-evals` because the harness
exports OPIK_PROJECT_NAME for this run and for the eval server.
"""
from __future__ import annotations

import os
from typing import Any

from .scorers.base import SuiteResult

PROJECT = "netracare-evals"


def available() -> tuple[bool, str]:
    if not os.environ.get("OPIK_API_KEY", "").strip():
        return False, "disabled (no OPIK_API_KEY in the environment)"
    try:
        import opik  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"disabled (Opik SDK unavailable: {exc})"
    return True, f"recording into project {PROJECT}"


def _client():
    from opik import Opik

    return Opik(project_name=PROJECT)


def upload_datasets() -> str:
    """Put the six small (or the selected) golden sets into Opik (R2)."""
    ok, why = available()
    if not ok:
        return why
    from .harness import DATASETS, load_rows

    try:
        client = _client()
        names = [
            "red_flags", "routing", "policy_questions",
            "safety_prompts", "summary_cases", "judge_calibration",
            "booking_scenarios",
        ]
        for name in names:
            path = DATASETS / f"{name}.csv"
            if not path.exists():
                continue
            rows = load_rows(name)
            dataset = client.get_or_create_dataset(name=f"netracare-{name}")
            items = [
                {
                    "input": r.get("message") or r.get("question") or r.get("prompt")
                             or r.get("todays_reason") or "",
                    "expected_output": r.get("expected_answer")
                                       or r.get("expected_label")
                                       or r.get("expected_speciality")
                                       or r.get("expected_red_flag")
                                       or "",
                    "metadata": {"row_id": _row_id(name, r)},
                }
                for r in rows
            ]
            dataset.clear()
            dataset.insert(items)
        return f"uploaded {len(names)} datasets to {PROJECT}"
    except Exception as exc:  # noqa: BLE001 — reporting only
        return f"dataset upload failed: {exc}"


def _row_id(set_name: str, row: dict) -> str:
    for key in ("sample_id", "question_id", "prompt_id", "case_id",
                "scenario_id", "judge_id"):
        if row.get(key):
            return str(row[key])
    return f"{set_name}-{hash(tuple(sorted(row.items()))) & 0xFFFF:x}"


def _score_value(value: Any):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return value


def record_experiment(result: SuiteResult, dataset_name: str) -> str:
    """Record this suite's run as an experiment in Opik. Returns a status line."""
    ok, why = available()
    if not ok:
        return why
    try:
        from opik import evaluate
        from opik.evaluation.metrics import score_result

        client = _client()
        dataset = client.get_or_create_dataset(name=dataset_name)

        items = []
        cache: dict[str, dict] = {}
        for row in result.rows:
            items.append({
                "input": row.input,
                "expected_output": str(row.extra.get("expected", "")),
                "metadata": {"row_id": row.id},
            })
            cache[row.id] = {
                "output": row.output,
                "scores": row.scores,
                "error": row.error,
            }
        if {i["metadata"]["row_id"] for i in items} != {
            i.get("metadata", {}).get("row_id") for i in dataset.get_items()
        }:
            dataset.clear()
            dataset.insert(items)

        def task(item: dict) -> dict:
            row_id = item.get("metadata", {}).get("row_id")
            cached = cache.get(str(row_id))
            if cached is None:
                raise KeyError(f"no cached row for {row_id}")
            return {"output": cached["output"], "scores": cached["scores"],
                    "error": cached["error"]}

        score_names: list[str] = []
        for row in result.rows:
            for name in row.scores:
                if name not in score_names:
                    score_names.append(name)

        def make_scorer(name: str):
            def scorer(dataset_item, task_outputs, task_span=None):
                value = (task_outputs.get("scores") or {}).get(name)
                if value is None:
                    # No cached score for this row (an errored row): record a
                    # failed score, never None — Opik 2.x crashes on None.
                    return score_result.ScoreResult(
                        name=name, value=0.0, scoring_failed=True,
                        reason="no cached score for this row")
                return score_result.ScoreResult(name=name, value=_score_value(value))
            scorer.__name__ = f"score_{name}"
            return scorer

        # Handout R3: routing-small-2026-11-20-a1b2c3d
        from datetime import date as _date

        date = (result.finished or "")[:10] or _date.today().isoformat()
        commit = (result.git_commit or "unknown")[:7]
        experiment_name = f"{result.suite}-{result.size}-{date}-{commit}"
        experiment = evaluate(
            dataset=dataset,
            task=task,
            scoring_functions=[make_scorer(n) for n in score_names],
            experiment_name=experiment_name,
            project_name=PROJECT,
            verbose=0,
        )
        name = getattr(experiment, "experiment_name", None) or result.suite
        return f"experiment `{name}` in {PROJECT}"
    except Exception as exc:  # noqa: BLE001 — never fail the run over reporting
        return f"experiment recording failed: {exc}"
