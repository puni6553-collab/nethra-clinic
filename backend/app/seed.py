"""Data pack loader (requirement R1).

Reads the instructor's data pack into SQLite at startup. The pack ships as a
SQLite database that is *named* `weekly_schedule.csv`, and every table inside
it uses generic column names (`field1`, `field2`, ...) with the real header
sitting in row 1. So: detect the format, lift the header row, map the columns
by position, and insert typed rows.

Appointments start EMPTY — the brief is explicit that bookings are made
through the app. Set SEED_APPOINTMENTS=1 to import the 18 sample rows instead
(handy for demos and follow-up testing).
"""
from __future__ import annotations

import csv
import os
import sqlite3

from sqlmodel import Session, select

from . import config
from .models import Appointment, Doctor, Holiday, Patient, WeeklySchedule

# Source table -> our table's column order (header row supplies the names).
LAYOUTS = {
    "doctors": ["doctor_id", "name", "specialities"],
    "holidays": ["date", "name"],
    "patients": ["patient_id", "name", "mobile", "year_of_birth"],
    "weekly schedule": ["doctor_id", "day", "session", "status"],
    "appointments_seed": [
        "appointment_id", "patient_id", "doctor_id", "speciality",
        "visit_type", "date", "start_time", "end_time", "status",
    ],
}


def find_source() -> str | None:
    """Locate the data pack: project data/ folder first, then Downloads."""
    for path in config.SOURCE_SQLITE:
        if os.path.exists(path) and _is_sqlite(path):
            return path
    for folder in config.SOURCE_CSV_DIRS:
        candidate = os.path.join(folder, "weekly schedule.csv")
        if os.path.exists(candidate) and not _is_sqlite(candidate):
            return candidate
    return None


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


def _read_table(path: str, table: str) -> list[dict]:
    """Return rows as dicts, from either the SQLite pack or a plain CSV."""
    if _is_sqlite(path):
        con = sqlite3.connect(path)
        try:
            rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
    else:
        # Plain CSV: first line is the header.
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))

    if not rows:
        return []

    columns = LAYOUTS[table]
    # The pack stores the header in row 1 under generic column names.
    first = [str(c).strip().lower() for c in rows[0]]
    if first[: len(columns)] == [c.lower() for c in columns]:
        data = rows[1:]
    elif first[:1] in (["doctor_id"], ["date"], ["patient_id"], ["appointment_id"]):
        data = rows  # already a real header row (plain CSV copy)
    else:
        data = rows  # no recognisable header: treat everything as data

    out = []
    for row in data:
        if not any(str(cell).strip() for cell in row):
            continue
        out.append({columns[i]: str(row[i]).strip() if i < len(row) else "" for i in range(len(columns))})
    return out


def load_data(db: Session) -> None:
    """Idempotent: only fills tables that are empty."""
    source = find_source()
    if source is None:
        print("[seed] WARNING: data pack not found. Checked:")
        for p in config.SOURCE_SQLITE:
            print(f"[seed]   - {p}")
        return

    print(f"[seed] reading data pack from {source}")
    _load_doctors(db, source)
    _load_schedule(db, source)
    _load_holidays(db, source)
    _load_patients(db, source)
    _load_appointments(db, source)
    # Iteration 2: pack 2 adds past visits + profiles on top of pack 1.
    _load_pack2(db)


def _empty(db: Session, model) -> bool:
    return db.exec(select(model)).first() is None


def _load_doctors(db: Session, source: str) -> None:
    if not _empty(db, Doctor):
        return
    for row in _read_table(source, "doctors"):
        if row["doctor_id"]:
            db.add(Doctor(doctor_id=row["doctor_id"], name=row["name"], specialities=row["specialities"]))
    db.commit()


def _load_schedule(db: Session, source: str) -> None:
    if not _empty(db, WeeklySchedule):
        return
    for row in _read_table(source, "weekly schedule"):
        if row["doctor_id"] and row["day"]:
            db.add(WeeklySchedule(
                doctor_id=row["doctor_id"], day=row["day"],
                session=row["session"], status=row["status"],
            ))
    db.commit()


def _load_holidays(db: Session, source: str) -> None:
    if not _empty(db, Holiday):
        return
    for row in _read_table(source, "holidays"):
        if row["date"]:
            db.add(Holiday(date=row["date"], name=row["name"]))
    db.commit()


def _load_patients(db: Session, source: str) -> None:
    if not _empty(db, Patient):
        return
    for row in _read_table(source, "patients"):
        if not row["patient_id"]:
            continue
        year = row.get("year_of_birth", "")
        db.add(Patient(
            patient_id=row["patient_id"],
            name=row["name"],
            mobile=row["mobile"],
            year_of_birth=int(year) if str(year).isdigit() else None,
        ))
    db.commit()


def _load_appointments(db: Session, source: str) -> None:
    if not config.SEED_APPOINTMENTS:
        print("[seed] appointments: starting empty (set SEED_APPOINTMENTS=1 to import samples)")
        return
    if not _empty(db, Appointment):
        return
    for row in _read_table(source, "appointments_seed"):
        if not row["patient_id"]:
            continue
        db.add(Appointment(
            patient_id=row["patient_id"],
            doctor_id=row["doctor_id"],
            speciality=row["speciality"],
            visit_type=row["visit_type"],
            date=row["date"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            status=row["status"],
        ))
    db.commit()
    print("[seed] appointments: imported seed rows")


# --- data pack 2 (iteration 2) ---------------------------------------------

def _pack2_csv(name: str) -> list[dict] | None:
    """Read a pack-2 CSV as dicts, or None if the pack folder is missing."""
    path = os.path.join(config.PACK2_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _load_pack2(db: Session) -> None:
    """Load past_visits.csv and patient_profiles.csv (idempotent)."""
    rows = _pack2_csv("patient_profiles.csv")
    if rows:
        n = 0
        for row in rows:
            pid = (row.get("patient_id") or "").strip()
            patient = db.get(Patient, pid) if pid else None
            if patient is None:
                continue
            patient.general_health = (row.get("general_health") or "").strip() or None
            patient.allergies = (row.get("allergies") or "").strip() or None
            db.add(patient)
            n += 1
        db.commit()
        print(f"[seed] pack2: profile fields on {n} patients")

    visits = _pack2_csv("past_visits.csv")
    if not visits:
        return
    # Already loaded? V001 (P01, 2025-11-17) is distinctive to this pack.
    already = db.exec(
        select(Appointment).where(
            Appointment.patient_id == "P01",
            Appointment.date == "2025-11-17",
            Appointment.status == "completed",
        )
    ).first()
    if already is not None:
        print("[seed] pack2: past visits already loaded")
        return

    for row in visits:
        note = (row.get("doctor_note") or "").strip()
        reason = (row.get("reason") or "").strip()
        if not row.get("patient_id"):
            continue
        db.add(Appointment(
            patient_id=row["patient_id"].strip(),
            doctor_id=row["doctor_id"].strip(),
            speciality=row["speciality"].strip(),
            visit_type=row["visit_type"].strip(),
            date=row["date"].strip(),
            start_time=row["start_time"].strip(),
            end_time=row["end_time"].strip(),
            status="completed",
            reason=reason or None,
            doctor_note=note or None,
        ))
    db.commit()
    print(f"[seed] pack2: imported {len(visits)} past visits")
