"""NetraCare's MCP server (iteration 6, R1/R2).

A separate program, alongside the API, that lets partner apps (health
aggregators and the like) find free slots and book a new consultation at
NetraCare. It serves two tools over Streamable HTTP at
http://localhost:8001/mcp:

    find_slots(speciality, date, date_end?, doctor_id?)
    book_appointment(patient_name, mobile, speciality, doctor_id,
                     date, start_time, reason)

Run it (from the project root, after the API is up or on its own):

    $env:LLM_MODEL='gemini-3.5-flash-lite'   # if your shell exports a model
    .\\venv\\Scripts\\python backend\\mcp_server.py

Two rules it never breaks:

* It calls the same booking code as the website and the chat agent
  (`app.actions.book`, which runs the red-flag check and then the one
  rules engine in `app.rules`). It never writes a second copy of a rule,
  and it never writes to the database on its own.
* It only finds slots and books. It never shows appointments, visit
  notes, summaries or any patient's details.

The patient is resolved by mobile + name through the same helper website
login uses (`app.security.resolve_patient`): a new mobile creates a new
patient, a known mobile with a different name is refused with
PATIENT_DETAILS_MISMATCH, so nobody can book into someone else's record.

Inspection:   npx @modelcontextprotocol/inspector
              Transport "Streamable HTTP", URL http://localhost:8001/mcp
"""
from __future__ import annotations

import sys
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import Any

# Run as a script from anywhere: make backend/ importable as the package root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import actions, config, rules, seed, slots  # noqa: E402
from app.database import engine, init_db  # noqa: E402
from app.models import Doctor  # noqa: E402
from app.rules import Refusal  # noqa: E402
from app.security import DetailsMismatch, resolve_patient  # noqa: E402

# The server a stranger's agent connects to. FastMCP mounts the tools at
# http://localhost:8001/mcp (the SDK's default /mcp path). Port comes from
# config.MCP_PORT (env MCP_PORT) so checks can run on their own port.
mcp = FastMCP("NetraCare", host="localhost", port=config.MCP_PORT)

# find_slots never returns more than this, so one call stays readable.
MAX_SLOTS = 30

SPECIALITIES = (
    "Cataract, Glaucoma, Retina, Cornea, Paediatric, Refractive, Dry eye, "
    "Squint or General"
)


def _db() -> Session:
    """A session per tool call: the server has no request scope."""
    return Session(engine)


def _doctors_for(db: Session, *, doctor_id: str, speciality: str) -> list[Doctor]:
    """The doctors one find_slots call may offer (both filters narrow)."""
    all_docs = list(db.exec(select(Doctor).order_by(Doctor.doctor_id)).all())
    if doctor_id:
        return [d for d in all_docs if d.doctor_id == doctor_id]
    if speciality:
        return [
            d for d in all_docs
            if any(s.strip().lower() == speciality.strip().lower()
                   for s in d.specialities.split(";"))
        ]
    return all_docs


def _refused(code: str, message: str) -> dict:
    """The one refusal shape every tool result uses."""
    return {"status": "refused", "code": code, "reason_code": code, "message": message}


def _explain_empty(db: Session, doctors: list[Doctor], day: date_cls) -> str:
    """Plain words for a day with nothing free — the rules engine explains.

    M02 wants a clear message, not an error: the rules engine is asked why
    (closed day, doctor off, surgery, leave, window) and its own plain
    message is passed through.
    """
    today = config.now().date()
    if day < today:
        return "That date has already passed. Please choose a future date."
    if day > today + timedelta(days=config.BOOKING_WINDOW_DAYS):
        return (
            f"Appointments can only be booked up to "
            f"{config.BOOKING_WINDOW_DAYS} days ahead."
        )
    try:
        rules.check_clinic_open(db, day)
    except Refusal as err:
        return err.message

    reasons: list[str] = []
    for doc in doctors:
        doc_reasons: list[str] = []
        for session in ("AM", "PM"):
            try:
                rules.check_doctor_schedule(db, doc, day, session)
                rules.check_doctor_leave(db, doc, day, session)
            except Refusal as err:
                if err.message not in doc_reasons:
                    doc_reasons.append(err.message)
        if doc_reasons:
            reasons.append(" ".join(doc_reasons))
        else:
            reasons.append(
                f"{doc.name}: every free start time on {day.isoformat()} is "
                "already taken."
            )
    return " ".join(reasons) or f"No free slots on {day.isoformat()}."


