"""Suite 6 — booking scenarios with the rule audit (handout R5).

Every scenario starts from a fresh eval database and the pack's fixed clock
(2 Nov 2026, 10 AM): pack 1, its `appointments_seed.csv` (SEED_APPOINTMENTS=1)
and pack 2's past visits, exactly as data pack 4's README says. The turns are
played through POST /api/chat as the patient in the row — `[confirm]` and
`[decline]` send `confirm: true|false`, one message otherwise, never a second
message in a new thread.

After each scenario the database is checked two ways:

* **task success** — did it end with `expected_result`, and does
  `expected_appointment` hold (appointment clauses, `A1xx` states and the
  Staff-approval / urgent-case clauses)?
* **rule audit** — `evals/rule_audit.py` re-reads every appointment in the
  database against brief section 04, written apart from the API so a bug in
  the API cannot hide itself. Any appointment it reports is a rule break.

The audit's own self-test (a hand-made booking for Dr. Iyer on a Thursday)
runs once, so we know the audit can actually fail.
"""
from __future__ import annotations

import re
import time

from .. import harness, rule_audit
from ..harness import BOOKING_DB, call, chat, llm_down
from .base import Measure, Row, SuiteResult, frac

_APPT_CLAUSE = re.compile(
    r"^(?P<doctors>D\d+(?:\s+or\s+D\d+)?)?\s*"
    r"(?P<speciality>[A-Za-z ]+?)\s+"
    r"(?P<visit>new|follow_up)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})$"
)
_ID_CLAUSE = re.compile(r"^(?P<aid>A\d+)\s+(?P<state>cancelled|still booked)\b")


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in str(text or "").split(";") if c.strip()]


def _snapshot(base: str, staff_token: str, patient_id: str) -> dict:
    """All appointments (staff view), this patient's rows, approvals, urgent cases."""
    _, appts = call("GET", "/api/appointments", token=staff_token, base=base)
    _, approvals = call("GET", "/api/staff/approvals", token=staff_token, base=base)
    _, urgent = call("GET", "/api/urgent-cases", token=staff_token, base=base)
    appts = appts if isinstance(appts, list) else []
    approvals = approvals if isinstance(approvals, list) else []
    urgent = urgent if isinstance(urgent, list) else []
    return {
        "all": {int(a["id"]): a for a in appts},
        "patient": {int(a["id"]) for a in appts if a.get("patient_id") == patient_id},
        "pending": {int(a.get("appointment_id")) for a in approvals
                    if a.get("status") == "pending"},
        "urgent": [u for u in urgent if u.get("patient_id") == patient_id],
        "raw_urgent_count": len(urgent),
    }


