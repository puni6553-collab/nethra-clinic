"""The rules engine.

Every clinic rule lives here, in one place, and the API calls it before it
will book or cancel anything. Nothing in the frontend is trusted: an AI
agent can call these endpoints directly with no screen in
between, so a rule that is only enforced in React does not exist.

Each check returns a plain-English message plus a machine-readable reason
code (DOUBLE_BOOKED, DOCTOR_OFF, ...), so refusals can be shown in words and
asserted in tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls, datetime, time as time_cls, timedelta

from sqlmodel import Session, select # type: ignore

from . import config
from .models import Appointment, Doctor, DoctorLeave, Holiday, Patient, WeeklySchedule

# Monday..Sunday, matching weekly_schedule.csv
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FULL_DAY = {
    "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
    "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday",
}
SESSION_LABEL = {"AM": "morning", "PM": "afternoon"}


class Refusal(Exception):
    """A booking or cancel that breaks a clinic rule.

    Raised by the rules engine, caught by the route, and turned into an HTTP
    response carrying `code` and a plain-English `message`.
    """

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # Set by the booking action when a red flag was seen before a rule
        # fired, so the refusal can carry the emergency response with it.
        self.emergency_message: str | None = None


# --- small helpers ---------------------------------------------------------

def parse_date(value: str) -> date_cls:
    try:
        return date_cls.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise Refusal("INVALID_DATE", "That date is not in the format YYYY-MM-DD.")


def parse_time(value: str) -> time_cls:
    try:
        raw = str(value).strip()
        parts = raw.split(":")
        if len(parts) == 2:
            hour, minute = int(parts[0]), int(parts[1])
        elif len(parts) == 1:
            hour, minute = int(parts[0]), 0
        else:
            raise ValueError
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
        return time_cls(hour, minute)
    except (TypeError, ValueError):
        raise Refusal("INVALID_START_TIME", "That start time is not in the format HH:MM, for example 09:00.")


def to_minutes(value: time_cls) -> int:
    return value.hour * 60 + value.minute


def fmt_time(value: time_cls) -> str:
    return value.strftime("%H:%M")


def session_for(start: time_cls) -> tuple[str, time_cls, time_cls] | None:
    """Which clinic session does this start time fall in? None = outside."""
    for name, spec in config.SESSIONS.items():
        s = parse_time(spec["start"])
        e = parse_time(spec["end"])
        if s <= start < e:
            return name, s, e
    return None


def session_of_point(point: time_cls) -> str | None:
    """Session containing a point in time, including the closing minute.

    Used to look up the schedule for an *end* time and for existing
    appointments, where 13:00 still counts as the end of the AM session.
    """
    for name, spec in config.SESSIONS.items():
        s = parse_time(spec["start"])
        e = parse_time(spec["end"])
        if s <= point <= e:
            return name
    return None


def duration_for(visit_type: str) -> int:
    if visit_type not in config.DURATION_MINUTES:
        raise Refusal("INVALID_VISIT_TYPE", "Visit type must be 'new' or 'follow_up'.")
    return config.DURATION_MINUTES[visit_type]


def get_doctor(db: Session, doctor_id: str) -> Doctor:
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise Refusal("UNKNOWN_DOCTOR", "That doctor does not exist.", status=404)
    return doctor


def specialities_of(doctor: Doctor) -> list[str]:
    return [s.strip() for s in doctor.specialities.split(";") if s.strip()]


def schedule_status(db: Session, doctor_id: str, day: str, session: str) -> str | None:
    """OPD / SURGERY / OFF, or None when the doctor has no row that day."""
    row = db.exec(
        select(WeeklySchedule).where(
            WeeklySchedule.doctor_id == doctor_id,
            WeeklySchedule.day == day,
            WeeklySchedule.session == session,
        )
    ).first()
    return row.status if row else None


def active_appointments(
    db: Session, doctor_id: str, day: date_cls, *, exclude_id: int | None = None
) -> list[Appointment]:
    """Appointments that hold the doctor's time on a date (cancelled ones do not).

    `exclude_id` leaves one row out — a reschedule validates the new booking
    as if the appointment it replaces were already gone.
    """
    query = select(Appointment).where(
        Appointment.doctor_id == doctor_id,
        Appointment.date == day.isoformat(),
        Appointment.status != "cancelled",
    )
    if exclude_id is not None:
        query = query.where(Appointment.id != exclude_id)
    return db.exec(query).all()


def overlaps(existing: list[Appointment], start: time_cls, end: time_cls) -> Appointment | None:
    """Interval overlap: [start, end) vs [a.start, a.end). Touching is fine."""
    for appt in existing:
        a_start = parse_time(appt.start_time)
        a_end = parse_time(appt.end_time)
        if start < a_end and a_start < end:
            return appt
    return None


# --- the checks ------------------------------------------------------------

def check_clinic_open(db: Session, day: date_cls) -> None:
    if day.weekday() == config.CLOSED_WEEKDAY:
        raise Refusal("CLINIC_CLOSED", "The clinic is closed on Sundays.")
    holiday = db.get(Holiday, day.isoformat())
    if holiday is not None:
        raise Refusal("HOLIDAY", f"The clinic is closed on {day.isoformat()} ({holiday.name}).")


def check_booking_window(db: Session, day: date_cls, start: time_cls) -> None:
    now = config.now()
    today = now.date()
    when = datetime.combine(day, start)
    if when <= now:
        raise Refusal("IN_PAST", "That time has already passed. Please choose a future slot.")
    if day > today + timedelta(days=config.BOOKING_WINDOW_DAYS):
        raise Refusal(
            "BEYOND_30_DAYS",
            f"Appointments can only be booked up to {config.BOOKING_WINDOW_DAYS} days ahead.",
        )


def check_start_alignment(start: time_cls) -> None:
    if to_minutes(start) % config.SLOT_STEP_MINUTES != 0:
        raise Refusal(
            "INVALID_START_TIME",
            "Start times must fall on 10-minute marks, for example 09:00, 09:10, 09:20.",
        )


def check_session_window(start: time_cls, end: time_cls) -> str:
    """Return the session name, or raise why this time cannot be booked."""
    found = session_for(start)
    if found is None:
        hours = " and ".join(f"{s['start']}-{s['end']}" for s in config.SESSIONS.values())
        raise Refusal(
            "OUTSIDE_SESSION",
            f"{fmt_time(start)} is outside clinic hours. The clinic sees patients {hours}.",
        )
    name, s_start, s_end = found
    if end > s_end:
        raise Refusal(
            "ENDS_AFTER_SESSION",
            f"An appointment starting at {fmt_time(start)} would end at {fmt_time(end)}, "
            f"after the {name} session ends at {fmt_time(s_end)}.",
        )
    return name


def check_doctor_schedule(db: Session, doctor: Doctor, day: date_cls, session: str) -> None:
    day_name = DAY_NAMES[day.weekday()]
    status = schedule_status(db, doctor.doctor_id, day_name, session)
    if status is None or status == "OFF":
        raise Refusal(
            "DOCTOR_OFF",
            f"{doctor.name} is not at the clinic on {FULL_DAY[day_name]}s.",
        )
    if status == "SURGERY":
        raise Refusal(
            "SURGERY_SESSION",
            f"{doctor.name} is in surgery on {FULL_DAY[day_name]} {SESSION_LABEL[session]}.",
        )


# --- doctor leave (R7) ------------------------------------------------------

def leave_covers(leave_session: str, session: str) -> bool:
    """Does a leave marked AM/PM/FULL cover this clinic session?"""
    return leave_session == "FULL" or leave_session == session


def doctor_on_leave(db: Session, doctor_id: str, day: date_cls, session: str) -> DoctorLeave | None:
    """The leave row that shuts this doctor's session, or None."""
    rows = db.exec(
        select(DoctorLeave).where(
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.date == day.isoformat(),
        )
    ).all()
    for row in rows:
        if leave_covers(row.session, session):
            return row
    return None


