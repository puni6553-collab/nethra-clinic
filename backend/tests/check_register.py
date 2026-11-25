"""New patient registration end to end (R3 + R5).

A fresh patient registers, is signed in immediately, books a real appointment,
and can only ever see their own. Registering the same mobile twice is refused.

Run:  venv\\Scripts\\python.exe tests\\check_register.py
"""
from __future__ import annotations

import json
import secrets
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = "http://127.0.0.1:8000"
PASS: list[str] = []
FAIL: list[str] = []


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


def msg(p):
    d = p.get("detail", p)
    return d.get("message", "") if isinstance(d, dict) else str(d)


def code(p):
    d = p.get("detail", p)
    return d.get("code", "") if isinstance(d, dict) else ""


def llm_down(p) -> bool:
    return code(p) == "REDFLAG_CHECK_FAILED"


def find_slot(token, doctor_id="D1", visit_type="new", limit=30):
    """First day in the next `limit` days that actually has a free slot.

    Dates are computed at run time — a hard-coded day is in the past the
    moment the clock moves on (and Sundays are never bookable).
    """
    day = date.today()
    for _ in range(limit):
        day += timedelta(days=1)
        if day.weekday() == 6:
            continue
        status, s = call("GET", f"/api/slots?doctor_id={doctor_id}"
                                f"&date={day.isoformat()}&visit_type={visit_type}",
                         token=token)
        slots = s.get("slots", []) if status == 200 else []
        if slots:
            return day.isoformat(), slots
    return None, []


def main():
    # A fresh mobile each run, clear of the pack's 90000000xx range, so the
    # suite always tests a genuinely new sign-up rather than tripping over
    # the account it created last time.
    mobile, name = f"97{secrets.randbelow(10**8):08d}", "Ravi Nair"

    print(f"\n--- registering a brand new patient: {name} ({mobile}) ---")

    status, s = call("POST", "/api/register",
                     {"mobile": mobile, "name": name, "year_of_birth": 1992})
    check("registration succeeds and signs them in",
          status == 200 and s.get("token") and s.get("role") == "patient",
          f"patient_id {s.get('patient_id')}")
    token = s.get("token")
    patient_id = s.get("patient_id")
    if not token:
        print("  cannot continue without a session"); sys.exit(1)

    print("\n--- the rules must refuse a bad or duplicate sign-up ---")

    status, dup = call("POST", "/api/register",
                       {"mobile": mobile, "name": "Someone Else"})
    check("same mobile refused (already registered)",
          code(dup) == "MOBILE_TAKEN", msg(dup))

    status, bad = call("POST", "/api/register", {"mobile": "12345", "name": "X"})
    check("short mobile refused", code(bad) == "INVALID_MOBILE", msg(bad))

    status, noName = call("POST", "/api/register", {"mobile": "9876543210", "name": "  "})
    check("blank name refused", code(noName) == "MISSING_NAME", msg(noName))

    print("\n--- the new patient starts with nothing, then books ---")

    status, mine = call("GET", "/api/appointments", token=token)
    check("new account sees no appointments", status == 200 and mine == [], f"{len(mine)}")

    # Ask the API which slots are genuinely free rather than guessing a time.
    book_date, free = find_slot(token)
    check("free slots are offered for a real doctor/day", bool(free),
          f"{book_date} -> {len(free)} free")
    if not free:
        print("  no slots to book; stopping"); sys.exit(1)

    status, noReason = call("POST", "/api/appointments", {
        "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
        "date": book_date, "start_time": free[0],
    }, token=token)
    check("booking without a reason refused",
          code(noReason) == "MISSING_REASON", msg(noReason))

    status, booked = call("POST", "/api/appointments", {
        "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
        "date": book_date, "start_time": free[0],
        "reason": "Blurry vision when reading",
    }, token=token)
    if llm_down(booked):
        # The red-flag check runs before any booking; without the LLM the
        # booking cannot complete (same convention as check_api.py).
        print("  SKIP  new patient books an appointment  -> "
              "red-flag check unavailable (LLM quota)")
        return
    check("new patient books an appointment", status == 201,
          f"{booked.get('date')} {booked.get('start_time')}-{booked.get('end_time')}")

    status, after = call("GET", "/api/appointments", token=token)
    check("the booking shows up for them",
          len(after) == 1 and after[0].get("patient_id") == patient_id,
          f"{len(after)} row(s)")

    print("\n--- staff can see the new patient in the directory ---")
    _, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    _, directory = call("GET", "/api/patients", token=staff["token"])
    found = [p for p in directory if p["patient_id"] == patient_id]
    check("new patient appears in the staff directory",
          found and found[0]["name"] == name, f"{len(directory)} patients total")

    print("\n" + "=" * 66)
    print(f"registration checks: {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("   FAIL:", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
