"""R7 checks: doctor leave, the rebooking proposals and the Staff list.

Two parts, both on the handout's fixed clock (2 Nov 2026, 10 AM):

  1. `leave_scenarios.csv` (the data pack's own five cases, L1-L4): mark
     the leave the row asks for and compare the proposal your code makes
     with the one the pack expects, including the tie-breaks.
  2. The handout's own R7 check (section 05): book three appointments with
     Dr. Rao on an open Monday, mark it FULL leave, and check that all
     three are cancelled, that the proposals follow the rebooking rules,
     that the slot is held while they wait, that DOCTOR_ON_LEAVE stops
     anyone booking him that day, that Staff Approve books each one (and
     the patient sees "Moved because Dr. ... is on leave"), and that Staff
     Reject frees the slot again.

The suite starts its OWN server on port 8010 with a throwaway database and
APP_NOW fixed, so it never touches the app you are running on :8000 and can
be re-run for ever. Bookings still call the red-flag check, so it SKIPs
cleanly when the LLM quota is gone.

Run:  venv\\Scripts\\python.exe tests\\check_leave.py   (no server needed)
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PACK4 = BACKEND.parent / "Iteration 4" / "Iteration4" / "data-pack-04"
PORT = 8010
BASE = f"http://127.0.0.1:{PORT}"
CLOCK = "2026-11-02T10:00:00"  # the pack's fixed clock (a Monday)

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


# --- HTTP ------------------------------------------------------------------

def call(method, path, body=None, token=None, base=BASE):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))


def skip(label, why):
    SKIP.append(label)
    print(f"  SKIP  {label}  -> {why}")


def msg(p):
    d = p.get("detail", p)
    return d.get("message", "") if isinstance(d, dict) else str(d)


def code(p):
    d = p.get("detail", p)
    return d.get("code", "") if isinstance(d, dict) else ""


def llm_down(body) -> bool:
    return code(body) == "REDFLAG_CHECK_FAILED"


# --- the throwaway server ---------------------------------------------------

def start_server():
    """uvicorn on :8010 with its own database and the pack's fixed clock."""
    db = Path(tempfile.gettempdir()) / "netracare_leave_check.db"
    if db.exists():
        db.unlink()
    log = Path(tempfile.gettempdir()) / "netracare_leave_check.log"
    env = dict(os.environ)
    env.update({
        "NETRACARE_DB": str(db),
        "APP_NOW": CLOCK,
        "SEED_APPOINTMENTS": "1",   # the pack's rows: A101, A102, A109, A111
        "LLM_MODEL": env.get("LLM_MODEL") or "gemini-3.5-flash-lite",
    })
    out = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=str(BACKEND), env=env, stdout=out, stderr=subprocess.STDOUT,
    )
    for _ in range(80):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return proc, db, log
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    proc.kill()
    out.close()
    tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
    raise RuntimeError(f"the test server never came up on :{PORT}\n{tail}")


def stop_server(proc, db: Path) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        try:
            path.unlink()
        except OSError:
            pass


# --- shared helpers ---------------------------------------------------------

def slots_on(token, doctor_id: str, day: str, visit_type: str) -> list[str]:
    _, body = call(
        "GET", f"/api/slots?doctor_id={doctor_id}&date={day}&visit_type={visit_type}",
        token=token,
    )
    return body.get("slots") or []


def open_day(token, day: date) -> bool:
    _, body = call(
        "GET", f"/api/slots?doctor_id=D1&date={day.isoformat()}&visit_type=new",
        token=token,
    )
    return not body.get("notes")


def parse_expectation(text: str) -> tuple[str, str, str, str, str]:
    """'D1 Cataract new 2026-11-04 09:00' -> its five fields."""
    doctor, speciality, visit_type, day, start = text.split()
    return doctor, speciality, visit_type, day, start


def appointment_rows(stok) -> list[dict]:
    _, rows = call("GET", "/api/appointments", token=stok)
    return rows if isinstance(rows, list) else []


def active_specialities(rows: list[dict]) -> dict[str, set[str]]:
    """Which specialities each patient already holds an appointment in."""
    out: dict[str, set[str]] = {}
    for r in rows:
        if r.get("status") in ("booked", "proposed"):
            out.setdefault(r["patient_id"], set()).add(r.get("speciality"))
    return out