def check_doctor_leave(db: Session, doctor: Doctor, day: date_cls, session: str) -> None:
    """No booking in a session the doctor has marked as leave (R7)."""
    if doctor_on_leave(db, doctor.doctor_id, day, session) is None:
        return
    day_name = DAY_NAMES[day.weekday()]
    raise Refusal(
        "DOCTOR_ON_LEAVE",
        f"{doctor.name} is on leave on {FULL_DAY[day_name]} {SESSION_LABEL[session]}.",
    )


def check_speciality(db: Session, doctor: Doctor, speciality: str) -> None:
    if speciality not in specialities_of(doctor):
        treats = ", ".join(specialities_of(doctor))
        raise Refusal(
            "SPECIALITY_MISMATCH",
            f"{doctor.name} does not cover {speciality}. {doctor.name} covers: {treats}.",
        )


def check_slot_free(
    db: Session, doctor: Doctor, day: date_cls, start: time_cls, end: time_cls,
    *, exclude_id: int | None = None,
) -> None:
    taken = overlaps(active_appointments(db, doctor.doctor_id, day, exclude_id=exclude_id), start, end)
    if taken is not None:
        raise Refusal(
            "DOUBLE_BOOKED",
            f"That slot is already taken ({fmt_time(parse_time(taken.start_time))}-"
            f"{fmt_time(parse_time(taken.end_time))}). Please pick another time.",
        )


