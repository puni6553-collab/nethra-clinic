"""Build `evals/results/summary.md` from the suite results on disk (R3, R6, R7).

The report is rebuilt from the JSON each suite leaves behind, so running a
single suite updates its section and leaves the rest alone. Anything written
by hand under `## Changes` survives every rebuild — that is where the
before/after of a fix goes (handout R7).
"""
from __future__ import annotations

from . import harness
from .scorers.base import RESULTS, SuiteResult

ORDER = ["red_flags", "routing", "policy_questions", "safety",
         "summary_cases", "booking"]

TARGET_LABELS = {
    "red_flags": "Red flags get the emergency response",
    "routing": "Routed to the right speciality",
    "policy_questions": "Policy answers cite a document",
    "safety": "Answers that name a condition or suggest treatment",
    "booking": "Bookings that break a clinic rule",
}

PATH = RESULTS / "summary.md"


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _loads() -> list[SuiteResult]:
    out = []
    for name in ORDER:
        result = SuiteResult.load(name)
        if result is not None:
            out.append(result)
    return out


def _target_measure(result: SuiteResult):
    for m in result.measures:
        if m.kind == "target":
            return m
    return None


def _pct_line(values: list[float]) -> str:
    if not values:
        return "n/a"
    return (f"p50 {harness.percentile(values, 50)} ms, "
            f"p95 {harness.percentile(values, 95)} ms over {len(values)} timed rows")


def _preserved_changes() -> str:
    """Keep whatever a human wrote under `## Changes` in the old report."""
    return _preserved_section("## Changes", (
        "_No changes recorded yet — after your first full run, add one "
        "fix and its before/after numbers here._"
    ))


def _preserved_section(heading: str, fallback: str) -> str:
    """Keep whatever a human wrote under `heading` in the old report."""
    if not PATH.exists():
        return fallback
    lines = PATH.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == heading
            continue
        if in_section:
            kept.append(line)
    body = "\n".join(kept).strip()
    return body or fallback


def _miss_explanations(targets_missed: list[str]) -> str:
    """Hand-written notes for each missed target, preserved across rebuilds.

    The scaffold asks for a short note per miss (which rows, why, what to
    try next). Humans append those under `## Misses to explain`; this keeps
    them when the report is regenerated.
    """
    fallback = (
        "Each of these needs a short note here (which rows fail, why "
        "you think they fail, what you would try next), or a fix and "
        "a re-run:"
    )
    body = _preserved_section("## Misses to explain", "")
    if not body:
        return fallback + "\n\n" + "\n".join(f"- {m}" for m in targets_missed)
    # Keep the human's notes; still list any target that has no note yet.
    noted = body.lower()
    missing = [m for m in targets_missed if m.split("=")[0].strip().lower() not in noted]
    out = [body]
    if missing:
        out += ["", "_Still needs a note:_"] + [f"- {m}" for m in missing]
    return "\n".join(out)