def _play_turns(base: str, token: str, turns: str) -> dict:
    """Replay one scenario's turns. Never sends a second message per turn."""
    thread: str | None = None
    replies: list[dict] = []
    latencies: list[float] = []
    errors: list[str] = []
    for turn in [t.strip() for t in str(turns or "").split("||")]:
        if not turn:
            continue
        if turn == "[confirm]":
            status, body, lat = chat(token, thread=thread, confirm=True, base=base)
        elif turn == "[decline]":
            status, body, lat = chat(token, thread=thread, confirm=False, base=base)
        else:
            status, body, lat = chat(token, message=turn, base=base)
        thread = body.get("thread_id") or thread
        latencies.append(lat)
        detail = body.get("detail") if isinstance(body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else ""
        if code == "NOTHING_PENDING":
            continue  # pack README: skip a confirm with nothing waiting
        if llm_down(body):
            errors.append(str(body.get("type")))
        replies.append(body if isinstance(body, dict) else {})
    return {
        "replies": replies,
        "last": replies[-1] if replies else {},
        "latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "errors": errors,
    }


def _derive(before: dict, after: dict, expected: str, last_type: str) -> str:
    """What the database says happened, from before/after snapshots."""
    new_booked = [i for i, row in after["all"].items()
                  if row.get("status") == "booked" and i not in before["all"]]
    now_cancelled = [i for i, row in after["all"].items()
                     if row.get("status") == "cancelled"
                     and before["all"].get(i, {}).get("status") == "booked"]
    new_pending = [a for a in after["pending"] if a not in before["pending"]]
    urgent_new = len(after["urgent"]) > len(before["urgent"])

    if urgent_new and expected == "emergency":
        return "emergency"
    if new_pending:
        return "to_staff"
    if new_booked and now_cancelled:
        return "rescheduled"
    if now_cancelled:
        return "cancelled"
    if new_booked:
        return "booked"
    if expected in ("answered", "idk"):
        return {"answer": "answered", "idk": "idk"}.get(last_type, "no_change")
    if urgent_new:
        return "emergency"
    return "no_change"


def _check_appointments(text: str, *, before: dict, after: dict,
                        id_map: dict[str, int]) -> tuple[bool | None, str]:
    """Does `expected_appointment` hold? (None = nothing to check)."""
    clauses = _clauses(text)
    if not clauses:
        return None, ""

    details: list[str] = []
    ok = True
    for clause in clauses:
        match_id = _ID_CLAUSE.match(clause)
        spec = _APPT_CLAUSE.match(clause)
        if match_id:
            db_id = id_map.get(match_id.group("aid"))
            row = after["all"].get(db_id) if db_id else None
            want = match_id.group("state")
            if row is None:
                ok = False
                details.append(f"{match_id.group('aid')} missing from the database")
            elif want == "cancelled" and row.get("status") != "cancelled":
                ok = False
                details.append(f"{match_id.group('aid')} is {row.get('status')}, wanted cancelled")
            elif want == "still booked" and row.get("status") != "booked":
                ok = False
                details.append(f"{match_id.group('aid')} is {row.get('status')}, wanted booked")
            continue
        if spec:
            doctors = {d for d in re.findall(r"D\d+", spec.group("doctors") or "")}
            wanted = {
                "speciality": spec.group("speciality").strip(),
                "visit_type": spec.group("visit"),
                "date": spec.group("date"),
                "start_time": spec.group("time"),
            }
            found = None
            for db_id, row in after["all"].items():
                if db_id in before["all"] or row.get("status") != "booked":
                    continue  # the clause is about a booking made in THIS run
                if row.get("patient_id") not in before["patient_ids"]:
                    continue
                if all(row.get(k) == v for k, v in wanted.items()) and (
                    not doctors or row.get("doctor_id") in doctors
                ):
                    found = row
                    break
            if found is None:
                ok = False
                details.append(
                    "no new booking matching "
                    f"{spec.group('doctors') or ''} {wanted['speciality']} "
                    f"{wanted['visit_type']} {wanted['date']} {wanted['start_time']}".strip()
                )
            continue
        low = clause.lower()
        if "approval request" in low:
            if not (after["pending"] - before["pending"]):
                ok = False
                details.append("no Staff approval request was created")
            continue
        if low.startswith("no booking"):
            new_booked = [i for i, row in after["all"].items()
                          if row.get("status") == "booked" and i not in before["all"]]
            if new_booked:
                ok = False
                details.append(f"unexpected new booking(s): {sorted(new_booked)}")
            continue
        if "urgent case" in low:
            if len(after["urgent"]) <= len(before["urgent"]):
                ok = False
                details.append("no new urgent case for the patient")
            continue
        details.append(f"clause not checked: {clause}")
    return ok, "; ".join(details)


def run(ctx) -> SuiteResult:
    file_name = "agent_scenarios" if ctx.large else "booking_scenarios"
    rows_in = harness.load_rows(file_name)
    result = SuiteResult(suite="booking", size=harness.size_label(ctx.large))
    id_map = harness.seed_id_map()
    threshold = ">= 22 of 24" if ctx.large else ">= 7 of 8"
    bar_need = 22 if ctx.large else 7

    server = harness.EvalServer(BOOKING_DB)
    total_breaks = 0
    try:
        for spec in rows_in:
            sid = spec.get("scenario_id", "")
            row = Row(id=sid,
                      input=str(spec.get("turns", ""))[:300],
                      extra={
                          "expected": spec.get("expected_result", ""),
                          "reason_code": spec.get("reason_code", ""),
                          "expected_appointment": spec.get("expected_appointment", ""),
                      })
            try:
                server.start()
            except Exception as exc:  # noqa: BLE001
                row.error = f"eval server failed to start: {exc}"
                row.scores["result_ok"] = False
                result.rows.append(row)
                continue

            try:
                total_breaks += _run_scenario(server, spec, row, id_map)
                # The audit's own self-test, once: a hand-made booking for
                # Dr. Iyer on a Thursday, which she never works.
                if not result.extra.get("audit_selftest") and BOOKING_DB.exists():
                    caught, detail = rule_audit.audit_selftest(BOOKING_DB, clock())
                    result.extra["audit_selftest"] = {"caught": caught, "detail": detail}
                    if not caught:
                        result.notes.append(f"audit self-test FAILED: {detail}")
            except Exception as exc:  # noqa: BLE001 — one bad row never stops the run
                row.error = f"scenario failed: {exc}"
                row.scores["result_ok"] = False
            finally:
                server.stop()
                result.rows.append(row)
    finally:
        server.stop()

    # Every scenario failed before its self-test: run it on the last database
    # the runner still has on disk, so the audit is never untested.
    if not result.extra.get("audit_selftest") and BOOKING_DB.exists():
        caught, detail = rule_audit.audit_selftest(BOOKING_DB, clock())
        result.extra["audit_selftest"] = {"caught": caught, "detail": detail}
        if not caught:
            result.notes.append(f"audit self-test FAILED: {detail}")

    # --- measures -----------------------------------------------------------
    passed, total = result.tally("result_ok")
    breaks_selftest = result.extra.get("audit_selftest") or {}
    result.measures = [
        Measure("Bookings that break a clinic rule", "target",
                str(total_breaks), "0", total_breaks == 0),
        Measure("Booking scenarios that end with the expected result", "bar",
                frac(passed, total), threshold,
                total > 0 and passed >= bar_need),
        Measure("Rule audit catches a hand-made broken booking", "bar",
                "yes" if breaks_selftest.get("caught") else "no",
                "yes", bool(breaks_selftest.get("caught"))),
    ]
    if result.error_rows:
        result.notes.append(
            f"rows that errored (counted as failures): {', '.join(result.error_rows)}")
    return result


def _run_scenario(server, spec: dict, row: Row, id_map: dict[str, int]) -> int:
    """One scenario on a freshly started server. Returns its rule-break count."""
    base = server.base
    patient_id = str(spec.get("patient_id", ""))
    staff_token = harness.login(base, "staff", name="Reception")
    person = harness.patient_by_id(base, staff_token or "", patient_id)
    if not staff_token or person is None:
        row.error = f"patient {patient_id} not found in the seeded directory"
        row.scores["result_ok"] = False
        return 0
    token = harness.login(base, "patient", mobile=person["mobile"], name=person["name"])
    if not token:
        row.error = "patient login failed"
        row.scores["result_ok"] = False
        return 0

    before = _snapshot(base, staff_token, patient_id)
    before["patient_ids"] = {patient_id}
    played = _play_turns(base, token, spec.get("turns", ""))
    after = _snapshot(base, staff_token, patient_id)
    after["patient_ids"] = {patient_id}

    row.latency_ms = played["latency_ms"]
    last = played["last"]
    row.output = str(last.get("message") or "")
    row.extra["turns"] = len(played["replies"])
    row.extra["pending_action"] = last.get("pending_action") or ""
    if played["errors"]:
        row.error = "LLM unavailable on turn(s): " + ", ".join(played["errors"])

    expected = str(spec.get("expected_result", "")).strip()
    got = _derive(before, after, expected, str(last.get("type") or ""))
    row.extra["got"] = got
    row.scores["result_ok"] = (got == expected) and not row.error

    appt_ok, appt_detail = _check_appointments(
        spec.get("expected_appointment", ""),
        before=before, after=after, id_map=id_map)
    if appt_ok is not None:
        row.scores["appointment_ok"] = appt_ok and not row.error
        if appt_detail:
            row.extra["appointment_detail"] = appt_detail

    breaks = rule_audit.audit(BOOKING_DB, clock())
    row.extra["rule_breaks"] = len(breaks)
    if breaks:
        row.extra["breaks"] = " | ".join(breaks[:6])
    return len(breaks)


def clock():
    from app import config

    return config.now()
