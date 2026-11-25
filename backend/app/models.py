"""Database tables.

Dates and times are stored as text ("2026-11-04", "09:00") on purpose: the
data pack uses that format, ISO strings sort correctly, and the rules engine
never has to guess about time zones.

Iteration 2 adds appointment.reason / appointment.doctor_note, patient
profiles (general_health, allergies) and the urgent_cases table that the
red-flag check writes to.
"""
from typing import Optional

from sqlmodel import Field, SQLModel


class Doctor(SQLModel, table=True):
    __tablename__ = "doctors"

    doctor_id: str = Field(primary_key=True)
    name: str
    specialities: str  # "Cataract;General" - semicolon separated, as in the CSV


class WeeklySchedule(SQLModel, table=True):
    __tablename__ = "weekly_schedule"

    id: Optional[int] = Field(default=None, primary_key=True)
    doctor_id: str
    day: str  # Mon..Sat
    session: str  # AM | PM
    status: str  # OPD | SURGERY | OFF


class Holiday(SQLModel, table=True):
    __tablename__ = "holidays"

    date: str = Field(primary_key=True)  # "2026-11-09"
    name: str


class Patient(SQLModel, table=True):
    __tablename__ = "patients"

    patient_id: str = Field(primary_key=True)  # "P01"
    name: str
    mobile: str
    year_of_birth: Optional[int] = None
    # Iteration 2 — patient_profiles.csv
    general_health: Optional[str] = None
    allergies: Optional[str] = None


class Appointment(SQLModel, table=True):
    __tablename__ = "appointments"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: str
    doctor_id: str
    speciality: str
    visit_type: str  # new | follow_up
    date: str  # "2026-11-04"
    start_time: str  # "09:00"
    end_time: str  # "09:20"
    status: str  # booked | cancelled | completed
    # Iteration 2 — booking reason (required) and the doctor's completion note
    reason: Optional[str] = None
    doctor_note: Optional[str] = None
    # True when the booking reason tripped the red-flag check (R4)
    urgent: bool = False
    # Iteration 4 (R7): a rebooking after doctor leave. The proposal is a
    # real appointment row with status "proposed" so it holds its slot; it
    # points back at the appointment it replaces and carries the one line
    # the patient is shown.
    moved_from_id: Optional[int] = None
    move_note: Optional[str] = None  # "Moved because Dr. Rao is on leave"
    # Iteration 6 (R2): where the booking was made — Website | Chat | Staff | MCP.
    booked_via: str = "Website"
    # Iteration 6 (R5): the LASIK intake answers, seen by Dr. Khan on the
    # appointment (optional; only the suitability-intake skill fills it).
    intake_notes: Optional[str] = None


class DoctorLeave(SQLModel, table=True):
    """A doctor's leave for a date and session (R7).

    Marked by the doctor for the next 30 days. FREE slots skip these
    sessions and the booking rules refuse them with DOCTOR_ON_LEAVE.
    """

    __tablename__ = "doctor_leave"

    id: Optional[int] = Field(default=None, primary_key=True)
    doctor_id: str
    date: str  # "2026-11-16"
    session: str  # AM | PM | FULL
    created_at: str = ""


class Rebooking(SQLModel, table=True):
    """One appointment moved because its doctor went on leave (R7).

    Created by the fixed rebooking steps in `leave.py`, never by the agent.
    Staff Approve turns the proposal into a booked appointment; Reject frees
    the slot it was holding.
    """

    __tablename__ = "rebookings"

    id: Optional[int] = Field(default=None, primary_key=True)
    leave_id: int
    original_appointment_id: int
    proposed_appointment_id: Optional[int] = None
    # proposed | approved | rejected | no_slot ("No slot: call the patient")
    status: str = "proposed"
    created_at: str = ""
    decided_at: Optional[str] = None


class ApprovalRequest(SQLModel, table=True):
    """A patient's cancel or reschedule that starts inside 2 hours (R1).

    The appointment stays `booked` while this is pending: nothing changes
    until Staff click Approve, which carries the action out as Staff, or
    Reject, which leaves the appointment exactly as it was.
    """

    __tablename__ = "approval_requests"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: str
    appointment_id: int
    action: str  # cancel | reschedule
    # For a reschedule: the target booking as JSON text (no new table).
    payload: Optional[str] = None
    status: str = "pending"  # pending | approved | rejected
    created_at: str = ""
    decided_at: Optional[str] = None


class UrgentCase(SQLModel, table=True):
    """A red-flag reason saved before the rules check (R4).

    Lives at the top of the Staff view until someone marks it seen.
    """

    __tablename__ = "urgent_cases"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: str
    reason: str
    category: Optional[str] = None  # one of the 5 brief categories, or None
    why: str = ""  # one-line model explanation
    created_at: str = ""  # ISO timestamp from config.now()
    seen: bool = False
    # Iteration 3: "booking" (default) or "chat" — where the red flag came from.
    source: str = "booking"
