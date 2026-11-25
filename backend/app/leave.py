"""R7 — doctor leave, and the fixed rebooking steps that follow it.

Two jobs, both plain code (the handout is explicit that this is *not* an
agent):

* `mark_leave` — a doctor marks a date and AM / PM / full day within the
  next 30 days. The rules engine then refuses bookings in that session
  (`DOCTOR_ON_LEAVE`) and `slots.free_slots` stops offering it, so the
  website, the API and the chat agent all stop offering it together.
* the rebooking walk — the affected appointments are worked through in
  order of their original start time. Each one is cancelled and given a
  proposal that *holds* its slot as an appointment with status `proposed`:
      - a new consultation goes to the earliest free slot at or after the
        original start, with any doctor who covers the speciality;
      - a follow-up stays with the same doctor, at their next free slot;
      - a tie goes to the original doctor, then to the lower doctor id.
  Each proposal holds its slot before the next one is worked out, so two
  patients can never be proposed the same time. Nothing free inside the
  30-day window becomes a `no_slot` row: "No slot: call the patient".

Staff then Approve (the proposal becomes `booked`) or Reject (the slot is
freed) — see `routers/leave.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, select

from . import config, rules, slots
from .models import Appointment, Doctor, DoctorLeave, Rebooking
from .rules import Refusal

SESSIONS = ("AM", "PM", "FULL")


def _now() -> str:
    return config.now().isoformat(timespec="seconds")


def sessions_of(mark: str) -> list[str]:
    """Which clinic sessions does this leave mark cover?"""
    return ["AM", "PM"] if mark == "FULL" else [mark]


def _covers(mark: str, session: str) -> bool:
    return mark == "FULL" or mark == session


# --- marking leave ----------------------------------------------------------

def mark_leave(
    db: Session, *, doctor_id: str, date: str, session: str
) -> tuple[DoctorLeave, list[Rebooking]]:
    """Mark leave and rebook everything it affects. Returns (leave, rows)."""
    doctor = rules.get_doctor(db, doctor_id)
    day = rules.parse_date(date)
    if session not in SESSIONS:
        raise Refusal("INVALID_SESSION", "Leave must be AM, PM or FULL.")

    now = config.now().date()
    if day < now:
        raise Refusal("IN_PAST", "Leave can only be marked for a future date.")
    if day > now + timedelta(days=config.BOOKING_WINDOW_DAYS):
        raise Refusal(
            "BEYOND_30_DAYS",
            f"Leave can only be marked up to {config.BOOKING_WINDOW_DAYS} days ahead.",
        )
    rules.check_clinic_open(db, day)

    # Marking the same session twice must not re-cancel anything.
    existing = db.exec(
        select(DoctorLeave).where(
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.date == day.isoformat(),
            DoctorLeave.session == session,
        )
    ).first()
    if existing is not None:
        return existing, []

    leave = DoctorLeave(
        doctor_id=doctor_id,
        date=day.isoformat(),
        session=session,
        created_at=_now(),
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)

    return leave, _rebook(db, leave=leave, doctor=doctor)


def _affected(db: Session, leave: DoctorLeave) -> list[Appointment]:
    """The leaving doctor's booked appointments in the leave session, in time order."""
    rows = db.exec(
        select(Appointment).where(
            Appointment.doctor_id == leave.doctor_id,
            Appointment.date == leave.date,
            Appointment.status == "booked",
        ).order_by(Appointment.start_time, Appointment.id)
    ).all()
    out = []
    for appt in rows:
        found = rules.session_for(rules.parse_time(appt.start_time))
        if found is None:
            continue
        if _covers(leave.session, found[0]):
            out.append(appt)
    return out


def _rebook(db: Session, *, leave: DoctorLeave, doctor: Doctor) -> list[Rebooking]:
    """Cancel each affected appointment and propose its replacement."""
    rows: list[Rebooking] = []
    note = f"Moved because {doctor.name} is on leave"
    for appt in _affected(db, leave):
        # The clinic cancels this one: no 2-hour cut-off, no approval for
        # the cancel itself. Staff approve the *rebooking*.
        appt.status = "cancelled"
        appt.move_note = note
        db.add(appt)
        db.commit()

        proposal = _propose(db, appt, leave=leave, doctor=doctor)
        if proposal is None:
            row = Rebooking(
                leave_id=leave.id,
                original_appointment_id=appt.id,
                proposed_appointment_id=None,
                status="no_slot",
                created_at=_now(),
            )
        else:
            db.add(proposal)
            db.commit()
            db.refresh(proposal)
            row = Rebooking(
                leave_id=leave.id,
                original_appointment_id=appt.id,
                proposed_appointment_id=proposal.id,
                status="proposed",
                created_at=_now(),
            )
        db.add(row)
        db.commit()
        db.refresh(row)
        rows.append(row)
    return rows


# --- finding the replacement slot ------------------------------------------

