"""R2 — the agent's tools (handout R2: "Tools over your API").

Every tool wraps the same functions the HTTP endpoints call. A tool never
re-checks a rule itself and never writes to the database: the three write
tools go straight through `app.actions`, which is what the routes use, so a
refusal is an ordinary result and the website and the chat can never
disagree about what is allowed.

The patient is never a tool argument. `build_tools()` builds the seven tools
without one: each tool reads the patient's id from the graph's *config*
(`configurable.patient_id`), which your code sets from the login token, so
the model has no way to ask for somebody else's appointments — handout R2:
"Pass the patient's ID to the tools through the graph's config, not as a
tool argument".

Before a book, cancel or reschedule reaches `app.actions`, the tool calls
LangGraph's `interrupt()` with a one-line summary (handout R5). Nothing is
written until the patient presses Confirm on the card; a resume value of
False returns a `declined` result and changes nothing.

Result shapes are short dicts. A refusal looks like
    {"result": "refused", "reason_code": "DOCTOR_OFF", "message": "..."}
which is a normal tool result, not an error.
"""
from __future__ import annotations

from datetime import date as date_cls, timedelta
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt
from sqlmodel import Session, select

from . import config, lab, rag, rules, skills, slots
from .actions import Outcome, book as act_book, cancel as act_cancel, reschedule as act_reschedule
from .database import engine
from .models import ApprovalRequest, Appointment, Doctor, WeeklySchedule
from .rules import Refusal


def _patient_id() -> str:
    """The logged-in patient, from the graph config — never from the LLM."""
    from langchain_core.runnables.config import ensure_config

    cfg = ensure_config()
    pid = (cfg.get("configurable") or {}).get("patient_id")
    if not pid:
        raise ValueError("patient_id is missing from the graph config")
    return str(pid)

# A refusal the model should explain, never an exception that kills the turn.
def _refused(err: Refusal) -> dict[str, Any]:
    return {"result": "refused", "reason_code": err.code, "message": err.message}


def _db():
    """A session per tool call: tools run outside the request scope."""
    return Session(engine)


# --- read tools ------------------------------------------------------------

def _doctor_rows(db: Session) -> list[dict]:
    """Every doctor with their OPD days, as the model needs to see them."""
    schedule = db.exec(select(WeeklySchedule)).all()
    out = []
    for doc in db.exec(select(Doctor).order_by(Doctor.doctor_id)).all():
        specs = [s.strip() for s in doc.specialities.split(";") if s.strip()]
        days: list[dict] = []
        for day in rules.DAY_NAMES:
            sessions = [
                {"session": s.session, "status": s.status}
                for s in schedule
                if s.doctor_id == doc.doctor_id and s.day == day
            ]
            opd = [s for s in sessions if s["status"] == "OPD"]
            if opd:
                days.append({"day": day, "sessions": [s["session"] for s in opd]})
        out.append({
            "doctor_id": doc.doctor_id,
            "name": doc.name,
            "specialities": specs,
            "opd_days": days,
        })
    return out


def _doctors_for(db: Session, *, doctor_id: str | None, speciality: str | None) -> list[Doctor]:
    """The doctors a find_slots call may offer (one filter, or all of them)."""
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


def _upcoming(db: Session, patient_id: str) -> list[Appointment]:
    today = config.now().date().isoformat()
    return list(db.exec(
        select(Appointment).where(
            Appointment.patient_id == patient_id,
            # "proposed" too: a rebooking after doctor leave is on its way.
            Appointment.status.in_(("booked", "proposed")),
            Appointment.date >= today,
        ).order_by(Appointment.date, Appointment.start_time)
    ).all())


def _approval_for(db: Session, appointment_id: int) -> ApprovalRequest | None:
    return db.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.appointment_id == appointment_id,
            ApprovalRequest.status == "pending",
        )
    ).first()


def _appt_row(db: Session, appt: Appointment, doctors: dict[str, str]) -> dict:
    approval = _approval_for(db, appt.id)
    return {
        "appointment_id": appt.id,
        "date": appt.date,
        "start_time": appt.start_time,
        "end_time": appt.end_time,
        "doctor_id": appt.doctor_id,
        "doctor_name": doctors.get(appt.doctor_id, appt.doctor_id),
        "speciality": appt.speciality,
        "visit_type": appt.visit_type,
        "status": appt.status,
        "booked_via": appt.booked_via or "Website",
        "waiting_for_staff": approval.action if approval else None,
    }


