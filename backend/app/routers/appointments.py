"""Slots, bookings, cancellations, reschedules and completions.

Every route here funnels through the rules engine. Refusals come back as
`{"detail": {"code": ..., "message": ...}}` so the screen can show the reason
in plain words — and so an AI agent calling the API can read the code.

Iteration 2: booking runs the LLM red-flag check on `reason` BEFORE the
rules engine; completing a visit requires a doctor note.

Iteration 4 (R1): reschedule, and the Staff approval queue for a patient's
cancel or reschedule inside 2 hours. Both go through `app.actions`, the
same functions the agent's tools call, so the website and the chat can
never disagree about what is allowed.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from .. import actions, config
from ..database import get_db
from ..models import ApprovalRequest, Appointment, Doctor, Patient
from ..rules import Refusal
from ..schemas import (
    AppointmentIn,
    AppointmentOut,
    ApprovalOut,
    CompleteIn,
    RescheduleIn,
    SlotOut,
)
from ..security import Identity, current_identity, current_staff
from ..slots import free_slots

router = APIRouter(prefix="/api", tags=["appointments"])


def _refusal(err: Refusal, emergency_message: str | None = None) -> HTTPException:
    """A rule refusal — plus the emergency response when a red flag was seen.

    Handout R4: the emergency response always comes first, even when the
    booking itself is refused. The urgent case has already been saved, so
    the message rides along in the error detail for the screen to show.
    """
    detail: dict = {"code": err.code, "message": err.message}
    emergency = emergency_message or err.emergency_message
    if emergency:
        detail["emergency_message"] = emergency
    return HTTPException(status_code=err.status, detail=detail)


def _names(db: Session) -> tuple[dict[str, str], dict[str, str]]:
    """Doctor and patient id -> name, for display."""
    doctors = {d.doctor_id: d.name for d in db.exec(select(Doctor)).all()}
    patients = {p.patient_id: p.name for p in db.exec(select(Patient)).all()}
    return doctors, patients


def _out(
    appt: Appointment,
    doctors: dict[str, str],
    patients: dict[str, str],
    *,
    emergency_message: str | None = None,
    approval: ApprovalRequest | None = None,
    result: str | None = None,
) -> AppointmentOut:
    return AppointmentOut(
        id=appt.id,
        patient_id=appt.patient_id,
        patient_name=patients.get(appt.patient_id, appt.patient_id),
        doctor_id=appt.doctor_id,
        doctor_name=doctors.get(appt.doctor_id, appt.doctor_id),
        speciality=appt.speciality,
        visit_type=appt.visit_type,
        date=appt.date,
        start_time=appt.start_time,
        end_time=appt.end_time,
        status=appt.status,
        reason=appt.reason,
        doctor_note=appt.doctor_note,
        urgent=bool(appt.urgent),
        emergency_message=emergency_message,
        # Iteration 4: what the write action did, and the Staff request
        # waiting on this appointment (if any).
        result=result,
        approval_id=approval.id if approval else None,
        approval_status=approval.status if approval else None,
        approval_action=approval.action if approval else None,
        # R7: why this row exists (a rebooking after doctor leave).
        moved_from_id=appt.moved_from_id,
        move_note=appt.move_note,
        # Iteration 6: where it was booked, and any LASIK intake answers.
        booked_via=appt.booked_via or "Website",
        intake_notes=appt.intake_notes,
    )


def _approvals_for(db: Session, appointment_ids: list[int]) -> dict[int, ApprovalRequest]:
    """The most recent approval request per appointment (for the patient's list)."""
    if not appointment_ids:
        return {}
    rows = db.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.appointment_id.in_(appointment_ids)
        ).order_by(ApprovalRequest.id)
    ).all()
    return {row.appointment_id: row for row in rows}


# --- R4: free slots --------------------------------------------------------