# --- the two tools ---------------------------------------------------------

@mcp.tool()
def find_slots(
    speciality: str = "",
    date: str = "",
    date_end: str = "",
    doctor_id: str = "",
) -> dict[str, Any]:
    """Find free new-consultation times at NetraCare eye clinic.

    Returns up to 30 free 20-minute start times, earliest first, as
    objects with date (YYYY-MM-DD), start_time (HH:MM), doctor_id and
    doctor_name. Start times sit on 10-minute marks (09:00, 09:10, ...).
    Only future times inside the clinic's 30-day booking window come
    back; Sundays and clinic holidays have no slots. When a day has
    nothing free the result is still "ok" with an empty list and a
    message that says why.

    Args:
        speciality: whose diary to search — one of: Cataract, Glaucoma,
            Retina, Cornea, Paediatric, Refractive, Dry eye, Squint,
            General. May be empty when doctor_id is given.
        date: the first day to search, as YYYY-MM-DD.
        date_end: optional last day of the search, as YYYY-MM-DD.
            Empty means just `date`. Nothing beyond the 30-day window
            is ever returned.
        doctor_id: optional doctor to restrict the search to, e.g. "D3"
            (the id book_appointment also wants). Empty = every doctor
            covering the speciality.
    """
    if not speciality and not doctor_id:
        return _refused(
            "MISSING_FILTER",
            "Give a speciality or a doctor_id so I know whose diary to look at.",
        )
    try:
        first = rules.parse_date(date)
        last = rules.parse_date(date_end) if date_end.strip() else first
    except Refusal as err:
        return _refused(err.code, err.message)
    if last < first:
        first, last = last, first

    with _db() as db:
        doctors = _doctors_for(db, doctor_id=doctor_id, speciality=speciality)
        if doctor_id and not doctors:
            return _refused("UNKNOWN_DOCTOR", "There is no doctor with that id.")
        if not doctors:
            return {
                "status": "ok",
                "slots": [],
                "count": 0,
                "message": f"No doctor covers {speciality} here.",
            }

        horizon = config.now().date() + timedelta(days=config.BOOKING_WINDOW_DAYS)
        found: list[dict] = []
        day = first
        while day <= last and day <= horizon and len(found) < MAX_SLOTS:
            for doc in doctors:
                if len(found) >= MAX_SLOTS:
                    break
                try:
                    free = slots.free_slots(
                        db, doctor_id=doc.doctor_id, date=day.isoformat(),
                        visit_type="new",
                    )
                except Refusal:
                    continue  # Sunday, holiday — not a day this doctor works
                for start in free:
                    found.append({
                        "date": day.isoformat(),
                        "start_time": start,
                        "doctor_id": doc.doctor_id,
                        "doctor_name": doc.name,
                    })
                    if len(found) >= MAX_SLOTS:
                        break
            day += timedelta(days=1)

        if not found:
            return {
                "status": "ok",
                "slots": [],
                "count": 0,
                "message": f"Nothing free: {_explain_empty(db, doctors, first)}",
            }
        return {
            "status": "ok",
            "slots": found,
            "count": len(found),
            "message": f"{len(found)} free new-consultation slot(s), earliest first.",
        }


