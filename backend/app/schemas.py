"""Request/response shapes. Pydantic keeps the API honest about its input."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    role: Literal["patient", "staff", "doctor"]
    # patient: mobile + name. A new mobile creates a new patient.
    mobile: str | None = None
    name: str | None = None
    # doctor: pick from the list (id or name is accepted).
    doctor_id: str | None = None


class LoginOut(BaseModel):
    token: str
    role: Literal["patient", "staff", "doctor"]
    name: str
    patient_id: str | None = None
    doctor_id: str | None = None


class RegisterIn(BaseModel):
    """A new patient signing up. Year of birth is optional — the clinic only
    asks for it because some specialities skew by age, not because it is
    needed to book."""
    mobile: str
    name: str
    year_of_birth: int | None = Field(default=None, ge=1900, le=2100)


class DoctorOut(BaseModel):
    doctor_id: str
    name: str
    specialities: list[str]


class SlotOut(BaseModel):
    date: str
    doctor_id: str
    visit_type: Literal["new", "follow_up"]
    duration_minutes: int
    slots: list[str]
    notes: list[str] = Field(default_factory=list)


class AppointmentIn(BaseModel):
    doctor_id: str
    speciality: str
    visit_type: Literal["new", "follow_up"]
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str = Field(pattern=r"^\d{1,2}:\d{2}$")
    # Iteration 2 R3: required, max 300 characters. Empty is allowed past
    # Pydantic so the route can return MISSING_REASON (not a generic 422).
    reason: str = Field(default="", max_length=300)
    # Staff may book on behalf of any patient; patients omit this (their own
    # record is used from the token, never from the request body).
    patient_id: str | None = None
    # Staff walk-in: give mobile + name to create/find the patient.
    mobile: str | None = None
    patient_name: str | None = None
    # Iteration 6 (R5): optional pre-assessment answers for the doctor
    # (the LASIK suitability skill fills this through the chat tool).
    intake_notes: str | None = Field(default=None, max_length=4000)


class AppointmentOut(BaseModel):
    id: int
    patient_id: str
    patient_name: str
    doctor_id: str
    doctor_name: str
    speciality: str
    visit_type: str
    date: str
    start_time: str
    end_time: str
    status: str
    # Iteration 2
    reason: str | None = None
    doctor_note: str | None = None
    urgent: bool = False
    # Present only on the booking response when the reason was a red flag.
    emergency_message: str | None = None
    # Iteration 4 (R1): what the write action did —
    # booked | cancelled | rescheduled | to_staff.
    result: str | None = None
    # Iteration 4 (R1): the Staff approval waiting on this appointment, if any.
    approval_id: int | None = None
    approval_status: str | None = None  # pending | approved | rejected
    approval_action: str | None = None  # cancel | reschedule
    # Iteration 4 (R7): a rebooking after doctor leave — the row it replaces
    # and the line the patient is shown ("Moved because Dr. … is on leave").
    moved_from_id: int | None = None
    move_note: str | None = None
    # Iteration 6 (R2/R5): where it was booked, and any intake answers.
    booked_via: str = "Website"
    intake_notes: str | None = None


class RescheduleIn(BaseModel):
    """Move an appointment (R1). Speciality and visit type are not asked
    for: they come from the appointment being moved."""
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str = Field(pattern=r"^\d{1,2}:\d{2}$")
    # Omit to keep the same doctor.
    doctor_id: str | None = None


class ApprovalOut(BaseModel):
    """One queued cancel/reschedule waiting for Staff (R1)."""
    id: int
    action: str  # cancel | reschedule
    status: str  # pending | approved | rejected
    patient_id: str
    patient_name: str = ""
    appointment_id: int
    appointment: AppointmentOut | None = None
    # For a reschedule: the requested new date/start_time/doctor_id.
    payload: dict | None = None
    created_at: str = ""
    decided_at: str | None = None


# --- Iteration 4 (R7): doctor leave and Staff rebooking ---------------------

class LeaveIn(BaseModel):
    """A doctor marking leave for a date and session (R7)."""
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    session: Literal["AM", "PM", "FULL"]


class LeaveOut(BaseModel):
    id: int
    doctor_id: str
    doctor_name: str = ""
    date: str
    session: str  # AM | PM | FULL
    created_at: str = ""
    # Appointments this leave moved, so the doctor can see what happened.
    rebooked: int = 0


class RebookingOut(BaseModel):
    """One appointment moved because its doctor is on leave (R7)."""
    id: int
    status: str  # proposed | approved | rejected | no_slot
    leave_id: int
    doctor_id: str
    doctor_name: str = ""
    leave_date: str
    leave_session: str
    original: AppointmentOut | None = None
    proposed: AppointmentOut | None = None
    # "No slot: call the patient" when there was nothing to propose.
    message: str = ""
    created_at: str = ""
    decided_at: str | None = None


class CompleteIn(BaseModel):
    """Doctor's note when marking a visit completed (R2).

    Empty is allowed past Pydantic so the route can return NOTE_REQUIRED
    with the handout's wording instead of a generic 422.
    """
    note: str = Field(default="", max_length=2000)


class CancelIn(BaseModel):
    # Empty body: who acts is decided by the token, not by the caller.
    pass


class FactOut(BaseModel):
    fact: str
    caveat: str


class RedFlagIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class RedFlagOut(BaseModel):
    red_flag: bool
    category: str | None = None
    why: str = ""


class UrgentCaseOut(BaseModel):
    id: int
    patient_id: str
    patient_name: str = ""
    reason: str
    category: str | None
    why: str
    created_at: str
    seen: bool
    source: str = "booking"  # booking | chat | mcp


class DoctorSummaryOut(BaseModel):
    patient_id: str
    patient_name: str
    summary: str
    label: str
    notes: list[dict]  # past completed visits, newest first


# --- Iteration 3 (chat), iteration 4 (agent) --------------------------------

class ChatIn(BaseModel):
    """One patient message, or the answer to a confirmation card.

    The browser sends only the new message and the thread_id (handout R4);
    `confirm` is the card's Yes/No (R5). Empty is allowed past Pydantic so
    the route can return MISSING_MESSAGE with plain words instead of a
    generic 422.
    """
    thread_id: str = Field(default="", max_length=64)
    message: str = Field(default="", max_length=500)
    # True = Confirm on the card, False = No. None = a typed message.
    confirm: bool | None = None


class ChatOut(BaseModel):
    # answer | idk | emergency | check_failed | unavailable
    type: Literal["answer", "idk", "emergency", "check_failed", "unavailable"]
    message: str
    sources: list[str] = Field(default_factory=list)
    # Iteration 6 (R4): skills loaded in this turn, for the "Skill:" line.
    skills: list[str] = Field(default_factory=list)
    # Echoed so the client can keep using the same thread (a fresh id is
    # generated when the request did not carry one).
    thread_id: str = ""
    # Set while a book/cancel/reschedule waits on the confirmation card (R5).
    pending_action: str | None = None


class SearchHit(BaseModel):
    chunk_id: str
    source: str
    section: str
    score: float
    text: str


class SearchDebugOut(BaseModel):
    query: str
    keyword: list[SearchHit]
    vector: list[SearchHit]
    hybrid: list[SearchHit]
