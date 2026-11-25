"""Verify follow-ups work with the completed visits from data pack 2 (R2 + R5).

Fatima Begum  (P13) completed Retina  with Dr. Shah   (D3, only Retina doctor)
Harish Chandra(P18) completed Cataract with Dr. Rao    (D1; Dr. Iyer D2 also does Cataract)

Iteration 2 changed two things this suite must respect:
  * every booking now needs a `reason` (R3), and
  * the old hard-coded date (2026-11-04) is in the past, so dates are found
    dynamically from /api/slots, the same way check_api.py does it.

Run:  venv\\Scripts\\python.exe tests\\check_followups.py   (server running)
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = "http://127.0.0.1:8000"
PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
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
    except Exception as e:
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


def find_slot(token, doctor_id, visit_type, start_from=None, limit=30):
    """First free slot for this doctor in the next `limit` days (never a Sunday).

    Returns (date, slots) so a test can pick a *different* time when it wants
    a rule other than "that slot is taken" to fire.
    """
    day = start_from or date.today()
    for _ in range(limit):
        day += timedelta(days=1)
        if day.weekday() == 6:
            continue
        _, s = call("GET", f"/api/slots?doctor_id={doctor_id}"
                           f"&date={day.isoformat()}&visit_type={visit_type}", token=token)
        slots = s.get("slots") or []
        if slots:
            return day.isoformat(), slots
    return None, []


def main():
    # Staff can read the patient directory; use it to resolve mobiles.
    _, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    stok = staff["token"]
    _, patients = call("GET", "/api/patients", token=stok)
    by_id = {p["patient_id"]: p for p in patients}

    fatima = by_id["P13"]
    harish = by_id["P18"]
    print(f"\n  P13 {fatima['name']} ({fatima['mobile']})")
    print(f"  P18 {harish['name']} ({harish['mobile']})")

    _, tf = call("POST", "/api/login",
                 {"role": "patient", "mobile": fatima["mobile"], "name": fatima["name"]})
    _, th = call("POST", "/api/login",
                 {"role": "patient", "mobile": harish["mobile"], "name": harish["name"]})

    # Clear anything a previous run left behind so the suite is repeatable.
    for sess in (tf, th):
        _, prior = call("GET", "/api/appointments", token=sess["token"])
        for a in prior:
            if a.get("status") == "booked":
                call("POST", f"/api/appointments/{a['id']}/cancel", {}, token=sess["token"])

    print("\n--- Fatima Begum: completed Retina with Dr. Shah (D3) ---")

    fu_date, fu_slots = find_slot(tf["token"], "D3", "follow_up")
    fu_time = fu_slots[0] if fu_slots else None
    if not fu_time:
        skip("follow-up with the completing doctor is ALLOWED", "no Dr. Shah slot in the next 30 days")
    else:
        # 1. The headline: follow-up with the doctor who completed the visit.
        status, r = call("POST", "/api/appointments", {
            "doctor_id": "D3", "speciality": "Retina", "visit_type": "follow_up",
            "date": fu_date, "start_time": fu_time,
            "reason": "Retina review after laser",
        }, token=tf["token"])
        if llm_down(r):
            skip("follow-up with the completing doctor is ALLOWED",
                 "red-flag check unavailable (LLM quota)")
            r = {}
        else:
            check("follow-up with the completing doctor is ALLOWED",
                  status == 201, f"{r.get('date')} {r.get('start_time')}-{r.get('end_time')} id {r.get('id')}")

        # 2. A second Retina booking must be refused — she now holds one.
        #    Use a DIFFERENT time so the slot-collision rule cannot mask it.
        if r.get("id") and len(fu_slots) > 1:
            status, r2 = call("POST", "/api/appointments", {
                "doctor_id": "D3", "speciality": "Retina", "visit_type": "new",
                "date": fu_date, "start_time": fu_slots[1],
                "reason": "Second opinion on retina",
            }, token=tf["token"])
            if llm_down(r2):
                skip("second Retina booking refused (one active per speciality)",
                     "red-flag check unavailable (LLM quota)")
            else:
                check("second Retina booking refused (one active per speciality)",
                      code(r2) == "ACTIVE_BOOKING_EXISTS", msg(r2))
        elif r.get("id"):
            skip("second Retina booking refused (one active per speciality)",
                 "only one free slot that day")

    print("\n--- Harish Chandra: completed Cataract with Dr. Rao (D1) ---")

    # 3. Follow-up with a different doctor who DOES cover Cataract.
    #    Dr. Iyer (D2) must be on OPD for Cataract that day, so probe slots.
    wrong_date, wrong_slots = find_slot(th["token"], "D2", "follow_up")
    wrong_time = wrong_slots[0] if wrong_slots else None
    if not wrong_time:
        skip("follow-up with a DIFFERENT doctor refused", "no Dr. Iyer slot in the next 30 days")
    else:
        status, r3 = call("POST", "/api/appointments", {
            "doctor_id": "D2", "speciality": "Cataract", "visit_type": "follow_up",
            "date": wrong_date, "start_time": wrong_time,
            "reason": "Cataract review",
        }, token=th["token"])
        if llm_down(r3):
            skip("follow-up with a DIFFERENT doctor refused",
                 "red-flag check unavailable (LLM quota)")
        else:
            check("follow-up with a DIFFERENT doctor refused",
                  code(r3) == "FOLLOW_UP_DIFFERENT_DOCTOR", msg(r3))

    # 4. Same speciality, correct doctor: allowed.
    ok_date, ok_slots = find_slot(th["token"], "D1", "follow_up")
    ok_time = ok_slots[0] if ok_slots else None
    if not ok_time:
        skip("follow-up with the completing doctor allowed", "no Dr. Rao slot in the next 30 days")
    else:
        status, r4 = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "Cataract", "visit_type": "follow_up",
            "date": ok_date, "start_time": ok_time,
            "reason": "Cataract review after surgery",
        }, token=th["token"])
        if llm_down(r4):
            skip("follow-up with the completing doctor allowed",
                 "red-flag check unavailable (LLM quota)")
        else:
            check("follow-up with the completing doctor allowed",
                  status == 201, f"id {r4.get('id')}")

            # 5. Durations differ: the follow-up is 10 minutes, not 20.
            if status == 201:
                h, m = map(int, r4["start_time"].split(":"))
                eh, em = map(int, r4["end_time"].split(":"))
                check("follow-up is 10 minutes (not 20)",
                      (eh * 60 + em) - (h * 60 + m) == 10, r4["end_time"])

    print("\n" + "=" * 66)
    print(f"follow-up checks: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    for s in SKIP:
        print("   SKIP:", s)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