def _outcome_result(outcome: Outcome, *, db: Session, doctors: dict[str, str]) -> dict:
    """Turn an actions.Outcome into the short dict the model reads."""
    appt = outcome.appointment
    result: dict[str, Any] = {"result": outcome.result}
    if outcome.message:
        result["message"] = outcome.message
    if appt is not None:
        result["appointment"] = _appt_row(db, appt, doctors)
    if outcome.approval is not None:
        result["approval_id"] = outcome.approval.id
        result["approval_status"] = outcome.approval.status
    if outcome.emergency_message:
        # A red flag was seen before the rule fired: hand it to the model
        # verbatim, and the red-flag node owns the wording.
        result["emergency_message"] = outcome.emergency_message
    if outcome.extra.get("old_appointment_id"):
        result["old_appointment_id"] = outcome.extra["old_appointment_id"]
    return result


def _names(db: Session) -> dict[str, str]:
    return {d.doctor_id: d.name for d in db.exec(select(Doctor)).all()}


# --- the factory -----------------------------------------------------------

def _when(date: str, start: str) -> str:
    """"Fri 13 Nov, 9:00 AM" from a "2026-11-13" / "09:00" pair."""
    try:
        day = date_cls.fromisoformat(date)
        hour, minute = start.split(":")
    except (ValueError, AttributeError):
        return f"{date} {start}"
    h = int(hour)
    suffix = "AM" if h < 12 else "PM"
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}, {h % 12 or 12}:{minute} {suffix}"


def _confirm(summary: str, kind: str) -> bool:
    """Pause for the patient's Confirm card (handout R5).

    Inside the graph this raises LangGraph's interrupt: the route returns
    the summary as `pending_action` and nothing is written until the
    patient presses Confirm. Called outside the graph — a Python shell —
    there is no card to wait for, so the call simply continues: the
    website's buttons and the HTTP API also act without an interrupt, and
    every clinic rule is still applied by `app.actions` either way.
    """
    from langchain_core.runnables.config import ensure_config

    conf = ensure_config().get("configurable") or {}
    if "__pregel_scratchpad" not in conf:
        return True  # not running inside a LangGraph graph
    return bool(interrupt({"kind": kind, "summary": summary}))