def earliest_proposal(
    token, *, covering: list[str], visit_type: str,
    after_date: str, after_time: str, original_doctor: str,
    stay_with_doctor: str | None = None,
    held_by_us: set[tuple[str, str, str]] | None = None,
) -> tuple[str, str, str] | None:
    """What the rebooking rules should pick: (date, time, doctor_id).

    Walks the public slots API the way the handout's rules describe, so the
    expectation comes from the clinic's own free-slot generator rather than
    from a copy of the rebooking code. `held_by_us` are the slots this
    leave's own proposals are holding: taken, but only by the answer being
    checked, so they count as free here.
    """
    if stay_with_doctor:
        order = [stay_with_doctor]  # a follow-up stays with the same doctor
    else:
        order = sorted(covering, key=lambda i: (i != original_doctor, i))
    held_by_us = held_by_us or set()

    def free_starts(doctor_id: str, day: str) -> list[str]:
        starts = list(slots_on(token, doctor_id, day, visit_type))
        starts += [t for (d, when, t) in held_by_us if d == doctor_id and when == day]
        return sorted(set(starts))

    day = date.fromisoformat(after_date)
    horizon = date.fromisoformat(CLOCK[:10]) + timedelta(days=30)
    best = None  # (date, time, index)
    while day <= horizon and best is None:
        for index, doctor_id in enumerate(order):
            for start in free_starts(doctor_id, day.isoformat()):
                if (day.isoformat(), start) < (after_date, after_time):
                    continue
                key = (day.isoformat(), start, index)
                if best is None or key < best:
                    best = key
        if best is None:
            day += timedelta(days=1)
    return None if best is None else (best[0], best[1], order[best[2]])


# --- part 1: data pack 4, leave_scenarios.csv ------------------------------

def run_leave_scenarios(stok, dtok_by_doctor: dict[str, str]) -> None:
    path = PACK4 / "leave_scenarios.csv"
    if not path.exists():
        skip("leave_scenarios.csv", f"not found at {path}")
        return

    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    print("\n--- data pack 4: leave_scenarios.csv ---")
    done: set[str] = set()
    for row in rows:
        leave_id = row["leave_id"]
        doctor_id = row["doctor_id"]
        token = dtok_by_doctor.get(doctor_id)
        if not token:
            skip(f"{leave_id} {row['affected_appointment']}", f"cannot log in as {doctor_id}")
            continue

        affected_id = None
        if leave_id not in done:
            # The one setup step in the pack (L4): Staff book the appointment
            # the leave is then marked against.
            setup = (row.get("setup") or "").strip()
            if setup:
                status, body = call("POST", "/api/appointments", {
                    "patient_id": "P20", "doctor_id": "D3", "speciality": "Retina",
                    "visit_type": "new", "date": "2026-11-04", "start_time": "09:00",
                    "reason": "Optician asked for a retina check",
                }, token=stok)
                if llm_down(body):
                    skip(f"{leave_id} setup", "red-flag check unavailable (LLM quota)")
                    return
                if status != 201:
                    skip(f"{leave_id} setup", f"booking failed: {msg(body)}")
                    return
                affected_id = body["id"]

            status, leave = call("POST", "/api/doctor/leave",
                                 {"date": row["leave_date"], "session": row["leave_session"]},
                                 token=token)
            if status != 201:
                skip(f"{leave_id} {row['leave_date']}", f"marking leave failed: {msg(leave)}")
                return
            done.add(leave_id)

        # Which appointment did this row mean?
        if affected_id is None:
            if row["affected_appointment"].startswith("("):
                skip(f"{leave_id} {row['affected_appointment']}", "no booking id")
                continue
            want = parse_expectation(row["original"])  # doctor speciality vt date start
            match = [
                a for a in appointment_rows(stok)
                if a["doctor_id"] == want[0] and a["speciality"] == want[1]
                and a["visit_type"] == want[2] and a["date"] == want[3]
                and a["start_time"] == want[4] and a["patient_id"] == row["patient_id"]
            ]
            if not match:
                skip(f"{leave_id} {row['affected_appointment']}", "original appointment not found")
                continue
            affected_id = match[0]["id"]

        _, rebookings = call("GET", "/api/staff/rebookings", token=stok)
        entry = next(
            (r for r in (rebookings if isinstance(rebookings, list) else [])
             if (r.get("original") or {}).get("id") == affected_id),
            None,
        )
        label = f"{leave_id} {row['affected_appointment']}"
        if entry is None:
            check(label, False, "no rebooking row was created")
            continue

        original = entry.get("original") or {}
        check(f"{label}: the original is cancelled",
              original.get("status") == "cancelled", original.get("status", "?"))

        expected = parse_expectation(row["expected_proposal"])
        proposed = entry.get("proposed")
        if expected[0] == "(none)":
            check(f"{label}: no slot, call the patient",
                  entry.get("status") == "no_slot", entry.get("status", "?"))
            continue
        if not proposed:
            check(f"{label}: proposal {row['expected_proposal']}",
                  False, f"status={entry.get('status')} message={entry.get('message')}")
            continue
        got = (proposed["doctor_id"], proposed["speciality"], proposed["visit_type"],
               proposed["date"], proposed["start_time"])
        check(f"{label}: proposes {row['expected_proposal']}",
              got == expected, f"got {' '.join(got)} — {row['why']}")


