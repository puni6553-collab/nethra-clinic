"""Password-less login for the three roles (requirement R3).

There are no passwords in this project, by design — the brief calls them out
as classroom-grade, not real security. What matters is that the *backend*
decides who is asking on every request, so a patient cannot reach another
patient's appointment even by calling the API directly.

The token is issued at login and sent as `Authorization: Bearer <token>`.
It is kept in memory: simple, and enough for a demo that never restarts.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from .database import get_db
from .models import Doctor, Patient

Role = Literal["patient", "staff", "doctor"]


@dataclass
class Identity:
    """Who is making this request."""

    role: Role
    name: str
    patient_id: str | None = None
    doctor_id: str | None = None
    token: str = field(default_factory=str, repr=False)


# token -> Identity. In-memory is plenty for iteration 1.
_SESSIONS: dict[str, Identity] = {}

STAFF_NAME = "Staff"  # any non-empty staff name maps to this role


def _issue(identity: Identity) -> Identity:
    token = secrets.token_urlsafe(24)
    identity.token = token
    _SESSIONS[token] = identity
    return identity


def _check_patient_details(*, mobile: str, name: str) -> tuple[str, str]:
    """Shared validation for login and registration."""
    mobile = (mobile or "").strip()
    name = (name or "").strip()
    if not mobile.isdigit() or len(mobile) < 10:
        raise HTTPException(400, detail={
            "code": "INVALID_MOBILE",
            "message": "Please enter a valid 10-digit mobile number.",
        })
    if not name:
        raise HTTPException(400, detail={
            "code": "MISSING_NAME",
            "message": "Please enter your name.",
        })
    return mobile, name


def register_patient(db: Session, *, mobile: str, name: str,
                     year_of_birth: int | None = None) -> Identity:
    """Create a new patient account and sign them straight in.

    Kept separate from login on purpose: an existing mobile must log in rather
    than quietly overwrite the record already on file.
    """
    mobile, name = _check_patient_details(mobile=mobile, name=name)

    if db.exec(select(Patient).where(Patient.mobile == mobile)).first() is not None:
        raise HTTPException(409, detail={
            "code": "MOBILE_TAKEN",
            "message": "That mobile number is already registered. Please just log in.",
        })

    patient = Patient(patient_id=_next_patient_id(db), name=name, mobile=mobile,
                      year_of_birth=year_of_birth)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return _issue(Identity(role="patient", name=patient.name, patient_id=patient.patient_id))


class DetailsMismatch(Exception):
    """The mobile number and name do not match the record on file.

    Raised by `resolve_patient`; the HTTP layer turns it into an
    HTTPException, and the iteration 6 MCP server turns it into a plain
    refusal result, so neither place keeps its own copy of the check.
    """

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def resolve_patient(
    db: Session,
    *,
    mobile: str,
    name: str,
    mismatch_code: str = "NAME_MISMATCH",
    mismatch_message: str = "That mobile number is registered to a different name.",
    mismatch_status: int = 401,
) -> Patient:
    """Find the patient for this mobile, or create one for a brand-new mobile.

    Shared by website login and the MCP server (iteration 6): the check is
    written once, only the refusal code changes — NAME_MISMATCH at login,
    PATIENT_DETAILS_MISMATCH when a partner tries to book into someone
    else's record.
    """
    mobile = (mobile or "").strip()
    name = (name or "").strip()
    if not mobile.isdigit() or len(mobile) < 10:
        raise DetailsMismatch("INVALID_MOBILE", "Please enter a valid 10-digit mobile number.")
    if not name:
        raise DetailsMismatch("MISSING_NAME", "Please enter your name.")

    patient = db.exec(select(Patient).where(Patient.mobile == mobile)).first()
    if patient is None:
        # New mobile -> new patient record.
        patient = Patient(patient_id=_next_patient_id(db), name=name, mobile=mobile)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    elif patient.name.strip().lower() != name.lower():
        # Known mobile, wrong name: refuse rather than let anyone in.
        raise DetailsMismatch(mismatch_code, mismatch_message, status=mismatch_status)
    return patient


def login_patient(db: Session, *, mobile: str, name: str) -> Identity:
    """Mobile + name. A brand-new mobile creates a brand-new patient."""
    try:
        patient = resolve_patient(db, mobile=mobile, name=name)
    except DetailsMismatch as exc:
        raise HTTPException(exc.status, detail={
            "code": exc.code,
            "message": exc.message,
        }) from exc

    return _issue(Identity(role="patient", name=patient.name, patient_id=patient.patient_id))


def login_staff(db: Session, *, name: str) -> Identity:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, detail={
            "code": "MISSING_NAME",
            "message": "Please enter your staff name.",
        })
    return _issue(Identity(role="staff", name=name))


def login_doctor(db: Session, *, doctor_id: str | None, name: str | None) -> Identity:
    """The doctor picks their name from the list — no password."""
    doctor = None
    if doctor_id:
        doctor = db.get(Doctor, doctor_id)
    if doctor is None and name:
        doctor = db.exec(select(Doctor).where(Doctor.name == name)).first()
    if doctor is None:
        raise HTTPException(400, detail={
            "code": "UNKNOWN_DOCTOR",
            "message": "Please choose a doctor from the list.",
        })
    return _issue(Identity(role="doctor", name=doctor.name, doctor_id=doctor.doctor_id))


def _next_patient_id(db: Session) -> str:
    ids = [p.patient_id for p in db.exec(select(Patient)).all()]
    numbers = [int(i[1:]) for i in ids if i.startswith("P") and i[1:].isdigit()]
    return f"P{(max(numbers) + 1) if numbers else 1:02d}"


# --- per-request identity ---------------------------------------------------

def current_identity(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Identity:
    """Resolve the token on EVERY protected request.

    Routes ask for `staff` or `patient` by depending on the wrappers below,
    so role checks live in the backend and not in the screen.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={
            "code": "NOT_LOGGED_IN",
            "message": "Please log in first.",
        })
    token = authorization.removeprefix("Bearer ").strip()
    identity = _SESSIONS.get(token)
    if identity is None:
        raise HTTPException(401, detail={
            "code": "BAD_TOKEN",
            "message": "Your session has expired. Please log in again.",
        })
    # Touch db so the dependency signature is honest about needing a session.
    _ = db
    return identity


def current_patient(identity: Identity = Depends(current_identity)) -> Identity:
    if identity.role != "patient":
        raise HTTPException(403, detail={
            "code": "WRONG_ROLE",
            "message": "This action is for patients only.",
        })
    return identity


def current_staff(identity: Identity = Depends(current_identity)) -> Identity:
    if identity.role != "staff":
        raise HTTPException(403, detail={
            "code": "WRONG_ROLE",
            "message": "This action is for staff only.",
        })
    return identity


def current_doctor(identity: Identity = Depends(current_identity)) -> Identity:
    if identity.role != "doctor":
        raise HTTPException(403, detail={
            "code": "WRONG_ROLE",
            "message": "This action is for doctors only.",
        })
    return identity
