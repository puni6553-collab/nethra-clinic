"""Iteration 2 LLM endpoints: Fact of the Day, urgent cases, doctor summary.

Role checks live here in the backend — the screens only render what they get.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import config, llm
from ..database import get_db
from ..models import Appointment, Doctor, Patient, UrgentCase
from ..schemas import DoctorSummaryOut, FactOut, RedFlagIn, RedFlagOut, UrgentCaseOut
from ..security import Identity, current_doctor, current_identity, current_patient, current_staff

router = APIRouter(prefix="/api", tags=["llm"])


# --- R5: Fact of the Day ---------------------------------------------------

@router.get("/fact-of-the-day", response_model=FactOut)
def fact_of_the_day(
    previous: str | None = None,
    identity: Identity = Depends(current_patient),
    db: Session = Depends(get_db),
) -> FactOut:
    """One general eye-health fact for the patient card.

    `previous` is the fact currently on screen so Refresh asks for a different
    one. The caveat is always added here, never by the model.
    """
    _ = identity, db
    fact = llm.fact_of_the_day(previous)
    return FactOut(fact=fact, caveat=config.FACT_CAVEAT)


# --- R4: red-flag screen (handy for tests / the booking pre-check) ---------

@router.post("/redflag", response_model=RedFlagOut)
def redflag(
    body: RedFlagIn,
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> RedFlagOut:
    """Score one reason with the same chain the booking route uses."""
    _ = identity, db
    from ..llm import LlmUnavailable

    try:
        result = llm.check_red_flags(body.reason)
    except LlmUnavailable as exc:
        print(f"[redflag] direct check failed: {exc}", flush=True)
        raise HTTPException(
            status_code=503,
            detail={"code": "REDFLAG_CHECK_FAILED", "message": config.REDFLAG_CHECK_FAILED},
        ) from exc
    return RedFlagOut(red_flag=result.red_flag, category=result.category, why=result.why)


# --- R4: urgent cases (staff) ----------------------------------------------

@router.get("/urgent-cases", response_model=list[UrgentCaseOut])
def list_urgent_cases(
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> list[UrgentCaseOut]:
    """Unseen first (newest), then seen — the Staff panel's order."""
    _ = identity
    rows = db.exec(select(UrgentCase).order_by(UrgentCase.seen, UrgentCase.id.desc())).all()
    patients = {p.patient_id: p.name for p in db.exec(select(Patient)).all()}
    return [
        UrgentCaseOut(
            id=r.id,
            patient_id=r.patient_id,
            patient_name=patients.get(r.patient_id, r.patient_id),
            reason=r.reason,
            category=r.category,
            why=r.why,
            created_at=r.created_at,
            seen=bool(r.seen),
            source=r.source or "booking",
        )
        for r in rows
    ]


@router.post("/urgent-cases/{case_id}/seen", response_model=UrgentCaseOut)
def mark_seen(
    case_id: int,
    identity: Identity = Depends(current_staff),
    db: Session = Depends(get_db),
) -> UrgentCaseOut:
    _ = identity
    row = db.get(UrgentCase, case_id)
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "That urgent case does not exist."})
    row.seen = True
    db.add(row)
    db.commit()
    db.refresh(row)
    patients = {p.patient_id: p.name for p in db.exec(select(Patient)).all()}
    return UrgentCaseOut(
        id=row.id,
        patient_id=row.patient_id,
        patient_name=patients.get(row.patient_id, row.patient_id),
        reason=row.reason,
        category=row.category,
        why=row.why,
        created_at=row.created_at,
        seen=True,
        source=row.source or "booking",
    )


# --- R6: doctor summary ----------------------------------------------------

def _age_from(year: int | None, today: datetime) -> int | None:
    if not year:
        return None
    return max(0, today.year - year)


@router.get("/doctors/patients/{patient_id}/summary", response_model=DoctorSummaryOut)
def patient_summary(
    patient_id: str,
    appointment_id: int | None = None,
    identity: Identity = Depends(current_doctor),
    db: Session = Depends(get_db),
) -> DoctorSummaryOut:
    """AI summary of recorded notes — doctors only (role checked above).

    `appointment_id` is the schedule row the doctor opened, so "today's
    reason" is that booking's reason even when it is booked for a future
    date (handout §09 books patients weeks ahead).
    """
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "That patient does not exist."})

    doctors = {d.doctor_id: d.name for d in db.exec(select(Doctor)).all()}
    visits = db.exec(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status == "completed",
        )
        .order_by(Appointment.date.desc(), Appointment.start_time.desc())
    ).all()

    # The reason for the appointment the doctor clicked, if any. It must be
    # this doctor's own appointment with this patient — a doctor can only
    # open patients from their own schedule.
    today_reason = None
    if appointment_id is not None:
        clicked = db.get(Appointment, appointment_id)
        if (
            clicked is None
            or clicked.patient_id != patient_id
            or clicked.doctor_id != identity.doctor_id
        ):
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "That appointment does not exist."})
        today_reason = clicked.reason
    if not today_reason:
        # Fallback: an appointment with this doctor today, if any.
        today_str = config.now().date().isoformat()
        today_row = db.exec(
            select(Appointment).where(
                Appointment.patient_id == patient_id,
                Appointment.doctor_id == identity.doctor_id,
                Appointment.date == today_str,
                Appointment.status == "booked",
            )
        ).first()
        today_reason = today_row.reason if today_row else None
    # Fall back to the most recent reason if there is no booking today.
    if not today_reason and visits:
        today_reason = visits[0].reason

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

    summary = llm.summarise_patient(
        name=patient.name,
        age=_age_from(patient.year_of_birth, config.now()),
        general_health=patient.general_health,
        allergies=patient.allergies,
        today_reason=today_reason,
        visits=visit_dicts,
    )

    notes = [
        {
            "date": v.date,
            "start_time": v.start_time,
            "doctor": doctors.get(v.doctor_id, v.doctor_id),
            "speciality": v.speciality,
            "visit_type": v.visit_type,
            "reason": v.reason,
            "note": v.doctor_note,
        }
        for v in visits
    ]

    return DoctorSummaryOut(
        patient_id=patient.patient_id,
        patient_name=patient.name,
        summary=summary,
        label="AI summary of recorded notes. Check the notes below.",
        notes=notes,
    )
