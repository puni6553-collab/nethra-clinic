"""Verify the rules engine against the data pack's own rule_tests table.

This is a development harness, not part of the app. It replays the instructor's
test cases with the fixed clock (2 Nov 2026, 10 AM) that they were written for,
and reports any mismatch.

Run:  venv\\Scripts\\python.exe tests\\verify_rules.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Fixed clock + seeded appointments, exactly as the test cases expect.
os.environ["APP_NOW"] = "2026-11-02T10:00:00"
os.environ["SEED_APPOINTMENTS"] = "1"

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# Fresh database for every run.
db_path = BACKEND / "netracare.db"
if db_path.exists():
    db_path.unlink()

from app.database import Session, engine, init_db  # noqa: E402
from app import config, seed  # noqa: E402
from app.rules import Refusal, validate_booking, validate_cancel, validate_reschedule  # noqa: E402
from app.models import Appointment  # noqa: E402

# Data pack 1 lives in backend/data/ (config.SOURCE_SQLITE prefers it), so the
# suite no longer depends on a file sitting in the Downloads folder.
SOURCE = config.SOURCE_SQLITE[0]


def read_cases() -> tuple[list[dict], dict[str, int]]:
    """The test file names appointments A001..A112; the database assigns its
    own integer IDs in seed order. Build the bridge between the two."""
    con = sqlite3.connect(SOURCE)

    seed_rows = con.execute('SELECT * FROM appointments_seed').fetchall()
    seed_header = [str(c) for c in seed_rows[0]]
    appt_col = seed_header.index("appointment_id")
    # Row N in the seed file becomes row N in the table (both start at 1).
    id_map = {str(r[appt_col]): i + 1 for i, r in enumerate(seed_rows[1:])}

    rows = con.execute('SELECT * FROM "rule_tests"').fetchall()
    con.close()
    header = [str(c) for c in rows[0]]
    cases = [{header[i]: row[i] for i in range(len(header))} for row in rows[1:]]
    return cases, id_map


def run() -> None:
    init_db()
    with Session(engine) as db:
        seed.load_data(db)

    cases, id_map = read_cases()
    passed = failed = skipped = 0
    failures: list[str] = []

    with Session(engine) as db:
        for case in cases:
            test_id = case["test_id"]
            action = case["action"]
            expected = case["expected"]
            want_code = case["reason_code"] or None

            got = None
            code = None
            try:
                if action == "book":
                    validate_booking(
                        db,
                        patient_id=case["acting_patient_id"],
                        doctor_id=case["doctor_id"],
                        speciality=case["speciality"],
                        visit_type=case["visit_type"],
                        date=case["date"],
                        start_time=case["start_time"],
                    )
                elif action == "reschedule":
                    row_id = id_map.get(str(case["appointment_id"]))
                    appt = db.get(Appointment, row_id) if row_id else None
                    if appt is None:
                        raise Refusal("NOT_FOUND", "missing", 404)
                    validate_reschedule(
                        db,
                        appt,
                        doctor_id=case["doctor_id"],
                        date=case["date"],
                        start_time=case["start_time"],
                        acting_patient_id=case["acting_patient_id"] or None,
                    )
                else:  # cancel
                    row_id = id_map.get(str(case["appointment_id"]))
                    appt = db.get(Appointment, row_id) if row_id else None
                    if appt is None:
                        raise Refusal("NOT_FOUND", "missing", 404)
                    acting = case["acting_patient_id"] or None
                    validate_cancel(db, appt, acting_patient_id=acting)
                got = "allowed"
            except Refusal as err:
                got = "refused"
                code = err.code

            # "to_staff" is a refusal the patient is told to hand to Staff.
            # The rules engine still raises INSIDE_2_HOURS for it; actions.py
            # is what turns that into a queued approval request.
            normalised_expected = "refused" if expected == "to_staff" else expected
            ok = got == normalised_expected and (want_code is None or code == want_code)

            if ok:
                passed += 1
            else:
                failed += 1
                failures.append(
                    f"{test_id} [{action}] expected {expected}"
                    + (f"/{want_code}" if want_code else "")
                    + f" -> got {got}" + (f"/{code}" if code else "")
                    + f"   ({case['note']})"
                )

            # Keep state moving like a real session: commit bookings.
            db.commit()

    print(f"\n{'=' * 70}")
    print(f"rule checks: {passed} passed, {failed} failed, {skipped} skipped")
    if failures:
        print("=" * 70)
        for line in failures:
            print("  FAIL", line)
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    run()