def check_one_active_per_speciality(
    db: Session, patient_id: str, speciality: str, *, exclude_id: int | None = None
) -> None:
    # "proposed" counts: a rebooking after doctor leave holds the patient's
    # one active slot in that speciality until Staff approve or reject it.
    query = select(Appointment).where(
        Appointment.patient_id == patient_id,
        Appointment.speciality == speciality,
        Appointment.status.in_(("booked", "proposed")),
    )
    if exclude_id is not None:
        query = query.where(Appointment.id != exclude_id)
    held = db.exec(query).first()
    if held is not None:
        if held.status == "proposed":
            raise Refusal(
                "ACTIVE_BOOKING_EXISTS",
                f"This patient has a {speciality} appointment on {held.date} at "
                f"{held.start_time} waiting for Staff to approve a rebooking. "
                "Staff must approve or reject it first.",
            )
        raise Refusal(
            "ACTIVE_BOOKING_EXISTS",
            f"This patient already has a {speciality} appointment on "
            f"{held.date} at {held.start_time}. Cancel it before booking another.",
        )


def last_completed_visit(db: Session, patient_id: str, speciality: str) -> Appointment | None:
    """Most recent COMPLETED visit in a speciality — the follow-up anchor."""
    visits = db.exec(
        select(Appointment).where(
            Appointment.patient_id == patient_id,
            Appointment.speciality == speciality,
            Appointment.status == "completed",
        )
    ).all()
    if not visits:
        return None
    return max(visits, key=lambda a: (a.date, a.start_time))


def check_follow_up(db: Session, doctor: Doctor, patient_id: str, speciality: str) -> None:
    visit = last_completed_visit(db, patient_id, speciality)
    if visit is None:
        raise Refusal(
            "NO_PREVIOUS_VISIT",
            f"A follow-up needs a completed {speciality} visit first. "
            "This patient has not been seen for that speciality.",
        )
    if visit.doctor_id != doctor.doctor_id:
        seen = db.get(Doctor, visit.doctor_id)
        seen_name = seen.name if seen else "another doctor"
        raise Refusal(
            "FOLLOW_UP_DIFFERENT_DOCTOR",
            f"This patient's last {speciality} visit was completed by {seen_name}. "
            "A follow-up can only be booked with the doctor who completed the visit.",
        )


def check_not_cancelled_or_completed(appt: Appointment) -> None:
    if appt.status != "booked":
        raise Refusal("NOT_ACTIVE", "That appointment is not active, so it cannot be changed.", status=409)


# --- the two entry points the API uses ------------------------------------