def build_tools() -> list[BaseTool]:
    """The clinic tools plus the two skill tools and the partner lab tool.

    The patient id comes from the graph's config. Skill and lab tools are
    read-only: they never book, never diagnose and never leave the clinic's
    safety rules (which always run first, in the red-flag node).
    """

    @tool
    def search_clinic_docs(question: str) -> dict:
        """Answer a question about the clinic (fees, hours, parking, rules) from the clinic documents. Use for anything about policy or procedure; returns the answer with its sources, or 'I don't know'."""
        try:
            chunks = rag.retrieve(question)
            answer = rag.answer_question(question, chunks)
        except rag.LlmUnavailable:
            return {"result": "unavailable", "message": config.CHAT_UNAVAILABLE}
        sources = rag.build_sources(answer, chunks)
        if sources is None:
            # The fixed message, passed on word for word (handout R6).
            return {"result": "idk", "message": config.IDK_MESSAGE}
        return {"result": "answered", "answer": answer.answer.strip(), "sources": sources}

    @tool
    def list_doctors() -> dict:
        """List the clinic's doctors with their IDs, the specialities they cover and the weekdays they see patients (OPD). Use before offering a doctor."""
        with _db() as db:
            return {"result": "ok", "doctors": _doctor_rows(db)}

    @tool
    def find_slots(
        visit_type: Literal["new", "follow_up"],
        start_date: str,
        end_date: str,
        doctor_id: str | None = None,
        speciality: str | None = None,
        session: Literal["AM", "PM"] | None = None,
    ) -> dict:
        """Find free appointment times. Give a visit type ('new' for a first visit, 'follow_up' for a review), a date range as YYYY-MM-DD, and either a doctor_id or a speciality, and optionally 'AM' or 'PM'. Returns at most 10 free start times, earliest first. Call this before offering any time."""
        if not doctor_id and not speciality:
            return {
                "result": "refused",
                "reason_code": "MISSING_FILTER",
                "message": "Give a doctor_id or a speciality so I know whose diary to look at.",
            }
        with _db() as db:
            doctors = _doctors_for(db, doctor_id=doctor_id, speciality=speciality)
            if doctor_id and not doctors:
                return _refused(Refusal("UNKNOWN_DOCTOR", "There is no doctor with that id."))
            if not doctors:
                return {
                    "result": "none",
                    "message": f"No doctor covers {speciality} here.",
                    "slots": [],
                }

            try:
                first = rules.parse_date(start_date)
                last = rules.parse_date(end_date)
            except Refusal as err:
                return _refused(err)
            if last < first:
                first, last = last, first

            found: list[dict] = []
            day = first
            # Never walk past the booking window: beyond it free_slots is empty
            # anyway, and this keeps a silly range from looping for ever.
            horizon = config.now().date() + timedelta(days=config.BOOKING_WINDOW_DAYS)
            while day <= last and day <= horizon and len(found) < 10:
                for doc in doctors:
                    if len(found) >= 10:
                        break
                    try:
                        free = slots.free_slots(
                            db, doctor_id=doc.doctor_id, date=day.isoformat(),
                            visit_type=visit_type,
                        )
                    except Refusal:
                        continue  # Sunday, holiday — just not a day this doctor works
                    for start in free:
                        if session:
                            got = rules.session_for(rules.parse_time(start))
                            if got is None or got[0] != session:
                                continue
                        found.append({
                            "date": day.isoformat(),
                            "start_time": start,
                            "doctor_id": doc.doctor_id,
                            "doctor_name": doc.name,
                        })
                        if len(found) >= 10:
                            break
                day += timedelta(days=1)

            found.sort(key=lambda s: (s["date"], s["start_time"], s["doctor_id"]))
            found = found[:10]
            if not found:
                return {
                    "result": "none",
                    "slots": [],
                    "message": "Nothing free in those dates.",
                }
            return {"result": "ok", "slots": found, "count": len(found)}

    @tool
    def my_appointments() -> dict:
        """List this patient's upcoming appointments with their IDs, and any cancel or reschedule that is still waiting for Staff to approve."""
        patient_id = _patient_id()
        with _db() as db:
            doctors = _names(db)
            rows = [_appt_row(db, a, doctors) for a in _upcoming(db, patient_id)]
        waiting = [r for r in rows if r["waiting_for_staff"]]
        if not rows:
            return {
                "result": "none",
                "appointments": [],
                "message": "You have no upcoming appointments.",
            }
        return {
            "result": "ok",
            "appointments": rows,
            "waiting_for_staff": waiting,
            "count": len(rows),
        }

    # --- write tools -------------------------------------------------------
    # Each of these goes through app.actions, exactly like the HTTP route,
    # so the API decides whether the change is allowed (handout R4/R6).
    # Before the action runs, the tool interrupts with a one-line summary
    # and waits for the patient's Confirm on the card (handout R5).

    @tool
    def book_appointment(
        doctor_id: str,
        speciality: str,
        visit_type: Literal["new", "follow_up"],
        date: str,
        start_time: str,
        reason: str,
        intake_notes: str | None = None,
    ) -> dict:
        """Book a clinic appointment. Needs a doctor_id, the speciality, a visit type ('new' or 'follow_up'), a date as YYYY-MM-DD, a start time as HH:MM and a short reason for the visit. Optional intake_notes carries pre-assessment answers (e.g. from the LASIK suitability skill) for the doctor. The clinic must accept the booking; a refusal is returned with its reason code."""
        patient_id = _patient_id()
        with _db() as db:
            doc = db.get(Doctor, doctor_id)
            label = "follow-up" if visit_type == "follow_up" else "consultation"
            minutes = config.DURATION_MINUTES.get(visit_type, 10)
            summary = (
                f"Book {doc.name if doc else doctor_id}, {speciality} {label}, "
                f"{_when(date, start_time)} ({minutes} min)?"
            )
            if not _confirm(summary, "book"):
                return {
                    "result": "declined",
                    "message": "The patient pressed No. Nothing was booked.",
                }
            try:
                outcome = act_book(
                    db,
                    patient_id=patient_id,
                    doctor_id=doctor_id,
                    speciality=speciality,
                    visit_type=visit_type,
                    date=date,
                    start_time=start_time,
                    reason=reason,
                    via="Chat",
                    intake_notes=(intake_notes or "").strip() or None,
                )
            except Refusal as err:
                return _refused(err)
            return _outcome_result(outcome, db=db, doctors=_names(db))

    @tool
    def cancel_appointment(appointment_id: int) -> dict:
        """Cancel one of this patient's appointments by its appointment_id. Ask the patient to confirm first. A cancel that starts inside 2 hours is not applied straight away: it is sent to Staff."""
        patient_id = _patient_id()
        with _db() as db:
            appt = db.get(Appointment, appointment_id)
            # Confirm only when the appointment is really this patient's and
            # active: another patient's id must never appear on a card, and
            # an impossible cancel is refused without asking (R2 check).
            if appt is not None and appt.patient_id == patient_id and appt.status == "booked":
                doc = db.get(Doctor, appt.doctor_id)
                summary = (
                    f"Cancel appointment #{appointment_id} with "
                    f"{doc.name if doc else appt.doctor_id} on "
                    f"{_when(appt.date, appt.start_time)}?"
                )
                if not _confirm(summary, "cancel"):
                    return {
                        "result": "declined",
                        "message": "The patient pressed No. Nothing was cancelled.",
                    }
            try:
                outcome = act_cancel(db, appointment_id=appointment_id, acting_patient_id=patient_id)
            except Refusal as err:
                return _refused(err)
            return _outcome_result(outcome, db=db, doctors=_names(db))

    @tool
    def reschedule_appointment(
        appointment_id: int,
        date: str,
        start_time: str,
        doctor_id: str | None = None,
    ) -> dict:
        """Move one of this patient's appointments to a new date and start time, optionally with a different doctor. Give the appointment_id to move, the new date (YYYY-MM-DD) and start time (HH:MM). Ask the patient to confirm first. A move that starts inside 2 hours is sent to Staff instead of being applied."""
        patient_id = _patient_id()
        with _db() as db:
            appt = db.get(Appointment, appointment_id)
            if appt is not None and appt.patient_id == patient_id and appt.status == "booked":
                new_doc_id = doctor_id or appt.doctor_id
                doc = db.get(Doctor, new_doc_id)
                summary = (
                    f"Move appointment #{appointment_id} to "
                    f"{_when(date, start_time)} with "
                    f"{doc.name if doc else new_doc_id}?"
                )
                if not _confirm(summary, "reschedule"):
                    return {
                        "result": "declined",
                        "message": "The patient pressed No. Nothing was moved.",
                    }
            try:
                outcome = act_reschedule(
                    db,
                    appointment_id=appointment_id,
                    date=date,
                    start_time=start_time,
                    doctor_id=doctor_id,
                    acting_patient_id=patient_id,
                )
            except Refusal as err:
                return _refused(err)
            return _outcome_result(outcome, db=db, doctors=_names(db))

    # --- skills (iteration 6, R4) ------------------------------------------
    # Read-only. Unknown skills and any path outside the skill's folder are
    # refused by app.skills; the red-flag check still runs on every message
    # before these tools can be reached.

    @tool
    def load_skill(name: str) -> dict:
        """Load a clinic skill by name when a task needs its know-how (see Available skills in your instructions). Returns the skill body, or a refusal for an unknown name. Skills never replace the clinic's safety rules."""
        body, why = skills.load(name)
        if body is None:
            return {"result": "refused", "message": why}
        return {"result": "ok", "name": (name or "").strip(), "body": body}

    @tool
    def read_skill_file(name: str, file: str) -> dict:
        """Read one extra file from an already-loaded skill's folder (for example 'post-op-guide.md' or 'questions.md'). Only files inside that skill's folder are allowed; anything else is refused."""
        text, why = skills.read_file(name, file)
        if text is None:
            return {"result": "refused", "message": why}
        return {
            "result": "ok",
            "name": (name or "").strip(),
            "file": (file or "").strip(),
            "body": text,
        }

    # --- partner lab (iteration 6, R3) --------------------------------------
    # CityCare Lab's own MCP tool, loaded at startup. When the partner
    # server is down the answer is the fixed LAB_UNAVAILABLE message —
    # the chat never breaks because a partner is offline.
    #
    # Sync on purpose: the chat runs the graph with `graph.invoke()`, and
    # ToolNode's sync path can only call a tool that has a sync body. The
    # work inside is still async — `lab.query` runs `lab.aquery` on its own
    # short-lived event loop, which the MCP tools accept (they open their
    # session per call).

    @tool
    def get_lab_test_info(test: str = "") -> dict:
        """Information about a blood or urine test at CityCare Hospital Lab, NetraCare's partner lab. Returns fasting rules, sample type, when the report is ready and the indicative price, plus the lab's address, timings and home collection. Information only: it cannot book tests, read results or say which tests a patient needs. Args: test — the test name in plain words, for example 'HbA1c' or 'fasting sugar'. Leave empty for the lab's details and the list of tests it offers."""
        return lab.query(test)

    return [
        search_clinic_docs,
        list_doctors,
        find_slots,
        my_appointments,
        book_appointment,
        cancel_appointment,
        reschedule_appointment,
        load_skill,
        read_skill_file,
        get_lab_test_info,
    ]
