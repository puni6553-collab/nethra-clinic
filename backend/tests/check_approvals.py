"""Verify R1: reschedule, and the Staff approval queue for late changes.

Run:  venv\\Scripts\\python.exe tests\\check_approvals.py   (no server needed)

This suite owns its clock and its database, which is what makes the 2-hour
rule testable at all:
  * APP_NOW = 2026-11-02T10:00 is a Monday morning with the clinic open. On
    the real clock the "late" checks can only run during clinic hours inside
    a 2-hour window, and the run may well land on a closed Sunday.
  * NETRACARE_DB points at a throwaway file, so this suite never disturbs the
    website's data — it can run while the app is up on :8000.

Every check here mirrors a row of the brief's section 10 or R1 itself:
  * a reschedule is one action (new booking validated as if the old one were
    gone, then both rows move together);
  * a refused reschedule leaves the original exactly as it was;
  * a cancel or reschedule inside 2 hours returns to_staff, the appointment
    stays booked, and only Staff can approve or reject it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

# --- must be set before app.config is imported ------------------------------
os.environ["APP_NOW"] = "2026-11-02T10:00:00"  # Monday, clinic open
_TMP_DB = os.path.join(tempfile.gettempdir(), f"netracare_approvals_{os.getpid()}.db")
os.environ["NETRACARE_DB"] = _TMP_DB
for _stale in (_TMP_DB, f"{_TMP_DB}-wal", f"{_TMP_DB}-shm"):
    if os.path.exists(_stale):
        os.unlink(_stale)

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

_client: TestClient | None = None


def call(method, path, body=None, token=None):
    """Same (status, payload) shape as the other suites' HTTP helper."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = _client.request(method, path, json=body, headers=headers)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


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


def book(token, doctor_id, speciality, visit_type, day, start, reason):
    return call("POST", "/api/appointments", {
        "doctor_id": doctor_id, "speciality": speciality, "visit_type": visit_type,
        "date": day, "start_time": start, "reason": reason,
    }, token=token)


def row(token, appt_id):
    """The appointment as the given patient sees it in their list."""
    _, rows = call("GET", "/api/appointments", token=token)
    return next((r for r in rows if r["id"] == appt_id), None)


def free_slots(token, doctor_id, day, visit_type="new"):
    _, s = call("GET", f"/api/slots?doctor_id={doctor_id}&date={day}"
                       f"&visit_type={visit_type}", token=token)
    return s.get("slots") or []


def main() -> None:
    global _client

    # Fresh database: the app's lifespan creates tables and loads the pack.
    from app.main import app
    with TestClient(app) as client:
        _client = client
        run()