@router.get("/slots", response_model=SlotOut)
def slots(
    doctor_id: str = Query(...),
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    visit_type: str = Query("new"),
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> SlotOut:
    """Start times that can be booked for this doctor/date/visit type."""
    _ = identity
    if visit_type not in config.DURATION_MINUTES:
        raise _refusal(Refusal("INVALID_VISIT_TYPE", "Visit type must be 'new' or 'follow_up'."))
    try:
        result = free_slots(db, doctor_id=doctor_id, date=date, visit_type=visit_type)
        notes: list[str] = []
    except Refusal as err:
        # A closed clinic or unknown doctor: no slots, and say why.
        result = []
        notes = [err.message]
    return SlotOut(
        date=date,
        doctor_id=doctor_id,
        visit_type=visit_type,  # type: ignore[arg-type]
        duration_minutes=config.DURATION_MINUTES[visit_type],
        slots=result,
        notes=notes,
    )


# --- R5/R3: book (red-flag check first) ------------------------------------

@router.post("/appointments", response_model=AppointmentOut, status_code=201)
def book(
    body: AppointmentIn,
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> AppointmentOut:
    """Book an appointment: red-flag check, then the one rules check.

    The LLM never decides the booking — it only flags urgency. Every booking
    still goes through the iteration 1 rules engine.
    """
    if identity.role == "patient":
        # The patient is taken from the token, never from the request body —
        # this is what stops one patient booking (or peeking) at another's.
        if body.patient_id and body.patient_id != identity.patient_id:
            raise _refusal(Refusal("NOT_YOUR_APPOINTMENT", "You can only book appointments for yourself.", 403))
        patient_id = identity.patient_id
    elif identity.role == "staff":
        patient_id = _resolve_patient(db, body)
    else:
        raise _refusal(Refusal("WRONG_ROLE", "Doctors do not book appointments.", 403))

    try:
        outcome = actions.book(
            db,
            patient_id=patient_id,
            doctor_id=body.doctor_id,
            speciality=body.speciality,
            visit_type=body.visit_type,
            date=body.date,
            start_time=body.start_time,
            reason=body.reason,
            via="Staff" if identity.role == "staff" else "Website",
            intake_notes=(body.intake_notes or "").strip() or None,
        )
    except Refusal as err:
        raise _refusal(err)

    doctors, patients = _names(db)
    return _out(
        outcome.appointment, doctors, patients,
        emergency_message=outcome.emergency_message,
        result=outcome.result,
    )


def _resolve_patient(db: Session, body: AppointmentIn) -> str:
    """Staff: pick an existing patient, or create a walk-in by mobile."""
    if body.patient_id:
        if db.get(Patient, body.patient_id) is None:
            raise _refusal(Refusal("UNKNOWN_PATIENT", "That patient does not exist.", 404))
        return body.patient_id

    mobile = (body.mobile or "").strip()
    if not mobile:
        raise _refusal(Refusal("MISSING_PATIENT", "Choose a patient or enter a walk-in mobile number."))
    if not mobile.isdigit() or len(mobile) < 10:
        raise _refusal(Refusal("INVALID_MOBILE", "Please enter a valid 10-digit mobile number."))

    patient = db.exec(select(Patient).where(Patient.mobile == mobile)).first()
    if patient is None:
        name = (body.patient_name or "").strip()
        if not name:
            raise _refusal(Refusal("MISSING_NAME", "New mobile number — please enter the patient's name."))
        ids = [p.patient_id for p in db.exec(select(Patient)).all()]
        numbers = [int(i[1:]) for i in ids if i.startswith("P") and i[1:].isdigit()]
        patient = Patient(patient_id=f"P{(max(numbers) + 1) if numbers else 1:02d}", name=name, mobile=mobile)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    return patient.patient_id


# --- R5: cancel ------------------------------------------------------------

@router.post("/appointments/{appt_id}/cancel", response_model=AppointmentOut)
def cancel(
    appt_id: int,
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> AppointmentOut:
    """Patients cancel their own, until 2 hours before. Staff cancel anything.

    Inside the cut-off a patient's cancel is not refused: it is queued for
    Staff (`result="to_staff"`) and the appointment stays booked.
    """
    if identity.role == "doctor":
        raise _refusal(Refusal("WRONG_ROLE", "Doctors do not cancel appointments.", 403))

    acting_patient = identity.patient_id if identity.role == "patient" else None
    try:
        outcome = actions.cancel(db, appointment_id=appt_id, acting_patient_id=acting_patient)
    except Refusal as err:
        raise _refusal(err)

    doctors, patients = _names(db)
    return _out(
        outcome.appointment, doctors, patients,
        approval=outcome.approval,
        result=outcome.result,
    )


# --- R1: reschedule --------------------------------------------------------

@router.post("/appointments/{appt_id}/reschedule", response_model=AppointmentOut)
def reschedule(
    appt_id: int,
    body: RescheduleIn,
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> AppointmentOut:
    """Move an appointment to a new date/time (and optionally another doctor).

    One action: the new booking is validated as if the old one were already
    cancelled, then the old row is freed and the new one takes its place in
    the same transaction. A refusal leaves the original exactly as it was.
    Inside the 2-hour cut-off a patient's move is queued for Staff instead.
    """
    if identity.role == "doctor":
        raise _refusal(Refusal("WRONG_ROLE", "Doctors do not change appointments.", 403))

    acting_patient = identity.patient_id if identity.role == "patient" else None
    try:
        outcome = actions.reschedule(
            db,
            appointment_id=appt_id,
            date=body.date,
            start_time=body.start_time,
            doctor_id=body.doctor_id,
            acting_patient_id=acting_patient,
        )
    except Refusal as err:
        raise _refusal(err)

    doctors, patients = _names(db)
    return _out(
        outcome.appointment, doctors, patients,
        approval=outcome.approval,
        result=outcome.result,
    )


# --- R1: the Staff approval queue ------------------------------------------

@router.get("/staff/approvals", response_model=list[ApprovalOut])
def list_approvals(
    status: str | None = Query(None, pattern=r"^(pending|approved|rejected)$"),
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> list[ApprovalOut]:
    """Every approval request, newest first — Staff only.

    By default only the ones still waiting, which is what the Staff screen
    works through.
    """
    _ = identity
    query = select(ApprovalRequest)
    query = query.where(ApprovalRequest.status == (status or "pending"))
    rows = db.exec(query.order_by(ApprovalRequest.id.desc())).all()

    doctors, patients = _names(db)
    out: list[ApprovalOut] = []
    for row in rows:
        appt = db.get(Appointment, row.appointment_id)
        if appt is None:
            continue
        payload = json.loads(row.payload) if row.payload else None
        out.append(ApprovalOut(
            id=row.id,
            action=row.action,
            status=row.status,
            patient_id=row.patient_id,
            patient_name=patients.get(row.patient_id, row.patient_id),
            appointment_id=row.appointment_id,
            appointment=_out(appt, doctors, patients),
            payload=payload,
            created_at=row.created_at,
            decided_at=row.decided_at,
        ))
    return out


def _get_approval(db: Session, approval_id: int) -> ApprovalRequest:
    row = db.get(ApprovalRequest, approval_id)
    if row is None:
        raise _refusal(Refusal("NOT_FOUND", "That request does not exist.", 404))
    return row


@router.post("/staff/approvals/{approval_id}/approve", response_model=AppointmentOut)
def approve(
    approval_id: int,
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> AppointmentOut:
    """Approve a queued cancel or reschedule: the action runs as Staff."""
    _ = identity
    row = _get_approval(db, approval_id)
    try:
        outcome = actions.approve(db, row)
    except Refusal as err:
        raise _refusal(err)
    doctors, patients = _names(db)
    return _out(
        outcome.appointment, doctors, patients,
        approval=outcome.approval,
        result=outcome.result,
    )


@router.post("/staff/approvals/{approval_id}/reject", response_model=ApprovalOut)
def reject(
    approval_id: int,
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> ApprovalOut:
    """Reject a queued request: the appointment is left exactly as it was."""
    _ = identity
    row = _get_approval(db, approval_id)
    try:
        outcome = actions.reject(db, row)
    except Refusal as err:
        raise _refusal(err)
    doctors, patients = _names(db)
    appt = outcome.appointment
    return ApprovalOut(
        id=outcome.approval.id,
        action=outcome.approval.action,
        status=outcome.approval.status,
        patient_id=outcome.approval.patient_id,
        patient_name=patients.get(outcome.approval.patient_id, outcome.approval.patient_id),
        appointment_id=outcome.approval.appointment_id,
        appointment=_out(appt, doctors, patients) if appt else None,
        payload=json.loads(outcome.approval.payload) if outcome.approval.payload else None,
        created_at=outcome.approval.created_at,
        decided_at=outcome.approval.decided_at,
    )


# --- R2: completed visits (note required) ----------------------------------

@router.post("/appointments/{appt_id}/complete", response_model=AppointmentOut)
def complete(
    appt_id: int,
    body: CompleteIn,
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> AppointmentOut:
    """A doctor marks their own appointment as completed with a required note.

    The note is what unlocks a follow-up and feeds the R6 summary.
    """
    if identity.role != "doctor":
        raise _refusal(Refusal("WRONG_ROLE", "Only a doctor can mark a visit as completed.", 403))

    note = (body.note or "").strip()
    if not note:
        raise _refusal(Refusal("NOTE_REQUIRED", "A short note is required when completing a visit."))

    appt = db.get(Appointment, appt_id)
    if appt is None:
        raise _refusal(Refusal("NOT_FOUND", "That appointment does not exist.", 404))
    if appt.doctor_id != identity.doctor_id:
        raise _refusal(Refusal("NOT_YOUR_APPOINTMENT", "You can only complete your own appointments.", 403))
    if appt.status != "booked":
        raise _refusal(Refusal("NOT_ACTIVE", f"That appointment is already {appt.status}.", 409))

    appt.status = "completed"
    appt.doctor_note = note
    db.add(appt)
    db.commit()
    db.refresh(appt)
    doctors, patients = _names(db)
    return _out(appt, doctors, patients)


# --- reading appointments, scoped by role -----------------------------------

@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    doctor_id: str | None = None,
    patient_id: str | None = None,
    status: str | None = None,
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> list[AppointmentOut]:
    """Patient: only their own. Doctor: only their own schedule. Staff: all.

    The scope comes from the token, so a patient cannot ask for someone
    else's appointment even by calling the API directly (R3).
    """
    query = select(Appointment)

    if identity.role == "patient":
        query = query.where(Appointment.patient_id == identity.patient_id)
    elif identity.role == "doctor":
        query = query.where(Appointment.doctor_id == identity.doctor_id)
        if doctor_id and doctor_id != identity.doctor_id:
            raise _refusal(Refusal("WRONG_ROLE", "You can only see your own schedule.", 403))
    else:  # staff may filter by anyone
        if doctor_id:
            query = query.where(Appointment.doctor_id == doctor_id)
        if patient_id:
            query = query.where(Appointment.patient_id == patient_id)

    if date:
        query = query.where(Appointment.date == date)
    if status:
        query = query.where(Appointment.status == status)

    rows = db.exec(query.order_by(Appointment.date, Appointment.start_time)).all()
    doctors, patients = _names(db)
    approvals = _approvals_for(db, [a.id for a in rows if a.id is not None])
    return [
        _out(a, doctors, patients, approval=approvals.get(a.id)) for a in rows
    ]
