"""Login for the three roles (R3) plus the doctor catalogue."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_db
from ..models import Doctor, Patient
from ..schemas import DoctorOut, LoginIn, LoginOut, RegisterIn
from ..security import Identity, current_identity, login_doctor, login_patient, login_staff
from ..security import register_patient

router = APIRouter(prefix="/api", tags=["auth"])


def _session_out(who: Identity) -> LoginOut:
    """Every auth endpoint returns the same shape; build it in one place."""
    return LoginOut(
        token=who.token,
        role=who.role,
        name=who.name,
        patient_id=who.patient_id,
        doctor_id=who.doctor_id,
    )


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)) -> LoginOut:
    """Password-less login.

    Patient: mobile + name (a new mobile creates a new patient).
    Doctor:  pick your name from the list.
    Staff:   any name.
    """
    if body.role == "patient":
        who = login_patient(db, mobile=body.mobile or "", name=body.name or "")
    elif body.role == "staff":
        who = login_staff(db, name=body.name or "")
    else:
        who = login_doctor(db, doctor_id=body.doctor_id, name=body.name)
    return _session_out(who)


@router.post("/register", response_model=LoginOut)
def register(body: RegisterIn, db: Session = Depends(get_db)) -> LoginOut:
    """Create a new patient account and sign them in, ready to book.

    Registration is a patient's own first step; reception adding a walk-in
    goes through booking instead, which creates the patient on the spot.
    """
    who = register_patient(
        db,
        mobile=body.mobile,
        name=body.name,
        year_of_birth=body.year_of_birth,
    )
    return _session_out(who)


@router.get("/me", response_model=LoginOut)
def me(identity: Identity = Depends(current_identity)) -> LoginOut:
    return _session_out(identity)


@router.get("/doctors", response_model=list[DoctorOut])
def list_doctors(db: Session = Depends(get_db)) -> list[DoctorOut]:
    """All 5 doctors and their specialities (R1 check: log in as Asha Verma,
    see all 5). Public so the login screen can offer the doctor list."""
    out = []
    for doc in db.exec(select(Doctor).order_by(Doctor.doctor_id)).all():
        out.append(DoctorOut(
            doctor_id=doc.doctor_id,
            name=doc.name,
            specialities=[s.strip() for s in doc.specialities.split(";") if s.strip()],
        ))
    return out


@router.get("/patients")
def list_patients(identity: Identity = Depends(current_identity),
                  db: Session = Depends(get_db)) -> list[dict]:
    """Patient directory — staff only, so patients cannot browse others."""
    if identity.role != "staff":
        raise HTTPException(403, detail={
            "code": "WRONG_ROLE",
            "message": "Only staff can see the patient list.",
        })
    return [
        {
            "patient_id": p.patient_id,
            "name": p.name,
            "mobile": p.mobile,
            "general_health": p.general_health,
            "allergies": p.allergies,
        }
        for p in db.exec(select(Patient).order_by(Patient.patient_id)).all()
    ]