def build() -> "SuiteResult | None":
    """Write summary.md. Returns the last suite loaded (or None)."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    results = _loads()

    lines: list[str] = ["# NetraCare evals — summary", ""]

    if not results:
        lines += ["_No suite results yet — run `python run_evals.py`._", ""]
        PATH.write_text("\n".join(lines), encoding="utf-8")
        return None

    sizes = sorted({r.size for r in results})
    commits = sorted({r.git_commit for r in results if r.git_commit})
    models = sorted({r.model for r in results if r.model})
    finished = max((r.finished for r in results if r.finished), default="")
    opik_lines = sorted({str(r.extra.get("opik", "")) for r in results
                         if r.extra.get("opik")})
    lines += [
        f"- Run: {finished or 'unknown'} · git `{', '.join(commits) or 'unknown'}`"
        f" · model {', '.join(models) or 'unknown'}",
        f"- Set size: {', '.join(sizes)} · clock {harness.CLOCK} (APP_NOW)"
        " · eval database, never the live one",
        "- Opik: " + ("; ".join(opik_lines) if opik_lines else "not recorded"),
        "",
        "## The five targets (brief section 09)",
        "",
        "| Target | Result | Target | Status |",
        "| --- | --- | --- | --- |",
    ]
    by_suite = {r.suite: r for r in results}
    targets_missed: list[str] = []
    for suite, label in TARGET_LABELS.items():
        result = by_suite.get(suite)
        measure = _target_measure(result) if result else None
        if measure is None:
            lines.append(f"| {_cell(label)} | not run this time | — | n/a |")
            continue
        lines.append(f"| {_cell(label)} | {_cell(measure.value)} "
                     f"| {_cell(measure.threshold)} | {measure.label} |")
        if measure.met is False:
            targets_missed.append(f"{suite}: {measure.name} = {measure.value}")
    lines.append("")

    lines += [
        "## Working bars (course bars, not brief targets)",
        "",
        "| Suite | Measure | Result | Bar | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        for m in result.measures:
            if m.kind != "bar":
                continue
            lines.append(f"| {result.suite} | {_cell(m.name)} | {_cell(m.value)} "
                         f"| {_cell(m.threshold)} | {m.label} |")
    lines.append("")

    report_rows = [(r.suite, m) for r in results for m in r.measures
                   if m.kind == "report"]
    if report_rows:
        lines += ["## Report only", "",
                  "| Suite | Measure | Value |", "| --- | --- | --- |"]
        for suite, m in report_rows:
            lines.append(f"| {suite} | {_cell(m.name)} | {_cell(m.value)} |")
        lines.append("")

    # --- latency (and the honest cost line) ---------------------------------
    lines += ["## Latency and cost", ""]
    all_lat: list[float] = []
    for result in results:
        vals = result.timed_latencies()
        if vals:
            all_lat.extend(vals)
            lines.append(f"- {result.suite}: {_pct_line(vals)}")
    lines += [f"- All timed rows: {_pct_line(all_lat)}",
              "- Cost per chat turn: not tracked locally — Opik records cost, "
              "tokens and latency per trace (needs `OPIK_API_KEY`).", ""]

    # --- RAGAS ---------------------------------------------------------------
    lines += ["## RAGAS (policy answers)", ""]
    policy = by_suite.get("policy_questions")
    ragas = (policy.extra.get("ragas") if policy else None) or {}
    block = (policy.extra.get("ragas_block") if policy else None) or {}
    if not policy:
        lines += ["_Policy suite not run this time._", ""]
    elif ragas.get("averages"):
        lines += ["| Metric | Average |", "| --- | --- |"]
        for key in sorted(ragas["averages"]):
            lines.append(f"| {key} | {ragas['averages'][key]:.4f} |")
        lines += ["", "Three lowest-scoring rows:", "",
                  "| Row | Question | Mean | Scores |", "| --- | --- | --- | --- |"]
        for low in ragas.get("lowest", []):
            scores = ", ".join(f"{k.replace('ragas_', '')}={v}"
                               for k, v in (low.get("scores") or {}).items())
            lines.append(f"| {low.get('id')} | {_cell(_short(low.get('question', ''), 60))} "
                         f"| {low.get('mean')} | {_cell(scores)} |")
        lines.append("")
    else:
        reason = block.get("error") or "not run this run"
        lines += [f"_RAGAS did not run: {reason}_", ""]

    # --- runs, failures, notes ----------------------------------------------
    lines += ["## Runs", "",
              "| Suite | Size | Rows | Failed | Errored | Started | Git | Opik |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for result in results:
        lines.append(f"| {result.suite} | {result.size} | {len(result.rows)} "
                     f"| {len(result.failed_rows)} | {len(result.error_rows)} "
                     f"| {_cell(result.started)} | `{_cell(result.git_commit)}` "
                     f"| {_cell(_short(str(result.extra.get('opik', '')), 40))} |")
    lines.append("")

    failing = [(r, row) for r in results for row in r.rows if row.failed]
    lines += ["## Failing rows", ""]
    if not failing:
        lines += ["None this run.", ""]
    else:
        for result, row in failing:
            false_scores = [k for k, v in row.scores.items() if v is False]
            detail = []
            if false_scores:
                detail.append("scores: " + ", ".join(false_scores))
            for key in ("miss", "got", "why", "appointment_detail", "breaks",
                        "added", "missing", "judge_note", "detail"):
                if row.extra.get(key):
                    detail.append(f"{key}: {_short(str(row.extra[key]), 160)}")
            if row.error:
                detail.append(f"error: {_short(row.error, 160)}")
            lines.append(f"- **{result.suite}** · `{row.id}` — {'; '.join(detail)}")
        lines.append("")

    notes = [(r.suite, note) for r in results for note in r.notes]
    if notes:
        lines += ["## Notes", ""]
        for suite, note in notes:
            lines.append(f"- **{suite}**: {_short(note, 300)}")
        lines.append("")

    # --- misses (targets first, then working bars) + Changes --------------
    bar_missed = [
        f"{r.suite}: {m.name} = {m.value}"
        for r in results for m in r.measures
        if m.kind == "bar" and m.met is False
    ]
    # Keep hand-written notes whenever any target or bar is missed, and
    # also when a previous report already had the section (so a later
    # all-green run does not wipe the explanations).
    if targets_missed or bar_missed or _preserved_section("## Misses to explain", ""):
        lines += ["## Misses to explain", "",
                  _miss_explanations(targets_missed + bar_missed), ""]

    lines += ["## Changes", "", _preserved_changes(), ""]

    PATH.write_text("\n".join(lines), encoding="utf-8")
    return results[-1]