# --- part 2: the handout's R7 check ----------------------------------------

def run_handout_check(stok, dtok: str, by_id: dict[str, dict]) -> None:
    _, doctors = call("GET", "/api/doctors")
    covering_general = [d["doctor_id"] for d in doctors if "General" in d["specialities"]]
    covering_cataract = [d["doctor_id"] for d in doctors if "Cataract" in d["specialities"]]

    held = active_specialities(appointment_rows(stok))

    def free_for(pid: str | None, speciality: str) -> bool:
        return bool(pid) and speciality not in held.get(pid, set())

    def pick(*prefer: str, speciality: str, exclude: tuple = ()) -> str | None:
        for pid in prefer:
            if pid in by_id and pid not in exclude and free_for(pid, speciality):
                return pid
        for pid in sorted(by_id):
            if pid not in exclude and free_for(pid, speciality):
                return pid
        return None

    # A follow-up needs a COMPLETED Cataract visit with Dr. Rao (D1).
    fu_pid = next(
        (pid for pid in ("P01", "P18", "P09", "P06")
         if pid in by_id and free_for(pid, "Cataract")),
        None,
    )
    cataract_pid = pick("P02", "P04", "P05", speciality="Cataract",
                        exclude=(fu_pid,) if fu_pid else ())
    general_pid = pick("P07", "P08", "P10", "P12", speciality="General")

    monday = find_day(stok, weekday=0, skip_dates=set())
    if not monday:
        skip("handout R7 check", "no open Monday in the booking window")
        return
    tuesday = (date.fromisoformat(monday) + timedelta(days=1)).isoformat()

    if not (fu_pid and cataract_pid and general_pid):
        skip("handout R7 check", "not enough patients free in the right specialities")
        return
    print(f"\n--- handout R7 check: leave on Monday {monday} ---")
    print(f"  patients: Cataract {cataract_pid}, General {general_pid}, follow-up {fu_pid}")

    # The pack books 10:00, 10:20 and 11:00; take those when they are free
    # and fall back to the next free start so the run never depends on what
    # an earlier run left behind.
    def minutes(t: str) -> int:
        h, m = map(int, t.split(":"))
        return h * 60 + m

    picked: list[tuple[str, int]] = []

    def pick_slot(pool: list[str], preferred: str, duration: int) -> str | None:
        for start in [preferred] + sorted(pool):
            if start not in pool:
                continue
            if any(minutes(start) < minutes(p[0]) + p[1] and minutes(p[0]) < minutes(start) + duration
                   for p in picked):
                continue
            picked.append((start, duration))
            return start
        return None

    new_pool = slots_on(stok, "D1", monday, "new")
    fu_pool = slots_on(stok, "D1", monday, "follow_up")
    t_cataract = pick_slot(new_pool, "10:00", 20)
    t_general = pick_slot(new_pool, "10:20", 20)
    t_followup = pick_slot(fu_pool, "11:00", 10)
    if not (t_cataract and t_general and t_followup):
        skip("handout R7 check", f"Dr. Rao has no room on {monday}")
        return

    def book(pid, speciality, visit_type, day, start, reason):
        return call("POST", "/api/appointments", {
            "patient_id": pid, "doctor_id": "D1", "speciality": speciality,
            "visit_type": visit_type, "date": day, "start_time": start,
            "reason": reason,
        }, token=stok)

    made = []
    for label, pid, spec, vt, start, reason in [
        ("Cataract new", cataract_pid, "Cataract", "new", t_cataract, "Routine eye check"),
        ("General new", general_pid, "General", "new", t_general, "Vision feels strained"),
        ("Cataract follow-up", fu_pid, "Cataract", "follow_up", t_followup, "Review after surgery"),
    ]:
        status, body = book(pid, spec, vt, monday, start, reason)
        if llm_down(body):
            skip("handout R7 check", "red-flag check unavailable (LLM quota)")
            return
        if status != 201:
            skip("handout R7 check", f"could not book {label}: {msg(body)}")
            return
        made.append(body)
    print(f"  booked {sorted(b['id'] for b in made)} at "
          f"{t_cataract}, {t_general}, {t_followup}")

    # --- the doctor marks leave -------------------------------------------
    print("\n--- doctor marks leave ---")
    status, leave = call("POST", "/api/doctor/leave",
                         {"date": monday, "session": "FULL"}, token=dtok)
    check("Dr. Rao marks FULL leave for the Monday",
          status == 201 and leave.get("rebooked") == 3,
          f"{status} rebooked={leave.get('rebooked')} {msg(leave)}")

    status, body = call("POST", "/api/doctor/leave",
                        {"date": monday, "session": "AM"}, token=stok)
    check("Staff cannot mark leave", status == 403, f"{status} {code(body)}")

    # --- three cancellations, three proposals ------------------------------
    print("\n--- the rebookings ---")
    _, rows = call("GET", "/api/staff/rebookings", token=stok)
    rows = [r for r in rows if isinstance(rows, list) and r["leave_date"] == monday]
    check("Staff see three rebookings", len(rows) == 3, f"{len(rows)} rows")

    by_appt = {r["id"]: r for r in appointment_rows(stok)}
    cancelled = [b["id"] for b in made if by_appt.get(b["id"], {}).get("status") == "cancelled"]
    check("all three originals are cancelled", len(cancelled) == 3, str(sorted(cancelled)))

    wanted = {b["id"]: (b["speciality"], b["visit_type"], b["start_time"]) for b in made}
    held_by_us = {
        (r["proposed"]["doctor_id"], r["proposed"]["date"], r["proposed"]["start_time"])
        for r in rows if r.get("proposed")
    }
    for row in rows:
        orig = row.get("original") or {}
        spec, vt, start = wanted.get(orig.get("id"), ("", "", ""))
        prop = row.get("proposed") or {}
        label = f"{spec} {vt} {start}"
        covering = covering_cataract if spec == "Cataract" else covering_general
        expect = earliest_proposal(
            stok, covering=covering, visit_type=vt,
            after_date=orig.get("date", monday), after_time=start,
            original_doctor=orig.get("doctor_id", "D1"),
            stay_with_doctor=orig.get("doctor_id") if vt == "follow_up" else None,
            held_by_us=held_by_us,
        )
        if expect is None:
            check(f"proposal for {label}", False, "nothing free to compare against")
            continue
        got = (prop.get("date"), prop.get("start_time"), prop.get("doctor_id"))
        check(f"proposal for {label} follows the rebooking rules",
              got == expect, f"got {got}, expected {expect}")

        free = slots_on(stok, prop.get("doctor_id", ""), prop.get("date", ""), vt)
        check(f"  the proposed slot for {label} is held",
              prop.get("start_time") not in free, f"{prop.get('start_time')} free={free[:4]}")

        note = prop.get("move_note") or ""
        check(f"  the patient is told why {label} moved",
              "on leave" in note, note or "(no note)")

    # --- the rules hold while leave is marked ------------------------------
    print("\n--- the rules hold while leave is marked ---")
    status, body = book(cataract_pid, "Cataract", "new", monday, "15:00", "Routine eye check")
    check("booking Dr. Rao on the leave day is refused",
          status >= 400, f"{status} {code(body)} {msg(body)[:70]}")
    check("Dr. Rao has no free slots that day",
          slots_on(stok, "D1", monday, "new") == [], "")

    # --- Staff approve -----------------------------------------------------
    print("\n--- Staff approve ---")
    approved = 0
    for row in rows:
        if not row.get("proposed"):
            continue
        status, body = call("POST", f"/api/staff/rebookings/{row['id']}/approve",
                            {}, token=stok)
        if status == 200 and body.get("status") == "approved":
            approved += 1
        else:
            check(f"approve rebooking {row['id']}", False,
                  f"{status} {code(body)} {msg(body)[:80]}")
    check("Staff approved every proposal", approved == 3, f"{approved}/3")

    for row in rows:
        prop = row.get("proposed") or {}
        if not prop:
            continue
        now = {a["id"]: a for a in appointment_rows(stok)}.get(prop["id"])
        check(f"the rebooking is booked for {prop.get('patient_name')}",
              bool(now) and now["status"] == "booked",
              f"{now and now['status']} {now and now['date']} {now and now['start_time']}")
        note = (now or {}).get("move_note") or ""
        check(f"  and still says why it moved", "on leave" in note, note or "(no note)")

    # --- Staff reject frees the slot --------------------------------------
    print("\n--- Staff reject frees the slot ---")
    held = active_specialities(appointment_rows(stok))
    reject_pid = next(
        (pid for pid in sorted(by_id)
         if free_for(pid, "General")
         and pid not in {cataract_pid, general_pid, fu_pid}),
        None,
    )
    if not reject_pid:
        skip("reject frees the slot", "no spare patient left")
        return

    picked.clear()
    pool = slots_on(stok, "D1", tuesday, "new")
    start = pick_slot(pool, "09:00", 20)
    if not start:
        skip("reject frees the slot", f"Dr. Rao has no free slot on {tuesday}")
        return
    status, body = book(reject_pid, "General", "new", tuesday, start, "Screen strain")
    if llm_down(body):
        skip("reject frees the slot", "red-flag check unavailable (LLM quota)")
        return
    if status != 201:
        skip("reject frees the slot", f"booking failed: {msg(body)}")
        return
    spare = body

    status, leave2 = call("POST", "/api/doctor/leave",
                          {"date": tuesday, "session": "AM"}, token=dtok)
    if status != 201:
        skip("reject frees the slot", f"second leave failed: {msg(leave2)}")
        return

    _, pending = call("GET", "/api/staff/rebookings", token=stok)
    target = next((r for r in pending
                   if (r.get("original") or {}).get("id") == spare["id"]), None)
    if not target or not target.get("proposed"):
        skip("reject frees the slot", "no proposal for the spare booking")
        return

    held_slot = slots_on(stok, target["proposed"]["doctor_id"],
                         target["proposed"]["date"], "new")
    check("the spare proposal holds its slot",
          target["proposed"]["start_time"] not in held_slot, str(held_slot[:4]))

    status, body = call("POST", f"/api/staff/rebookings/{target['id']}/reject",
                        {}, token=stok)
    check("Staff Reject clears the rebooking",
          status == 200 and body.get("status") == "rejected",
          f"{status} {body.get('status')}")

    freed = slots_on(stok, target["proposed"]["doctor_id"],
                     target["proposed"]["date"], "new")
    check("Reject frees the slot again",
          target["proposed"]["start_time"] in freed, str(freed[:4]))

    rows_now = {a["id"]: a for a in appointment_rows(stok)}
    prop_row = rows_now.get(target["proposed"]["id"])
    orig_row = rows_now.get(spare["id"])
    check("after Reject the patient has no active booking",
          bool(prop_row) and prop_row["status"] == "cancelled"
          and bool(orig_row) and orig_row["status"] == "cancelled",
          f"prop={prop_row and prop_row['status']} orig={orig_row and orig_row['status']}")


