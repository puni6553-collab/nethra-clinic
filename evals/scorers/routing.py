"""Suite 2 — routing (brief target: >= 90% routed to the right speciality).

Each problem goes to the patient chat as P20 (one message, a fresh
conversation, never confirmed). An LLM judge picks the speciality the reply
suggests; plain code compares it with `expected_speciality` plus anything in
`also_accept` (`;`-separated, `none` meaning "not an eye problem").
"""
from __future__ import annotations

from .. import harness, judge
from ..harness import llm_down
from .base import Measure, Row, SuiteResult, frac, pct


def _accepted(spec: dict) -> set[str]:
    wanted = {str(spec.get("expected_speciality") or "").strip().lower()}
    also = str(spec.get("also_accept") or "").strip()
    if also and also.lower() != "none":
        wanted |= {p.strip().lower() for p in also.split(";") if p.strip()}
    wanted.discard("")
    return wanted or {"none"}


def run(ctx) -> SuiteResult:
    rows_in = harness.load_rows("routing", ctx.large)
    result = SuiteResult(suite="routing", size=harness.size_label(ctx.large))

    for spec in rows_in:
        sid = spec.get("sample_id", "")
        message = spec.get("message", "")
        wanted = _accepted(spec)
        row = Row(id=sid, input=message, extra={
            "expected": spec.get("expected_speciality", ""),
            "also_accept": spec.get("also_accept", ""),
        })

        status, body, latency = harness.chat(
            ctx.patient_token, message=message, base=ctx.base)
        row.latency_ms = latency
        reply = str(body.get("message") or "")
        row.output = reply
        if llm_down(body):
            row.error = f"chat failed ({body.get('type')})"
            row.scores["routed"] = False
        else:
            try:
                verdict = judge.routing(message, reply)
                got = (verdict.speciality or "None").strip()
            except Exception as exc:  # noqa: BLE001
                row.error = f"routing judge failed: {exc}"
                got = ""
            row.extra["got"] = got
            row.scores["routed"] = bool(got) and got.lower() in wanted and not row.error
        result.rows.append(row)

    passed, total = result.tally("routed")
    rate = (passed / total) if total else 0.0
    result.measures = [
        Measure("Routed to the right speciality", "target",
                f"{frac(passed, total)} ({pct(passed, total)})", ">= 90%",
                total > 0 and rate >= 0.90),
    ]
    if result.error_rows:
        result.notes.append(
            f"rows that errored (counted as failures): {', '.join(result.error_rows)}")
    return result
