"""The rule audit (handout R5) — an independent check of every booking.

Written apart from the API on purpose: it reads the eval database with plain
sqlite3 and re-states brief section 04 as code, so a bug in the API (or in
`rules.py`) cannot hide itself from the booking evals.

Checks, per active appointment (status `booked` or `proposed`):

1.  start times sit on 10-minute marks
2.  not on a Sunday
3.  not on a clinic holiday
4.  within 30 days of the clinic clock
5.  inside a clinic session and ends by 1 PM / 6 PM
6.  inside the doctor's OPD session (weekly schedule says OPD, not SURGERY/OFF)
7.  the doctor is not on leave for that session
8.  the doctor covers the appointment's speciality
9.  no two active appointments overlap for the same doctor
10. one active booking per speciality per patient
11. a follow-up is with the doctor who saw the patient last in that
    speciality (and the patient has been seen there at all)
12. the end time matches the visit type's duration
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app import config

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ACTIVE = ("booked", "proposed")


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _sessions() -> list[tuple[str, int, int]]:
    return [
        (name, _minutes(spec["start"]), _minutes(spec["end"]))
        for name, spec in config.SESSIONS.items()
    ]


def _session_covering(start: int, end: int) -> str | None:
    for name, s_start, s_end in _sessions():
        if s_start is not None and s_end is not None and start >= s_start and end <= s_end:
            return name
    return None


def _load(db: Path, table: str, columns: str = "*") -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT {columns} FROM {table}")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def audit(db_path: Path | str, now: datetime) -> list[str]:
    """Every rule break in this database, as plain sentences. [] = clean."""
    db = Path(db_path)
    if not db.exists():
        return [f"database missing: {db}"]

    appts = [a for a in _load(db, "appointments") if a.get("status") in ACTIVE]
    doctors = {d["doctor_id"]: d for d in _load(db, "doctors")}
    schedule = _load(db, "weekly_schedule")
    holidays = {h["date"] for h in _load(db, "holidays")}
    leave = _load(db, "doctor_leave")
    completed = [a for a in _load(db, "appointments") if a.get("status") == "completed"]

    opd: dict[tuple[str, str, str], str] = {
        (s["doctor_id"], s["day"], s["session"]): s["status"] for s in schedule
    }
    leave_keys = {
        (l["doctor_id"], l["date"], l["session"]) for l in leave
    }

    today = now.date()
    window_end = today + timedelta(days=config.BOOKING_WINDOW_DAYS)
    breaks: list[str] = []

    def tag(a: dict) -> str:
        return (f"{a.get('patient_id')} {a.get('date')} {a.get('start_time')} "
                f"{a.get('doctor_id')} {a.get('speciality')} "
                f"(appointment #{a.get('id')})")

    for a in appts:
        start = _minutes(a.get("start_time", ""))
        end = _minutes(a.get("end_time", ""))
        day = None
        try:
            day = datetime.fromisoformat(str(a.get("date", ""))).date()
        except ValueError:
            breaks.append(f"{tag(a)}: date is not YYYY-MM-DD")
            continue
        if start is None or end is None:
            breaks.append(f"{tag(a)}: start/end time is not HH:MM")
            continue

        # 1. ten-minute marks
        if start % config.SLOT_STEP_MINUTES:
            breaks.append(f"{tag(a)}: start {a['start_time']} is not on a "
                          f"{config.SLOT_STEP_MINUTES}-minute mark")
        # 2. Sunday
        if day.weekday() == config.CLOSED_WEEKDAY:
            breaks.append(f"{tag(a)}: booked on a Sunday")
        # 3. holiday
        if day.isoformat() in holidays:
            breaks.append(f"{tag(a)}: booked on a clinic holiday")
        # 4. 30-day window
        if day < today or day > window_end:
            breaks.append(f"{tag(a)}: outside the 30-day window "
                          f"({today} .. {window_end})")
        # 5. inside a session, ends by 13:00 / 18:00
        session = _session_covering(start, end)
        if session is None:
            breaks.append(f"{tag(a)}: {a['start_time']}-{a['end_time']} is not "
                          f"inside a clinic session (must end by 1 PM or 6 PM)")

        doctor = doctors.get(a.get("doctor_id"))
        if doctor is None:
            breaks.append(f"{tag(a)}: unknown doctor")
            continue

        # 6. the doctor's OPD session that day
        if session is not None:
            day_name = DAY_NAMES[day.weekday()]
            status = opd.get((doctor["doctor_id"], day_name, session))
            if status is None:
                breaks.append(f"{tag(a)}: {doctor['name']} has no schedule row "
                              f"for {day_name} {session}")
            elif status != "OPD":
                breaks.append(f"{tag(a)}: {doctor['name']} is not in OPD on "
                              f"{day_name} {session} (status {status})")
            # 7. leave
            if (doctor["doctor_id"], day.isoformat(), session) in leave_keys or (
                doctor["doctor_id"], day.isoformat(), "FULL"
            ) in leave_keys:
                breaks.append(f"{tag(a)}: {doctor['name']} is on leave on "
                              f"{day.isoformat()} ({session})")

        # 8. the doctor covers the speciality
        covered = {s.strip() for s in str(doctor.get("specialities", "")).split(";")}
        if a.get("speciality") not in covered:
            breaks.append(f"{tag(a)}: {doctor['name']} does not cover "
                          f"{a.get('speciality')}")

        # 12. duration
        want = config.DURATION_MINUTES.get(str(a.get("visit_type", "")))
        if want is not None and end - start != want:
            breaks.append(f"{tag(a)}: runs {end - start} minutes, "
                          f"{a.get('visit_type')} should be {want}")

        # 11. follow-up rules
        if a.get("visit_type") == "follow_up":
            prior = [
                c for c in completed
                if c.get("patient_id") == a.get("patient_id")
                and c.get("speciality") == a.get("speciality")
                and str(c.get("date", "")) < str(a.get("date", ""))
            ]
            if not prior:
                breaks.append(f"{tag(a)}: follow-up with no earlier visit in "
                              f"{a.get('speciality')}")
            else:
                last = max(prior, key=lambda c: (str(c.get("date", "")),
                                                 str(c.get("start_time", ""))))
                if last.get("doctor_id") != a.get("doctor_id"):
                    breaks.append(
                        f"{tag(a)}: follow-up with {doctor['name']}, but the "
                        f"patient was last seen in {a.get('speciality')} by "
                        f"{last.get('doctor_id')} on {last.get('date')}"
                    )

    # 9. overlaps (same doctor, same date)
    by_doctor: dict[tuple[str, str], list[dict]] = {}
    for a in appts:
        by_doctor.setdefault((a.get("doctor_id", ""), a.get("date", "")), []).append(a)
    for key, group in by_doctor.items():
        for i, left in enumerate(group):
            ls, le = _minutes(left.get("start_time", "")), _minutes(left.get("end_time", ""))
            if ls is None or le is None:
                continue
            for right in group[i + 1:]:
                rs, re = _minutes(right.get("start_time", "")), _minutes(right.get("end_time", ""))
                if rs is None or re is None:
                    continue
                if ls < re and rs < le:
                    breaks.append(
                        f"{tag(left)}: overlaps #{right.get('id')} "
                        f"({right.get('start_time')}-{right.get('end_time')}) "
                        f"for {key[0]} on {key[1]}"
                    )

    # 10. one active booking per speciality per patient
    seen: dict[tuple[str, str], int] = {}
    for a in appts:
        key = (a.get("patient_id", ""), a.get("speciality", ""))
        seen[key] = seen.get(key, 0) + 1
    for (patient_id, speciality), count in sorted(seen.items()):
        if count > 1:
            breaks.append(f"{patient_id}: {count} active bookings in {speciality}")

    return sorted(breaks)


def audit_selftest(db_path: Path | str, now: datetime) -> tuple[bool, str]:
    """Prove the audit is not a no-op (handout section 10).

    Books Dr. Meera Iyer on a Thursday by hand — she is not at the clinic on
    Thursdays — and checks that `audit` reports it. The row is removed
    afterwards, so the database the scenarios use is untouched.
    """
    db = Path(db_path)
    day = now.date()
    while day.weekday() != 3:  # Thursday
        day += timedelta(days=1)
    if day <= now.date() or day > now.date() + timedelta(days=config.BOOKING_WINDOW_DAYS):
        day = now.date() + timedelta(days=(3 - now.date().weekday()) % 7 or 7)

    patient = None
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        doctors = [dict(r) for r in con.execute("SELECT * FROM doctors")]
        iyer = next((d for d in doctors if "Iyer" in d.get("name", "")), None)
        if iyer is None:
            return False, "Dr. Iyer not found in the seed"
        speciality = str(iyer.get("specialities", "")).split(";")[0].strip()
        for row in con.execute(
            "SELECT patient_id FROM appointments WHERE status IN ('booked','proposed') "
            "AND speciality = ?", (speciality,)
        ):
            patient = row["patient_id"]
        if patient is None:
            row = con.execute(
                "SELECT patient_id FROM patients ORDER BY patient_id LIMIT 1"
            ).fetchone()
            patient = row["patient_id"] if row else None
        if patient is None:
            return False, "no patient in the seed"

        cur = con.execute(
            "INSERT INTO appointments (patient_id, doctor_id, speciality, visit_type, "
            "date, start_time, end_time, status, reason, urgent, booked_via) "
            "VALUES (?, ?, ?, 'new', ?, '10:00', '10:20', 'booked', "
            "'audit self-test', 0, 'Website')",
            (patient, iyer["doctor_id"], speciality, day.isoformat()),
        )
        con.commit()
        new_id = cur.lastrowid
        found = [b for b in audit(db, now) if f"#{new_id}" in b]
    finally:
        con.execute("DELETE FROM appointments WHERE reason = 'audit self-test'")
        con.commit()
        con.close()

    if found:
        return True, found[0]
    return False, f"the hand-made {day.isoformat()} booking for Dr. Iyer was not reported"