def run() -> None:
    from app import config

    today = config.now().date()
    day = (today + timedelta(days=1)).isoformat()  # a Tuesday, clinic open
    sunday = today + timedelta(days=(6 - today.weekday()) % 7 or 7)
    while sunday.weekday() != 6:
        sunday += timedelta(days=1)
    sunday = sunday.isoformat()

    _, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    STAFF = staff["token"]
    _, pats = call("GET", "/api/patients", token=STAFF)
    by_id = {p["patient_id"]: p for p in pats}

    def as_patient(pid):
        p = by_id[pid]
        _, s = call("POST", "/api/login",
                    {"role": "patient", "mobile": p["mobile"], "name": p["name"]})
        return s["token"]

    P1 = as_patient("P01")   # Asha Verma
    P2 = as_patient("P02")   # Rohan Gupta
    P3 = as_patient("P03")   # a third patient, for the "someone else" check
    print(f"\n  clock {config.now():%Y-%m-%d %H:%M} ({config.now():%A})  "
          f"db {os.path.basename(config.DB_PATH)}")
    print(f"  patients: P01 {by_id['P01']['name']}, P02 {by_id['P02']['name']}, "
          f"P03 {by_id['P03']['name']}")

    # Clear anything a previous run left behind (this file is fresh each run,
    # but the guards keep the suite honest if that ever changes).
    for tok in (P1, P2, P3):
        _, prior = call("GET", "/api/appointments", token=tok)
        for a in prior:
            if a.get("status") == "booked":
                call("POST", f"/api/appointments/{a['id']}/cancel", {}, token=STAFF)

    # ---------------------------------------------------------------------
    print("\n--- R1: reschedule is one action (old freed, new booked) ---")
    # ---------------------------------------------------------------------
    slots = free_slots(P1, "D1", day)
    if not slots:
        skip("reschedule moves the appointment", f"no Dr. Rao slot on {day}")
        appt_id = None
        start = None
    else:
        start = slots[0]
        st, appt = book(P1, "D1", "General", "new", day, start, "Vision getting blurry")
        if llm_down(appt):
            skip("reschedule moves the appointment", "red-flag check unavailable (LLM quota)")
            appt_id = None
        else:
            check("booked a movable appointment", st == 201, f"{st} {msg(appt)} id {appt.get('id')}")
            appt_id = appt.get("id")

    if appt_id:
        later = [s for s in free_slots(P1, "D1", day) if s > start]
        target = later[3] if len(later) > 3 else later[-1]
        st, moved = call("POST", f"/api/appointments/{appt_id}/reschedule",
                         {"date": day, "start_time": target}, token=P1)
        check("reschedule returns 200", st == 200, f"{st} {code(moved)} {msg(moved)}")
        check("result is rescheduled", moved.get("result") == "rescheduled",
              str(moved.get("result")))
        check("new row is booked at the new time",
              moved.get("start_time") == target and moved.get("status") == "booked",
              f"{moved.get('start_time')} {moved.get('status')}")
        new_id = moved.get("id")
        check("the move created a NEW row (id differs)", new_id != appt_id,
              f"{appt_id} -> {new_id}")

        old = row(P1, appt_id)
        check("old row is cancelled, freeing the slot",
              old and old["status"] == "cancelled", str(old and old["status"]))

        print("\n--- R1: a refused reschedule leaves the original untouched ---")
        st, bad = call("POST", f"/api/appointments/{new_id}/reschedule",
                       {"date": sunday, "start_time": "10:00"}, token=P1)
        check("Sunday refused with CLINIC_CLOSED", st == 400 and code(bad) == "CLINIC_CLOSED",
              f"{st} {code(bad)} {msg(bad)}")
        still = row(P1, new_id)
        check("original unchanged after the refusal",
              still and still["status"] == "booked" and still["start_time"] == target
              and still["date"] == day,
              str(still and (still["status"], still["date"], still["start_time"])))

        # A slot somebody else holds: the new booking is checked first.
        # free_slots already withholds anything taken, so pick a second one.
        s2 = free_slots(P2, "D1", day)
        taken = None
        if len(s2) > 1:
            taken = s2[1]
            st, other = book(P2, "D1", "General", "new", day, taken, "Routine eye test")
            if llm_down(other):
                taken = None
                skip("move onto a taken slot is refused",
                     "red-flag check unavailable (LLM quota)")
            elif st != 201:
                taken = None
                skip("move onto a taken slot is refused", f"{st} {code(other)} {msg(other)}")
        if taken:
            st, clash = call("POST", f"/api/appointments/{new_id}/reschedule",
                             {"date": day, "start_time": taken}, token=P1)
            check("move onto a taken slot refused (DOUBLE_BOOKED)",
                  st == 400 and code(clash) == "DOUBLE_BOOKED",
                  f"{st} {code(clash)} {msg(clash)}")
            still = row(P1, new_id)
            check("original unchanged after DOUBLE_BOOKED",
                  still and still["start_time"] == target,
                  str(still and still["start_time"]))
        else:
            skip("move onto a taken slot is refused", "no second free slot that day")

        print("\n--- R1: you cannot move somebody else's appointment ---")
        st, steal = call("POST", f"/api/appointments/{new_id}/reschedule",
                         {"date": day, "start_time": "16:00"}, token=P3)
        check("another patient refused 403 NOT_YOUR_APPOINTMENT",
              st == 403 and code(steal) == "NOT_YOUR_APPOINTMENT",
              f"{st} {code(steal)} {msg(steal)}")
        still = row(P1, new_id)
        check("appointment unchanged after the failed steal",
              still and still["status"] == "booked" and still["start_time"] == target,
              str(still and (still["status"], still["start_time"])))
    else:
        skip("reschedule checks", "could not book a movable appointment")

    # Hand the patients a clean slate for the late-change checks.
    for tok in (P1, P2):
        _, prior = call("GET", "/api/appointments", token=tok)
        for a in prior:
            if a.get("status") == "booked":
                call("POST", f"/api/appointments/{a['id']}/cancel", {}, token=STAFF)

    # ---------------------------------------------------------------------
    print("\n--- R1: a cancel inside 2 hours goes to Staff, not straight through ---")
    # ---------------------------------------------------------------------
    late_slots = [s for s in free_slots(P2, "D1", today.isoformat())
                  if _mins(s) > _mins(config.now().strftime("%H:%M")) + 45
                  and _mins(s) < _mins(config.now().strftime("%H:%M")) + 115]
    if not late_slots:
        skip("late cancel is queued for Staff", "no slot inside the 2-hour window today")
        late_id = None
    else:
        st, late = book(P2, "D1", "General", "new", today.isoformat(), late_slots[0],
                        "Slight blur when reading")
        if llm_down(late):
            skip("late cancel is queued for Staff", "red-flag check unavailable (LLM quota)")
            late_id = None
        else:
            check("booked a slot starting inside 2 hours", st == 201,
                  f"{st} {msg(late)} {late.get('start_time')}")
            late_id = late.get("id")

    if late_id:
        st, queued = call("POST", f"/api/appointments/{late_id}/cancel", {}, token=P2)
        check("patient cancel returns 200 (not a refusal)", st == 200, f"{st} {code(queued)}")
        check("result is to_staff", queued.get("result") == "to_staff",
              str(queued.get("result")))
        check("an approval request id came back", bool(queued.get("approval_id")),
              str(queued.get("approval_id")))
        approval_id = queued.get("approval_id")
        check("the appointment STAYS booked until Staff act",
              queued.get("status") == "booked", str(queued.get("status")))

        print("\n--- R1: Staff see the request, and only Staff can act on it ---")
        st, pend = call("GET", "/api/staff/approvals", token=STAFF)
        mine = [a for a in pend if a.get("appointment_id") == late_id]
        check("Staff list returns the pending request", st == 200 and len(mine) == 1,
              f"{st} {len(mine)} pending")
        if mine:
            check("the request names the patient and the action",
                  mine[0].get("action") == "cancel"
                  and mine[0].get("patient_id") == "P02"
                  and mine[0].get("status") == "pending",
                  f"{mine[0].get('action')} {mine[0].get('patient_id')} {mine[0].get('status')}")

            print("\n--- R1: Reject leaves the appointment exactly as it was ---")
            st, rej = call("POST", f"/api/staff/approvals/{approval_id}/reject", {}, token=STAFF)
            check("reject returns status rejected", st == 200 and rej.get("status") == "rejected",
                  f"{st} {rej.get('status')}")
            still = row(P2, late_id)
            check("appointment still booked after Reject",
                  still and still["status"] == "booked", str(still and still["status"]))

            print("\n--- R1: Approve carries the action out as Staff ---")
            st, queued2 = call("POST", f"/api/appointments/{late_id}/cancel", {}, token=P2)
            check("asking again queues a fresh request", st == 200
                  and queued2.get("result") == "to_staff"
                  and queued2.get("approval_id") not in (None, approval_id),
                  f"{st} {queued2.get('approval_id')} (was {approval_id})")
            st, ok = call("POST", f"/api/staff/approvals/{queued2['approval_id']}/approve",
                          {}, token=STAFF)
            check("approve runs the cancel", st == 200 and ok.get("status") == "cancelled",
                  f"{st} {ok.get('status')} {code(ok)}")
            now_row = row(P2, late_id)
            check("the appointment is cancelled after Approve",
                  now_row and now_row["status"] == "cancelled",
                  str(now_row and now_row["status"]))

            print("\n--- R1: the patient sees the request's status next to the appointment ---")
            _, rows = call("GET", "/api/appointments", token=P2)
            seen = [r for r in rows if r.get("approval_status")]
            check("the patient's list carries approval_status/action",
                  any(r["id"] == late_id for r in seen),
                  str([(r["id"], r["approval_status"], r["approval_action"]) for r in seen]))
        else:
            skip("Staff act on the request", "request did not appear in the Staff list")
            approval_id = None

    # ---------------------------------------------------------------------
    print("\n--- R1: a reschedule inside 2 hours also goes to Staff ---")
    # ---------------------------------------------------------------------
    late_slots = [s for s in free_slots(P1, "D1", today.isoformat())
                  if _mins(s) > _mins(config.now().strftime("%H:%M")) + 45
                  and _mins(s) < _mins(config.now().strftime("%H:%M")) + 115]
    if not late_slots:
        skip("late reschedule is queued for Staff", "no slot inside the 2-hour window today")
        move_id = None
    else:
        st, base = book(P1, "D1", "General", "new", today.isoformat(), late_slots[0],
                        "Letters are harder to read")
        if llm_down(base):
            skip("late reschedule is queued for Staff", "red-flag check unavailable (LLM quota)")
            move_id = None
        else:
            check("booked a movable slot inside 2 hours", st == 201,
                  f"{st} {msg(base)} {base.get('start_time')}")
            move_id = base.get("id")

    if move_id:
        original = row(P1, move_id)
        target = next((s for s in free_slots(P1, "D1", today.isoformat())
                       if s > original["start_time"]), None)
        if not target:
            skip("late reschedule is queued for Staff", "no later free slot today")
        else:
            st, queued = call("POST", f"/api/appointments/{move_id}/reschedule",
                              {"date": today.isoformat(), "start_time": target}, token=P1)
            check("late reschedule returns 200 to_staff", st == 200
                  and queued.get("result") == "to_staff", f"{st} {queued.get('result')}")
            check("an approval request came back", bool(queued.get("approval_id")),
                  str(queued.get("approval_id")))
            still = row(P1, move_id)
            check("nothing has moved yet — original time intact",
                  still and still["start_time"] == original["start_time"]
                  and still["status"] == "booked",
                  str(still and (still["status"], still["start_time"])))

            st, ok = call("POST", f"/api/staff/approvals/{queued['approval_id']}/approve",
                          {}, token=STAFF)
            check("Staff Approve performs the reschedule", st == 200
                  and ok.get("result") == "rescheduled" and ok.get("start_time") == target,
                  f"{st} {ok.get('result')} {ok.get('start_time')} (wanted {target})")
            _, rows = call("GET", "/api/appointments", token=P1)
            moved_old = next((r for r in rows if r["id"] == move_id), None)
            check("the original row is cancelled after Approve",
                  moved_old and moved_old["status"] == "cancelled",
                  str(moved_old and moved_old["status"]))

    # ---------------------------------------------------------------------
    print("\n--- R1: only Staff may touch the approval queue ---")
    # ---------------------------------------------------------------------
    st, denied = call("GET", "/api/staff/approvals", token=P1)
    check("a patient cannot list approvals", st == 403 and code(denied) == "WRONG_ROLE",
          f"{st} {code(denied)}")
    st, denied = call("POST", "/api/staff/approvals/1/approve", {}, token=P1)
    check("a patient cannot approve", st == 403 and code(denied) == "WRONG_ROLE",
          f"{st} {code(denied)}")
    st, denied = call("POST", "/api/staff/approvals/1/reject", {}, token=P1)
    check("a patient cannot reject", st == 403 and code(denied) == "WRONG_ROLE",
          f"{st} {code(denied)}")

    _, doc_login = call("POST", "/api/login",
                        {"role": "doctor", "doctor_id": "D1", "name": "Dr. Anand Rao"})
    if doc_login.get("token"):
        st, denied = call("GET", "/api/staff/approvals", token=doc_login["token"])
        check("a doctor cannot list approvals", st == 403 and code(denied) == "WRONG_ROLE",
              f"{st} {code(denied)}")
    else:
        skip("a doctor cannot list approvals", "doctor login unavailable")

    print("\n" + "=" * 66)
    print(f"approval checks: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    for s in SKIP:
        print("   SKIP:", s)
    print("=" * 66)


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


if __name__ == "__main__":
    try:
        main()
    finally:
        for _p in (_TMP_DB, f"{_TMP_DB}-wal", f"{_TMP_DB}-shm"):
            if os.path.exists(_p):
                try:
                    os.unlink(_p)
                except OSError:
                    pass
    sys.exit(1 if FAIL else 0)
