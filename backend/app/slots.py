"""Free-slot generation (requirement R4).

Given a doctor, a date and a visit type, list the start times that can
actually be booked. It runs the *same* checks as the booking rules, so:
anything this offers, the API will accept; anything it withholds, the API
would refuse.
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta

from sqlmodel import Session

from . import config
from .rules import (
    Refusal,
    active_appointments,
    check_clinic_open,
    check_doctor_schedule,
    doctor_on_leave,
    duration_for,
    fmt_time,
    get_doctor,
    overlaps,
    parse_date,
    parse_time,
    to_minutes,
)


def free_slots(db: Session, *, doctor_id: str, date: str, visit_type: str) -> list[str]:
    """Every start time (HH:MM) that is free for this doctor on this date."""
    doctor = get_doctor(db, doctor_id)
    duration = duration_for(visit_type)
    day = parse_date(date)

    # The clinic must be open and the date reachable: a Sunday, a holiday or
    # a day outside the 30-day window simply has no slots to offer.
    check_clinic_open(db, day)
    today = config.now().date()
    if day < today or day > today + timedelta(days=config.BOOKING_WINDOW_DAYS):
        return []

    held = active_appointments(db, doctor.doctor_id, day)  # cancelled ones don't hold time
    now = config.now()
    day_start = datetime.combine(day, datetime.min.time())
    starts: list[str] = []
    for session_name, spec in config.SESSIONS.items():
        # Surgery, off, or marked leave for this session -> no slots at all.
        try:
            check_doctor_schedule(db, doctor, day, session_name)
        except Refusal:
            continue
        if doctor_on_leave(db, doctor.doctor_id, day, session_name) is not None:
            continue  # R7: the doctor has marked this session as leave

        s_start = to_minutes(parse_time(spec["start"]))
        s_end = to_minutes(parse_time(spec["end"]))

        for minutes in range(s_start, s_end, config.SLOT_STEP_MINUTES):
            start = (day_start + timedelta(minutes=minutes)).time()
            end = (day_start + timedelta(minutes=minutes + duration)).time()
            if minutes + duration > s_end:
                break  # would run past the end of the session
            if day_start + timedelta(minutes=minutes) <= now:
                continue  # already gone (only bites on today)
            if overlaps(held, start, end):
                continue  # someone already holds this stretch
            starts.append(fmt_time(start))

    return sorted(set(starts))
