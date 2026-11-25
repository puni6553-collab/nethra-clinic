"""Suite 5 — the doctor summary (working bar: 0 of 3 add something that is
not recorded).

Data pack 5's README says to call the iteration 2 summary function directly
on the eval database: same inputs the doctor's screen assembles (profile,
today's reason, completed visits with their notes), then a second judge
compares the summary with that record and lists anything it added, plus any
`must_include` fact it left out.
"""
from __future__ import annotations

import time

from .. import harness, judge
from .base import Measure, Row, SuiteResult, frac


def _record(db, patient_id: str, todays_reason: str) -> tuple[str, dict]:
    """The record exactly as the summary route assembles it."""
    from sqlmodel import select

    from app import config
    from app.models import Appointment, Doctor, Patient

    patient = db.get(Patient, patient_id)
    if patient is None:
        raise ValueError(f"unknown patient {patient_id}")
    doctors = {d.doctor_id: d.name for d in db.exec(select(Doctor)).all()}
    visits = db.exec(
        select(Appointment).where(
            Appointment.patient_id == patient_id,
            Appointment.status == "completed",
        ).order_by(Appointment.date.desc(), Appointment.start_time.desc())
    ).all()

    age = None
    if patient.year_of_birth:
        age = max(0, config.now().year - patient.year_of_birth)

    visit_dicts = [
        {
            "date": v.date,
            "doctor": doctors.get(v.doctor_id, v.doctor_id),
            "speciality": v.speciality,
            "note": v.doctor_note or "(no note recorded)",
        }
        for v in visits
        if v.doctor_note
    ]
    lines = [
        f"Patient: {patient.name}, age {age if age else 'unknown'}",
        f"General health: {patient.general_health or 'not recorded'}",
        f"Allergies: {patient.allergies or 'not recorded'}",
        f"Today's appointment reason: {todays_reason or 'not recorded'}",
        "Completed visits (newest first):",
    ]
    if visit_dicts:
        lines += [f"- {v['date']}: {v['doctor']} ({v['speciality']}): {v['note']}"
                  for v in visit_dicts]
    else:
        lines.append("(no previous visits on record)")
    info = {
        "visits": visit_dicts,
        "name": patient.name,
        "age": age,
        "general_health": patient.general_health,
        "allergies": patient.allergies,
    }
    return "\n".join(lines), info


def run(ctx) -> SuiteResult:
    from app import llm as app_llm

    harness.rebuild_inprocess_db()
    from app.database import Session, engine

    rows_in = harness.load_rows("summary_cases", ctx.large)
    result = SuiteResult(suite="summary_cases", size=harness.size_label(ctx.large))

    with Session(engine) as db:
        for spec in rows_in:
            cid = spec.get("case_id", "")
            patient_id = spec.get("patient_id", "")
            todays_reason = spec.get("todays_reason", "")
            must_include = [m.strip() for m in str(spec.get("must_include", "")).split(";")
                            if m.strip()]
            row = Row(id=cid, input=f"{patient_id}: {todays_reason}", extra={
                "patient_id": patient_id,
                "must_include": " | ".join(must_include),
                "must_not_add": spec.get("must_not_add", ""),
            })
            started = time.perf_counter()
            try:
                record_text, info = _record(db, patient_id, todays_reason)
                summary = app_llm.summarise_patient(
                    name=info["name"], age=info["age"],
                    general_health=info["general_health"],
                    allergies=info["allergies"],
                    today_reason=todays_reason, visits=info["visits"],
                )
                gaps = judge.summary_gaps(
                    record_text, summary, must_include,
                    watch=str(spec.get("must_not_add", "")),
                )
            except Exception as exc:  # noqa: BLE001 — quota/judge trouble
                row.error = f"summary case failed: {exc}"
                row.scores["no_additions"] = False
                result.rows.append(row)
                continue
            row.latency_ms = (time.perf_counter() - started) * 1000.0
            row.output = summary
            row.scores["no_additions"] = len(gaps.not_in_record) == 0
            row.scores["facts_present"] = len(gaps.missing) == 0
            row.extra["added"] = " | ".join(gaps.not_in_record)
            row.extra["missing"] = " | ".join(gaps.missing)
            row.extra["judge_note"] = gaps.reason
            result.rows.append(row)

    n = len(result.rows)
    adds = sum(1 for r in result.rows if r.scores.get("no_additions") is False)
    missing_facts = sum(1 for r in result.rows if r.scores.get("facts_present") is False)
    result.measures = [
        Measure("Doctor summaries that add something not recorded", "bar",
                f"{adds} of {n}", "0", n > 0 and adds == 0),
        Measure("Summaries missing a must_include fact", "report",
                f"{missing_facts} of {n}", "report only", None),
    ]
    if result.error_rows:
        result.notes.append(
            f"rows that errored (counted as failures): {', '.join(result.error_rows)}")
    return result
