"""R7 — doctor leave, and the Staff rebooking list (optional, handout 05).

    POST /api/doctor/leave            the doctor marks AM / PM / FULL
    GET  /api/doctor/leave            what this doctor has marked
    GET  /api/staff/rebookings        what the leave moved, for Staff
    POST /api/staff/rebookings/{id}/approve   the proposal becomes booked
    POST /api/staff/rebookings/{id}/reject    the proposal's slot is freed

Everything that decides *what happens* lives in `app.leave` — a fixed set of
steps, not an agent. This file only checks who is asking and shapes the
answer. The rules engine still decides whether the resulting booking is
allowed (`DOCTOR_ON_LEAVE`, free slots, and a re-check on Approve).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from .. import leave as engine
from ..database import get_db
from ..models import Appointment, Doctor, DoctorLeave, Rebooking
from ..rules import Refusal
from ..schemas import LeaveIn, LeaveOut, RebookingOut
from ..security import Identity, current_doctor, current_staff
from .appointments import _names, _out

router = APIRouter(prefix="/api", tags=["leave"])


def _refusal(err: Refusal) -> HTTPException:
    return HTTPException(status_code=err.status, detail={"code": err.code, "message": err.message})


def _leave_out(db: Session, row: DoctorLeave, doctors: dict[str, str]) -> LeaveOut:
    count = len(db.exec(
        select(Rebooking).where(Rebooking.leave_id == row.id)
    ).all())
    return LeaveOut(
        id=row.id,
        doctor_id=row.doctor_id,
        doctor_name=doctors.get(row.doctor_id, row.doctor_id),
        date=row.date,
        session=row.session,
        created_at=row.created_at,
        rebooked=count,
    )


def _rebooking_out(
    db: Session, row: Rebooking, doctors: dict[str, str], patients: dict[str, str]
) -> RebookingOut:
    leave = db.get(DoctorLeave, row.leave_id)
    original = db.get(Appointment, row.original_appointment_id)
    proposed = (
        db.get(Appointment, row.proposed_appointment_id)
        if row.proposed_appointment_id
        else None
    )
    return RebookingOut(
        id=row.id,
        status=row.status,
        leave_id=row.leave_id,
        doctor_id=leave.doctor_id if leave else "",
        doctor_name=doctors.get(leave.doctor_id, leave.doctor_id) if leave else "",
        leave_date=leave.date if leave else "",
        leave_session=leave.session if leave else "",
        original=_out(original, doctors, patients) if original else None,
        proposed=_out(proposed, doctors, patients) if proposed else None,
        message="No slot: call the patient" if row.status == "no_slot" else "",
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


# --- the doctor marks leave -------------------------------------------------

@router.post("/doctor/leave", response_model=LeaveOut, status_code=201)
def mark_leave(
    body: LeaveIn,
    identity: Identity = Depends(current_doctor),
    db: Session = Depends(get_db),
) -> LeaveOut:
    """Mark leave for a date and session, and rebook what it affects."""
    try:
        row, _ = engine.mark_leave(
            db, doctor_id=identity.doctor_id or "", date=body.date, session=body.session
        )
    except Refusal as err:
        raise _refusal(err)
    doctors, _ = _names(db)
    return _leave_out(db, row, doctors)


@router.get("/doctor/leave", response_model=list[LeaveOut])
def my_leave(
    identity: Identity = Depends(current_doctor),
    db: Session = Depends(get_db),
) -> list[LeaveOut]:
    """This doctor's own leave, newest first."""
    rows = db.exec(
        select(DoctorLeave)
        .where(DoctorLeave.doctor_id == identity.doctor_id)
        .order_by(DoctorLeave.date.desc(), DoctorLeave.id.desc())
    ).all()
    doctors, _ = _names(db)
    return [_leave_out(db, r, doctors) for r in rows]


# --- Staff work through the rebookings -------------------------------------

@router.get("/staff/rebookings", response_model=list[RebookingOut])
def list_rebookings(
    status: str | None = Query(None, pattern=r"^(proposed|approved|rejected|no_slot)$"),
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> list[RebookingOut]:
    """The rebooking list — Staff only.

    With no filter this is what still needs a decision: the proposals and
    the "No slot: call the patient" rows.
    """
    _ = identity
    query = select(Rebooking)
    if status:
        query = query.where(Rebooking.status == status)
    else:
        query = query.where(Rebooking.status.in_(("proposed", "no_slot")))
    rows = db.exec(query.order_by(Rebooking.id)).all()

    doctors, patients = _names(db)
    return [_rebooking_out(db, r, doctors, patients) for r in rows]


def _get(db: Session, rebooking_id: int) -> Rebooking:
    row = db.get(Rebooking, rebooking_id)
    if row is None:
        raise _refusal(Refusal("NOT_FOUND", "That rebooking does not exist.", status=404))
    return row


@router.post("/staff/rebookings/{rebooking_id}/approve", response_model=RebookingOut)
def approve(
    rebooking_id: int,
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> RebookingOut:
    """Approve the proposal: it becomes a booked appointment."""
    _ = identity
    _get(db, rebooking_id)
    try:
        engine.approve(db, rebooking_id)
    except Refusal as err:
        raise _refusal(err)
    doctors, patients = _names(db)
    return _rebooking_out(db, _get(db, rebooking_id), doctors, patients)


@router.post("/staff/rebookings/{rebooking_id}/reject", response_model=RebookingOut)
def reject(
    rebooking_id: int,
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> RebookingOut:
    """Reject the proposal: its slot is freed and the row leaves the list."""
    _ = identity
    _get(db, rebooking_id)
    try:
        engine.reject(db, rebooking_id)
    except Refusal as err:
        raise _refusal(err)
    doctors, patients = _names(db)
    return _rebooking_out(db, _get(db, rebooking_id), doctors, patients)