@mcp.tool()
def book_appointment(
    patient_name: str,
    mobile: str,
    speciality: str,
    doctor_id: str,
    date: str,
    start_time: str,
    reason: str,
) -> dict[str, Any]:
    """Book a NEW consultation at NetraCare eye clinic for one patient.

    The booking runs NetraCare's own code: first the red-flag check on
    the reason, then every clinic rule. Nothing is written unless the
    result is status "booked".

    Args:
        patient_name: the patient's full name, exactly as registered
            with the clinic (checked against the mobile number).
        mobile: their 10-digit mobile number. A new number creates a
            new patient; a known number with a different name is
            refused with PATIENT_DETAILS_MISMATCH, so a booking can
            never land in someone else's record.
        speciality: one of: Cataract, Glaucoma, Retina, Cornea,
            Paediatric, Refractive, Dry eye, Squint, General. Must be
            a speciality the doctor covers.
        doctor_id: the doctor to book, e.g. "D1" (get ids from
            find_slots). New consultations only.
        date: YYYY-MM-DD, a future day within the next 30 days, not a
            Sunday or a clinic holiday.
        start_time: HH:MM on a 10-minute mark (09:00, 09:10, ...) inside
            a clinic session: AM 09:00-13:00 or PM 14:00-18:00. The
            consultation lasts 20 minutes and must finish within the
            session.
        reason: required, up to 300 characters: a short reason for the
            visit. A reason the red-flag check flags returns status
            "red_flag", books nothing, and carries the emergency
            response.

    Results:
        status "booked"   -> the appointment details (id, date, time,
                             doctor, patient) and booked_via "MCP".
        status "refused"  -> code and message, e.g. DOUBLE_BOOKED or
                             BEYOND_30_DAYS (the same codes and plain
                             messages the website returns).
        status "red_flag" -> code RED_FLAG, no booking, and
                             emergency_message: show it to the patient
                             exactly as it is.
    """
    with _db() as db:
        # Identity first, exactly as at website login (one shared helper).
        try:
            patient = resolve_patient(
                db,
                mobile=mobile,
                name=patient_name,
                mismatch_code="PATIENT_DETAILS_MISMATCH",
                mismatch_message=(
                    "That mobile number is registered to a different name. "
                    "No booking was made."
                ),
            )
        except DetailsMismatch as err:
            return _refused(err.code, err.message)

        # Red-flag check first, then every clinic rule — the website's
        # own booking path (app.actions.book), never a second copy.
        try:
            outcome = actions.book(
                db,
                patient_id=patient.patient_id,
                doctor_id=doctor_id,
                speciality=speciality,
                visit_type="new",
                date=date,
                start_time=start_time,
                reason=reason,
                trace_role="partner",
                via="MCP",
                urgent_source="mcp",
                block_on_red_flag=True,
            )
        except Refusal as err:
            result = _refused(err.code, err.message)
            if err.emergency_message:
                # A red flag was seen before a rule fired: the emergency
                # response still rides along, word for word.
                result["emergency_message"] = err.emergency_message
            return result

        if outcome.result == "red_flag":
            return {
                "status": "red_flag",
                "code": "RED_FLAG",
                "reason_code": "RED_FLAG",
                "emergency_message": config.EMERGENCY_MESSAGE,
                "show_to_patient": config.EMERGENCY_MESSAGE,
                "note": (
                    "No booking was made and an urgent case was saved for the "
                    "clinic. Show emergency_message to the patient exactly as "
                    "it is."
                ),
            }

        appt = outcome.appointment
        assert appt is not None  # booked outcome always carries one
        doc = db.get(Doctor, appt.doctor_id)
        doc_name = doc.name if doc else appt.doctor_id
        return {
            "status": "booked",
            "message": (
                f"Booked appointment #{appt.id}: {appt.date} "
                f"{appt.start_time}-{appt.end_time} with {doc_name} "
                f"({appt.speciality}, new consultation) for {patient.name}. "
                "The patient sees it in their NetraCare appointments and can "
                "cancel it there as usual."
            ),
            "appointment": {
                "id": appt.id,
                "patient_id": appt.patient_id,
                "patient_name": patient.name,
                "doctor_id": appt.doctor_id,
                "doctor_name": doc_name,
                "speciality": appt.speciality,
                "visit_type": appt.visit_type,
                "date": appt.date,
                "start_time": appt.start_time,
                "end_time": appt.end_time,
                "status": appt.status,
                "booked_via": appt.booked_via,
            },
        }


def main() -> None:
    """Create tables, load the data pack (idempotent), then serve."""
    init_db()
    with Session(engine) as db:
        seed.load_data(db)
    print(
        f"[mcp] NetraCare MCP server on http://localhost:{config.MCP_PORT}/mcp "
        f"(model {config.LLM_MODEL})",
        flush=True,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