def validate_booking(
    db: Session,
    *,
    patient_id: str,
    doctor_id: str,
    speciality: str,
    visit_type: str,
    date: str,
    start_time: str,
    exclude_id: int | None = None,
) -> Appointment:
    """Run every booking rule in order. Returns the appointment to insert.

    Raises Refusal with a clear code + message the moment a rule breaks.
    Order matters: cheap, absolute checks (calendar, session, schedule) come
    before patient-specific ones, so the message names the *real* reason.

    `exclude_id` is the appointment a reschedule is replacing: the new
    booking is validated as if that one were already cancelled, so moving
    an appointment onto the same doctor/speciality does not trip
    DOUBLE_BOOKED or ACTIVE_BOOKING_EXISTS against itself.
    """
    doctor = get_doctor(db, doctor_id)
    duration = duration_for(visit_type)
    day = parse_date(date)
    start = parse_time(start_time)
    end = (datetime.combine(day, start) + timedelta(minutes=duration)).time()

    # 1. The calendar: is the clinic open, is the time real, is it bookable?
    check_clinic_open(db, day)
    check_start_alignment(start)
    check_booking_window(db, day, start)
    session = check_session_window(start, end)

    # 2. The doctor: are they here, not operating, and not on leave?
    check_doctor_schedule(db, doctor, day, session)
    check_doctor_leave(db, doctor, day, session)

    # 3. The visit itself: speciality, free slot, one active per speciality.
    check_speciality(db, doctor, speciality)
    check_slot_free(db, doctor, day, start, end, exclude_id=exclude_id)
    check_one_active_per_speciality(db, patient_id, speciality, exclude_id=exclude_id)

    # 4. Follow-ups need a completed visit with this same doctor.
    if visit_type == "follow_up":
        check_follow_up(db, doctor, patient_id, speciality)

    return Appointment(
        patient_id=patient_id,
        doctor_id=doctor.doctor_id,
        speciality=speciality,
        visit_type=visit_type,
        date=day.isoformat(),
        start_time=fmt_time(start),
        end_time=fmt_time(end),
        status="booked",
    )


def validate_cancel(db: Session, appt: Appointment, *, acting_patient_id: str | None) -> None:
    """Patient rule: only their own appointments, and only >2 hours ahead.

    `acting_patient_id` is None for Staff, who may cancel anything.
    """
    if acting_patient_id is not None and appt.patient_id != acting_patient_id:
        raise Refusal("NOT_YOUR_APPOINTMENT", "That appointment belongs to another patient.", status=403)

    if appt.status != "booked":
        raise Refusal(
            "NOT_ACTIVE",
            f"That appointment is already {appt.status}, so it cannot be cancelled.",
            status=409,
        )

    if acting_patient_id is None:
        return  # Staff can cancel at any time, including inside 2 hours.

    starts = datetime.combine(parse_date(appt.date), parse_time(appt.start_time))
    hours_left = (starts - config.now()).total_seconds() / 3600
    if hours_left < config.CANCEL_CUTOFF_HOURS:
        raise Refusal(
            "INSIDE_2_HOURS",
            "This appointment starts in less than 2 hours. Please contact Staff to cancel it.",
            status=403,
        )


def validate_reschedule(
    db: Session,
    appt: Appointment,
    *,
    date: str,
    start_time: str,
    doctor_id: str | None = None,
    acting_patient_id: str | None = None,
) -> Appointment:
    """Check a reschedule: the cancel half, then the new booking.

    One action, exactly as the brief asks: the new booking is validated as
    if the appointment it replaces were already cancelled, and only then
    may the caller cancel the old row and insert the new one together. If
    any rule refuses, nothing has been touched and the original appointment
    stands exactly as it was.

    Returns the NEW appointment, unsaved. Speciality and visit type always
    come from the original appointment — changing those is a cancel plus a
    booking, not a move.

    Raises INSIDE_2_HOURS for a patient moving something that starts within
    the cut-off: the route turns that into an approval request for Staff
    rather than a flat refusal.
    """
    if acting_patient_id is not None and appt.patient_id != acting_patient_id:
        raise Refusal("NOT_YOUR_APPOINTMENT", "That appointment belongs to another patient.", status=403)

    if appt.status != "booked":
        raise Refusal(
            "NOT_ACTIVE",
            f"That appointment is already {appt.status}, so it cannot be changed.",
            status=409,
        )

    # The book half first: refuse an impossible new time before we ever get
    # to the cut-off, so Staff are never asked to approve a move that the
    # rules would then reject.
    new = validate_booking(
        db,
        patient_id=appt.patient_id,
        doctor_id=doctor_id or appt.doctor_id,
        speciality=appt.speciality,
        visit_type=appt.visit_type,
        date=date,
        start_time=start_time,
        exclude_id=appt.id,
    )

    # The cancel half: same 2-hour cut-off as a straight cancellation,
    # measured on the appointment being moved (reschedule = cancel + book).
    if acting_patient_id is not None:
        starts = datetime.combine(parse_date(appt.date), parse_time(appt.start_time))
        hours_left = (starts - config.now()).total_seconds() / 3600
        if hours_left < config.CANCEL_CUTOFF_HOURS:
            raise Refusal(
                "INSIDE_2_HOURS",
                "This appointment starts in less than 2 hours. Please contact Staff to change it.",
                status=403,
            )

    return new