def _candidate_doctors(db: Session, appt: Appointment) -> list[str]:
    """Who may take this appointment, in tie-break order.

    A follow-up stays with the doctor who saw the patient; a new
    consultation may go to any doctor who covers the speciality, with the
    original doctor winning a tie and the lower doctor id breaking the rest.
    """
    if appt.visit_type == "follow_up":
        return [appt.doctor_id]
    covering = [
        d.doctor_id
        for d in db.exec(select(Doctor).order_by(Doctor.doctor_id)).all()
        if appt.speciality in rules.specialities_of(d)
    ]
    return sorted(covering, key=lambda i: (i != appt.doctor_id, i))


def _propose(
    db: Session, appt: Appointment, *, leave: DoctorLeave, doctor: Doctor
) -> Appointment | None:
    """The replacement appointment, or None when nothing is free in the window."""
    original = datetime.combine(
        rules.parse_date(appt.date), rules.parse_time(appt.start_time)
    )
    candidates = _candidate_doctors(db, appt)
    horizon = config.now().date() + timedelta(days=config.BOOKING_WINDOW_DAYS)

    day = original.date()
    best: tuple[datetime, int, str] | None = None  # (when, tie index, doctor)
    while day <= horizon and best is None:
        for index, doctor_id in enumerate(candidates):
            try:
                free = slots.free_slots(
                    db, doctor_id=doctor_id, date=day.isoformat(),
                    visit_type=appt.visit_type,
                )
            except Refusal:
                continue  # Sunday, holiday — not a day this doctor works
            for start in free:
                when = datetime.combine(day, rules.parse_time(start))
                if when < original:
                    continue  # "at or after the original start time"
                key = (when, index)
                if best is None or key < (best[0], best[1]):
                    best = (when, index, doctor_id)
        if best is None:
            day += timedelta(days=1)

    if best is None:
        return None

    when, _, doctor_id = best
    duration = config.DURATION_MINUTES[appt.visit_type]
    end = when + timedelta(minutes=duration)
    return Appointment(
        patient_id=appt.patient_id,
        doctor_id=doctor_id,
        speciality=appt.speciality,
        visit_type=appt.visit_type,
        date=when.date().isoformat(),
        start_time=rules.fmt_time(when.time()),
        end_time=rules.fmt_time(end.time()),
        status="proposed",
        reason=appt.reason,
        urgent=bool(appt.urgent),
        booked_via=appt.booked_via or "Website",
        intake_notes=appt.intake_notes,
        moved_from_id=appt.id,
        move_note=f"Moved because {doctor.name} is on leave",
    )


# --- Staff decide -----------------------------------------------------------

def _rebooking(db: Session, rebooking_id: int) -> Rebooking:
    row = db.get(Rebooking, rebooking_id)
    if row is None:
        raise Refusal("NOT_FOUND", "That rebooking does not exist.", status=404)
    return row


def approve(db: Session, rebooking_id: int) -> Appointment:
    """Staff Approve: the proposal becomes a real, booked appointment."""
    row = _rebooking(db, rebooking_id)
    if row.status == "no_slot":
        raise Refusal(
            "NO_PROPOSAL",
            "There is no new time to approve — please call the patient.",
            status=409,
        )
    if row.status != "proposed":
        raise Refusal("ALREADY_DECIDED", f"This rebooking was already {row.status}.", status=409)
    proposal = db.get(Appointment, row.proposed_appointment_id or 0)
    if proposal is None:
        raise Refusal("NOT_FOUND", "That proposal no longer exists.", status=404)

    # The rules decide again, against the database as it is *now*: the
    # proposal holds its own slot, so it validates with exclude_id=itself.
    rules.validate_booking(
        db,
        patient_id=proposal.patient_id,
        doctor_id=proposal.doctor_id,
        speciality=proposal.speciality,
        visit_type=proposal.visit_type,
        date=proposal.date,
        start_time=proposal.start_time,
        exclude_id=proposal.id,
    )

    proposal.status = "booked"
    row.status = "approved"
    row.decided_at = _now()
    db.add(proposal)
    db.add(row)
    db.commit()
    db.refresh(proposal)
    db.refresh(row)
    return proposal


def reject(db: Session, rebooking_id: int) -> Appointment | None:
    """Staff Reject: the proposal is dropped and its slot is freed.

    A `no_slot` row has nothing to free — Reject just takes it off the list
    (the appointment was already cancelled when leave was marked).
    """
    row = _rebooking(db, rebooking_id)
    if row.status == "no_slot":
        row.status = "rejected"
        row.decided_at = _now()
        db.add(row)
        db.commit()
        db.refresh(row)
        return None
    if row.status != "proposed":
        raise Refusal("ALREADY_DECIDED", f"This rebooking was already {row.status}.", status=409)
    proposal = db.get(Appointment, row.proposed_appointment_id or 0)
    if proposal is None:
        raise Refusal("NOT_FOUND", "That proposal no longer exists.", status=404)

    proposal.status = "cancelled"
    row.status = "rejected"
    row.decided_at = _now()
    db.add(proposal)
    db.add(row)
    db.commit()
    db.refresh(proposal)
    db.refresh(row)
    return proposal