def find_day(token, *, weekday: int, skip_dates: set[str], days=30) -> str | None:
    """The first matching day in the booking window the clinic is open on."""
    _, health = call("GET", "/api/health")
    today = date.fromisoformat(str(health.get("now", CLOCK))[:10])
    for i in range(1, days):
        day = today + timedelta(days=i)
        if day.weekday() != weekday:
            continue
        if day.isoformat() in skip_dates:
            continue
        if (day + timedelta(days=1)).isoformat() in skip_dates:
            continue  # the reject day must be clear too
        if open_day(token, day) and open_day(token, day + timedelta(days=1)):
            return day.isoformat()
    return None


# --- the run ----------------------------------------------------------------

def main() -> int:
    print(f"starting an isolated server on :{PORT} (clock {CLOCK})…")
    try:
        proc, db, log = start_server()
    except RuntimeError as exc:
        print(exc)
        return 1

    try:
        _, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
        stok = staff.get("token")
        if not stok:
            print("staff login failed")
            return 1

        dtok_by_doctor: dict[str, str] = {}
        for doctor_id in ("D1", "D3"):
            _, sess = call("POST", "/api/login", {"role": "doctor", "doctor_id": doctor_id})
            if sess.get("token"):
                dtok_by_doctor[doctor_id] = sess["token"]

        _, patients = call("GET", "/api/patients", token=stok)
        by_id = {p["patient_id"]: p for p in patients}

        run_leave_scenarios(stok, dtok_by_doctor)
        if "D1" in dtok_by_doctor:
            run_handout_check(stok, dtok_by_doctor["D1"], by_id)
        else:
            skip("handout R7 check", "cannot log in as Dr. Rao")
    finally:
        stop_server(proc, db)

    print("\n" + "=" * 66)
    print(f"leave checks: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    for s in SKIP:
        print("   SKIP:", s)
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
