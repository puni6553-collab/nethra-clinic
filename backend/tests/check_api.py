"""End-to-end API checks: login, roles, booking, refusal, cancellation.

Exercises the running server over HTTP, exactly as the browser and an
AI agent would.

Run:  venv\\Scripts\\python.exe tests\\check_api.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

BASE = "http://127.0.0.1:8000"
PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def call(method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}
    except Exception as exc:  # connection refused etc.
        return 0, {"error": str(exc)}


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))


def skip(label: str, why: str) -> None:
    SKIP.append(label)
    print(f"  SKIP  {label}  -> {why}")


def message(payload: dict) -> str:
    d = payload.get("detail", payload)
    return d.get("message", "") if isinstance(d, dict) else str(d)


def code(payload: dict) -> str:
    d = payload.get("detail", payload)
    return d.get("code", "") if isinstance(d, dict) else ""


def mins(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def next_open_day(from_day: date, *, want_weekday: int | None = None) -> date:
    """First upcoming day that is not a Sunday (the clinic's closed day)."""
    day = from_day
    for _ in range(10):
        day += timedelta(days=1)
        if day.weekday() != 6 and (want_weekday is None or day.weekday() == want_weekday):
            return day
    return from_day + timedelta(days=1)


def closure_notes(token: str, day: date) -> list[str]:
    """Why the clinic cannot see patients that day (empty when it can).

    The data pack also has holidays, which the calendar helper above knows
    nothing about — the slots endpoint is the authority on both.
    """
    _, s = call("GET", f"/api/slots?doctor_id=D1&date={day}&visit_type=new", token=token)
    return list(s.get("notes", []))


def first_bookable_day(
    token: str, from_day: date, *, want_weekday: int | None = None, doctor_id: str = "D1"
) -> date:
    """First upcoming non-Sunday that actually offers slots for `doctor_id`."""
    day = from_day
    for _ in range(45):
        day = next_open_day(day, want_weekday=want_weekday)
        _, s = call("GET", f"/api/slots?doctor_id={doctor_id}&date={day}&visit_type=new", token=token)
        if s.get("slots"):
            return day
    return next_open_day(from_day, want_weekday=want_weekday)


def slot_away(slots: list[str], reference: str, gap: int = 30) -> str:
    """A listed slot at least `gap` minutes from `reference`, so it cannot
    collide with the appointment already held there."""
    ref = mins(reference)
    for s in slots:
        if abs(mins(s) - ref) >= gap:
            return s
    return slots[-1] if slots else "15:00"


def next_bookable_today(token: str) -> str | None:
    """A free start time today that sits inside the 2-hour cut-off.

    Asks /api/slots rather than working it out here: the endpoint already
    knows about Sundays, holidays, surgery mornings and the booking window,
    so a day the clinic is not open comes back with no slots and this simply
    returns None (AGENTS.md trap 5 — never hardcode a day's openness).
    """
    status, payload = call(
        "GET", f"/api/slots?doctor_id=D1&date={date.today().isoformat()}&visit_type=new",
        token=token,
    )
    if status != 200:
        return None
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    for slot in payload.get("slots") or []:
        start_min = mins(slot)
        # The endpoint already withheld anything in the past; this keeps the
        # start far enough away to book and under the cut-off (2 hours = 120
        # minutes, config.CANCEL_CUTOFF_HOURS).
        if now_min + 45 <= start_min < now_min + 120:
            return slot
    return None


def main() -> None:
    today = date.today()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7 or 7)
    # `soon`, `thu` and `far` need the slots endpoint to know which days the
    # clinic is actually open (holidays included), so they are resolved once
    # a session exists — see the R4 section below.

    print("\n--- health ---")
    status, health = call("GET", "/api/health")
    check("server is up", status == 200, f"clock={health.get('clock')}")

    print("\n--- R1: data pack ---")
    status, doctors = call("GET", "/api/doctors")
    check("5 doctors visible", status == 200 and len(doctors) == 5, f"{len(doctors)} doctors")

    print("\n--- R3: logins ---")
    status, p = call("POST", "/api/login", {"role": "patient", "mobile": "9000000001", "name": "Asha Verma"})
    check("patient login (Asha Verma)", status == 200 and p.get("patient_id") == "P01", f"{p.get('patient_id')}")
    p_tok = p.get("token", "")

    status, wrong = call("POST", "/api/login", {"role": "patient", "mobile": "9000000001", "name": "Someone Else"})
    check("wrong name refused", status == 401, code(wrong))

    status, new_p = call("POST", "/api/login", {"role": "patient", "mobile": "9812345670", "name": "Walk In"})
    check("new mobile creates a patient", status == 200, new_p.get("patient_id", ""))
    other_tok = new_p.get("token", "")

    # The booking checks need a patient who cannot already hold a conflicting
    # booking. The data pack seeds Asha Verma with an active Cataract
    # appointment, which would trip the one-active-per-speciality rule before
    # the test got started — so use a guaranteed-clean patient instead.
    # Registered once, then logged into on later runs (the suite is repeatable).
    status, fresh = call("POST", "/api/register",
                         {"mobile": "9876500001", "name": "Suite Bookings"})
    if status != 200:
        status, fresh = call("POST", "/api/login",
                             {"role": "patient", "mobile": "9876500001", "name": "Suite Bookings"})
    check("clean patient ready for booking checks",
          status == 200 and fresh.get("patient_id"), fresh.get("patient_id", ""))
    book_tok = fresh.get("token", "")

    # Both of these patients are reused on every run, so clear whatever a
    # previous run left behind and let each run start from empty.
    for test_token in (book_tok, other_tok):
        _, prior = call("GET", "/api/appointments", token=test_token)
        for a in prior:
            if a.get("status") == "booked":
                call("POST", f"/api/appointments/{a['id']}/cancel", {}, token=test_token)

    status, st = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    check("staff login", status == 200 and st.get("role") == "staff")
    st_tok = st.get("token", "")

    status, dr = call("POST", "/api/login", {"role": "doctor", "doctor_id": "D1"})
    check("doctor login (Dr. Rao)", status == 200 and dr.get("doctor_id") == "D1")
    dr_tok = dr.get("token", "")

    status, dr2 = call("POST", "/api/login", {"role": "doctor", "doctor_id": "D3"})
    check("second doctor login (Dr. Shah)", status == 200 and dr2.get("doctor_id") == "D3")
    dr2_tok = dr2.get("token", "")

    status, anon = call("GET", "/api/appointments")
    check("no token -> 401", status == 401, code(anon))

    print("\n--- R4: free slots ---")
    # Resolve the days this suite books against. The clinic's calendar has
    # holidays as well as closed Sundays, and a closed day would mask the
    # very rule each check is trying to demonstrate.
    soon = first_bookable_day(p_tok, today)
    thu = next_open_day(today, want_weekday=3)          # next Thursday
    while closure_notes(p_tok, thu):                    # ... that is open
        thu = next_open_day(thu, want_weekday=3)
    far = today + timedelta(days=45)                    # past the 30-day window
    while far.weekday() == 6 or closure_notes(p_tok, far):
        far += timedelta(days=1)

    status, slots = call("GET", f"/api/slots?doctor_id=D1&date={soon}&visit_type=new", token=p_tok)
    listed = slots.get("slots", [])
    check("slots offered for a normal day", status == 200 and len(listed) > 0, f"{len(listed)} slots")

    # Dr. Iyer (D2) is OFF on Thursdays.
    thu = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    status, off = call("GET", f"/api/slots?doctor_id=D2&date={thu.isoformat()}&visit_type=new", token=p_tok)
    check("Dr. Iyer offers no slots on Thursday",
          status == 200 and off.get("slots") == [],
          "; ".join(off.get("notes", [])))

    # Follow-ups are 10 minutes, so they fit more starts than consultations.
    status, fu_slots = call("GET", f"/api/slots?doctor_id=D1&date={soon}&visit_type=follow_up", token=p_tok)
    check("follow-up list is longer than consultation list",
          status == 200 and len(fu_slots.get("slots", [])) >= len(listed),
          f"{len(fu_slots.get('slots', []))} vs {len(listed)}")

    print("\n--- R5: booking + refusals ---")
    book_date, book_time = soon.isoformat(), listed[0]

    # Reason is required (iteration 2) and red-flag reasons are refused first.
    status, no_reason = call("POST", "/api/appointments", {
        "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
        "date": book_date, "start_time": book_time,
    }, token=book_tok)
    check("booking without a reason refused",
          status == 400 and code(no_reason) == "MISSING_REASON", message(no_reason))

    status, redflag = call("POST", "/api/appointments", {
        "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
        "date": book_date, "start_time": book_time,
        "reason": "I suddenly can't see anything from my left eye",
    }, token=book_tok)
    if status == 503 and code(redflag) == "REDFLAG_CHECK_FAILED":
        # The safety check could not run (LLM quota), so the server rightly
        # refuses every booking — none of the booking rules below can be
        # exercised. Report them as skipped instead of failed.
        for _name in (
            "sudden vision loss is flagged urgent",
            "booking succeeds",
            "20-minute consultation",
            "same slot for another patient refused",
            "one active booking per speciality",
            "follow-up with no prior visit refused",
            "Dr. Khan + Glaucoma refused",
            "45 days ahead refused",
            "Sunday refused",
            "Dr. Rao Thursday morning refused",
            "13:30 refused (outside session)",
        ):
            skip(_name, "red-flag check unavailable (LLM quota)")
        appt_id = None
    else:
        # Emergency response first (handout R4): either the booking went
        # through with `emergency_message`, or a rule refused it with the
        # emergency text riding along in the error detail.
        def emerg(payload: dict) -> str:
            d = payload.get("detail", payload)
            return d.get("emergency_message", "") if isinstance(d, dict) else ""

        emergency_first = (
            status in (400, 503)
            and (code(redflag) in {"URGENT_CASE", "REDFLAG_CHECK_FAILED"} or emerg(redflag))
        ) or (
            status == 201 and bool(redflag.get("emergency_message"))
        )
        check("sudden vision loss is flagged urgent", emergency_first,
              f"{status} {code(redflag) or 'booked'} {message(redflag)}")
        # If the urgent booking went through it holds the slot/speciality — clear it.
        if status == 201 and redflag.get("id"):
            call("POST", f"/api/appointments/{redflag['id']}/cancel", {}, token=book_tok)

        status, booked = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
            "date": book_date, "start_time": book_time,
            "reason": "Cataract review after recent flashes",
        }, token=book_tok)
        check("booking succeeds", status == 201, f"{book_date} {book_time} -> id {booked.get('id')}")
        appt_id = booked.get("id")
        check("20-minute consultation",
              booked.get("end_time") and mins(booked["end_time"]) - mins(book_time) == 20,
              booked.get("end_time", ""))

        # Same slot again, different patient -> slot taken. (Use "General": Dr. Rao
        # covers it, and this patient has no active General booking yet.)
        status, clash = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": book_date, "start_time": book_time,
            "reason": "Routine eye check",
        }, token=other_tok)
        check("same slot for another patient refused",
              status == 400 and code(clash) == "DOUBLE_BOOKED", message(clash))

        # The booker now holds a Cataract booking: a *different* Cataract time is
        # refused by the one-active-per-speciality rule (not by overlap).
        status, second = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
            "date": book_date, "start_time": slot_away(listed, book_time),
            "reason": "Second opinion on cataract",
        }, token=book_tok)
        check("one active booking per speciality",
              code(second) == "ACTIVE_BOOKING_EXISTS", message(second))

        # Follow-up for a patient with no completed visit at all. Use the Suite
        # Bookings patient: it holds no completed visits (its booking is cancelled
        # before the doctor-visit section runs), so this refusal cannot be masked
        # by a visit left over from an earlier run.
        status, fu = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "follow_up",
            "date": book_date, "start_time": slot_away(listed, book_time),
            "reason": "Follow-up visit",
        }, token=book_tok)
        check("follow-up with no prior visit refused",
              code(fu) == "NO_PREVIOUS_VISIT", message(fu))

        # Speciality the doctor does not cover (Dr. Khan does not do Glaucoma).
        # Ask for a day Dr. Khan actually offers slots first: `soon` is
        # Dr. Rao's day, and on a Monday Dr. Khan operates — SURGERY_SESSION
        # would fire before the speciality rule and mask it (trap 5).
        khan_day = first_bookable_day(other_tok, soon, doctor_id="D4")
        _, khan_slots = call(
            "GET", f"/api/slots?doctor_id=D4&date={khan_day}&visit_type=new", token=other_tok
        )
        status, spec = call("POST", "/api/appointments", {
            "doctor_id": "D4", "speciality": "Glaucoma", "visit_type": "new",
            "date": khan_day.isoformat(),
            "start_time": (khan_slots.get("slots") or ["10:00"])[0],
            "reason": "Eye pressure check",
        }, token=other_tok)
        check("Dr. Khan + Glaucoma refused", code(spec) == "SPECIALITY_MISMATCH", message(spec))

        # Booking window: 45 days ahead (on a weekday, so the window is the
        # only rule that can bite).
        status, window = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": far.isoformat(), "start_time": "10:00",
            "reason": "Routine eye test",
        }, token=other_tok)
        check("45 days ahead refused", code(window) == "BEYOND_30_DAYS", message(window))

        # Sunday.
        status, sun = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": sunday.isoformat(), "start_time": "10:00",
            "reason": "Routine eye test",
        }, token=other_tok)
        check("Sunday refused", code(sun) == "CLINIC_CLOSED", message(sun))

        # Surgery session: Dr. Rao operates Thursday AM.
        status, sx = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
            "date": thu.isoformat(), "start_time": "10:00",
            "reason": "Cataract consultation",
        }, token=other_tok)
        check("Dr. Rao Thursday morning refused",
              code(sx) in {"SURGERY_SESSION", "DOCTOR_OFF"}, message(sx))

        # Handout §09: a RED-FLAG reason on that same refused slot. The
        # emergency response must come first, an urgent case must be saved,
        # and the booking itself must still be refused by the rule.
        status, rf_sx = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
            "date": thu.isoformat(), "start_time": "10:00",
            "reason": "I suddenly can't see from my right eye",
        }, token=other_tok)
        if code(rf_sx) == "REDFLAG_CHECK_FAILED":
            skip("red flag + surgery session: emergency first, then refusal",
                 "red-flag check unavailable (LLM quota)")
        else:
            sx_refused = status == 400 and code(rf_sx) in {"SURGERY_SESSION", "DOCTOR_OFF"}
            emerg_text = (rf_sx.get("detail", {}) or {}).get("emergency_message", "")
            check("red flag + surgery session: emergency first, then refusal",
                  sx_refused and "urgent attention" in emerg_text,
                  f"{status} {code(rf_sx)} emerg={bool(emerg_text)} {message(rf_sx)}")
            # The urgent case must stand even though the booking was refused.
            status, cases = call("GET", "/api/urgent-cases", token=st_tok)
            check("urgent case stands after a refused booking",
                  status == 200 and any(
                      "suddenly can't see" in (c.get("reason") or "") and not c.get("seen")
                      for c in cases
                  ), f"{len(cases)} cases")
            # Tidy up: mark it seen so later runs start clean.
            for c in cases:
                if "suddenly can't see" in (c.get("reason") or "") and not c.get("seen"):
                    call("POST", f"/api/urgent-cases/{c['id']}/seen", {}, token=st_tok)

        # A time outside clinic hours (13:30 sits in the lunch break).
        status, lunch = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": soon.isoformat(), "start_time": "13:30",
            "reason": "Routine eye test",
        }, token=other_tok)
        check("13:30 refused (outside session)", code(lunch) == "OUTSIDE_SESSION", message(lunch))

    print("\n--- R3: role scoping (the security check) ---")
    status, mine = call("GET", "/api/appointments", token=p_tok)
    check("patient sees only their own appointments",
          status == 200 and all(a["patient_id"] == "P01" for a in mine), f"{len(mine)} rows")

    status, others = call("GET", "/api/appointments", token=other_tok)
    check("second patient does not see P01's", all(a["patient_id"] != "P01" for a in others))

    status, dr_view = call("GET", "/api/appointments", token=dr_tok)
    check("doctor sees only own schedule", all(a["doctor_id"] == "D1" for a in dr_view))

    if appt_id:
        status, steal = call("POST", f"/api/appointments/{appt_id}/cancel", {}, token=other_tok)
        check("cancelling someone else's refused",
              status == 403 and code(steal) == "NOT_YOUR_APPOINTMENT", message(steal))
    else:
        skip("cancelling someone else's refused", "no booking to cancel")

    status, staff_view = call("GET", "/api/appointments", token=st_tok)
    check("staff sees every appointment", status == 200, f"{len(staff_view)} rows")

    # Handout §09: the doctor summary is doctors-only, checked in the backend.
    status, steal_summary = call("GET", "/api/doctors/patients/P01/summary", token=p_tok)
    check("patient cannot fetch a doctor summary",
          status in (401, 403) and code(steal_summary) in {"WRONG_ROLE", "NOT_LOGGED_IN"},
          f"{status} {code(steal_summary)}")

    print("\n--- R5: cancellation ---")

    # (a) The 2-hour cut-off. It can only be shown when the clinic is open
    #     today and a free start still sits inside the window, so the helper
    #     probes /api/slots instead of assuming today is a working day.
    near_slot = next_bookable_today(st_tok)
    if not near_slot:
        for label in ("booking inside 2 hours succeeds",
                      "patient cancel inside 2 hours is queued for Staff",
                      "the appointment stays booked until Staff act",
                      "a Staff approval request was raised",
                      "staff can cancel inside 2 hours"):
            skip(label, "no free slot inside the 2-hour window today")
    else:
        status, near = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": date.today().isoformat(), "start_time": near_slot,
            "reason": "Same-day eye discomfort",
        }, token=other_tok)
        near_id = near.get("id")
        if not near_id and code(near) == "REDFLAG_CHECK_FAILED":
            # The safety check could not run (LLM quota), so the server
            # rightly refused to book. The 2-hour rule cannot be shown
            # without a booking — say so instead of failing.
            skip("booking inside 2 hours succeeds", "red-flag check unavailable (LLM quota)")
            skip("patient cancel inside 2 hours is queued for Staff", "no booking to cancel")
            skip("the appointment stays booked until Staff act", "no booking to cancel")
            skip("a Staff approval request was raised", "no booking to cancel")
            skip("staff can cancel inside 2 hours", "no booking to cancel")
        else:
            check("booking inside 2 hours succeeds", status == 201,
                  near_slot if status == 201 else f"{near_slot} {code(near)} {message(near)}")

            if near_id:
                # Iteration 4 (R1): this used to be a flat 403 "contact Staff".
                # It now queues an approval request and the appointment stays
                # booked until Staff Approve (brief R1 / section 10).
                status, late = call("POST", f"/api/appointments/{near_id}/cancel", {}, token=other_tok)
                check("patient cancel inside 2 hours is queued for Staff",
                      status == 200 and late.get("result") == "to_staff",
                      f"{status} {late.get('result')} {message(late)}")
                check("the appointment stays booked until Staff act",
                      late.get("status") == "booked", str(late.get("status")))
                check("a Staff approval request was raised",
                      bool(late.get("approval_id")), str(late.get("approval_id")))

                status, staff_cancel = call("POST", f"/api/appointments/{near_id}/cancel", {}, token=st_tok)
                check("staff can cancel inside 2 hours",
                      status == 200 and staff_cancel.get("status") == "cancelled")
            else:
                skip("patient cancel inside 2 hours is queued for Staff", "no booking to cancel")
                skip("the appointment stays booked until Staff act", "no booking to cancel")
                skip("a Staff approval request was raised", "no booking to cancel")
                skip("staff can cancel inside 2 hours", "no booking to cancel")

    # (b) Ordinary cancellation — independent of the 2-hour case above.
    if appt_id:
        status, p_cancel = call("POST", f"/api/appointments/{appt_id}/cancel", {}, token=book_tok)
        check("patient cancels own booking",
              status == 200 and p_cancel.get("status") == "cancelled")

        # The freed slot must be bookable by someone else.
        status, rebook = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": book_date, "start_time": book_time,
            "reason": "Routine eye test",
        }, token=other_tok)
        if code(rebook) == "REDFLAG_CHECK_FAILED":
            skip("cancelled slot is free again", "red-flag check unavailable (LLM quota)")
        else:
            check("cancelled slot is free again", status == 201,
                  message(rebook) or f"booked id {rebook.get('id')}")

        status, again = call("POST", f"/api/appointments/{appt_id}/cancel", {}, token=book_tok)
        check("cancelling twice refused", code(again) == "NOT_ACTIVE", message(again))
    else:
        skip("patient cancels own booking", "no booking to cancel")
        skip("cancelled slot is free again", "no booking to cancel")
        skip("cancelling twice refused", "no booking to cancel")

    print("\n--- R2: completed visits ---")
    # Make sure the doctor has something to complete — but only for a patient
    # this suite can sign in as (the follow-up checks need that session).
    known_patients: set[str] = set()
    for tok in (p_tok, other_tok, book_tok):
        if not tok:
            continue
        _, me = call("GET", "/api/me", token=tok)
        if me.get("patient_id"):
            known_patients.add(me["patient_id"])

    status, doc_appts = call("GET", "/api/appointments", token=dr_tok)
    bookable = [
        a for a in doc_appts
        if a["status"] == "booked" and a["patient_id"] in known_patients
    ]
    if not bookable:
        status, fresh = call("POST", "/api/appointments", {
            "doctor_id": "D1", "speciality": "General", "visit_type": "new",
            "date": soon.isoformat(), "start_time": slot_away(listed, book_time, gap=60),
            "reason": "Vision getting blurry",
        }, token=p_tok)
        if status == 201:
            bookable = [fresh]
        elif code(fresh) == "ACTIVE_BOOKING_EXISTS":
            # P01's only active booking is Cataract, so book that instead.
            status, fresh = call("POST", "/api/appointments", {
                "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
                "date": soon.isoformat(), "start_time": slot_away(listed, book_time, gap=60),
                "reason": "Cataract follow-up",
            }, token=p_tok)
            bookable = [fresh] if status == 201 else []
        if not bookable:
            # Last resort: the walk-in patient already in this suite.
            status, fresh = call("POST", "/api/appointments", {
                "doctor_id": "D1", "speciality": "General", "visit_type": "new",
                "date": soon.isoformat(), "start_time": slot_away(listed, book_time, gap=90),
                "reason": "Blurry vision",
            }, token=other_tok)
            bookable = [fresh] if status == 201 else []

    if bookable:
        target = bookable[0]
        # Iteration 2: completing without a note is refused.
        status, no_note = call("POST", f"/api/appointments/{target['id']}/complete",
                               {}, token=dr_tok)
        check("complete without a note refused",
              status == 400 and code(no_note) == "NOTE_REQUIRED", message(no_note))

        status, done = call("POST", f"/api/appointments/{target['id']}/complete",
                            {"note": "Reviewed and advised on drops."},
                            token=dr_tok)
        check("doctor completes a visit with a note",
              status == 200 and done.get("status") == "completed"
              and "Reviewed" in (done.get("doctor_note") or ""),
              done.get("doctor_note", ""))

        status, not_doc = call("POST", f"/api/appointments/{target['id']}/complete",
                               {"note": "Should not work."}, token=st_tok)
        check("staff cannot complete a visit", code(not_doc) == "WRONG_ROLE", message(not_doc))

        status, other_doc = call("POST", f"/api/appointments/{target['id']}/complete",
                                 {"note": "Should not work."}, token=dr2_tok)
        check("another doctor cannot complete it",
              status in (401, 403) and code(other_doc) in {"WRONG_ROLE", "NOT_YOUR_APPOINTMENT"},
              message(other_doc))

        # Refusal first: a follow-up with a doctor who did NOT complete the
        # visit. (Tested before the allowed one so the one-active-per-
        # speciality rule cannot pre-empt it.) The other doctor must cover
        # the speciality — otherwise SPECIALITY_MISMATCH fires first — and
        # be on OPD that day, so pick one dynamically from the doctor list.
        other_token = _token_for(target["patient_id"], p_tok, other_tok)
        wrong_doc: dict | None = None
        for cand in doctors:
            if cand["doctor_id"] == target["doctor_id"]:
                continue
            if target["speciality"] not in cand["specialities"]:
                continue
            _, cand_fu = call("GET", f"/api/slots?doctor_id={cand['doctor_id']}"
                              f"&date={soon}&visit_type=follow_up", token=other_token)
            cand_slots = cand_fu.get("slots") or []
            if not cand_slots:
                continue  # not on OPD that day (surgery / off)
            status, wrong_doc = call("POST", "/api/appointments", {
                "doctor_id": cand["doctor_id"], "speciality": target["speciality"],
                "visit_type": "follow_up", "date": soon.isoformat(),
                "start_time": cand_slots[0],
                "reason": "Follow-up for review",
            }, token=other_token)
            break
        if wrong_doc is None:
            skip("follow-up with a different doctor refused",
                 f"no other {target['speciality']} doctor on OPD {soon}")
        elif code(wrong_doc) == "REDFLAG_CHECK_FAILED":
            skip("follow-up with a different doctor refused",
                 "red-flag check unavailable (LLM quota)")
        else:
            check("follow-up with a different doctor refused",
                  code(wrong_doc) == "FOLLOW_UP_DIFFERENT_DOCTOR", message(wrong_doc))

        # ...and now the legal one: the same doctor who completed the visit.
        status, ok_fu = call("POST", "/api/appointments", {
            "doctor_id": target["doctor_id"], "speciality": target["speciality"],
            "visit_type": "follow_up", "date": soon.isoformat(),
            "start_time": slot_away(listed, target["start_time"], gap=60),
            "reason": "Follow-up after completed visit",
        }, token=_token_for(target["patient_id"], p_tok, other_tok))
        if code(ok_fu) == "REDFLAG_CHECK_FAILED":
            skip("follow-up with the completing doctor allowed",
                 "red-flag check unavailable (LLM quota)")
        else:
            check("follow-up with the completing doctor allowed",
                  status == 201, message(ok_fu) or f"booked id {ok_fu.get('id')}")
    elif code(fresh) == "REDFLAG_CHECK_FAILED":
        skip("doctor had a booking to complete",
             "red-flag check unavailable (LLM quota)")
    else:
        check("doctor had a booking to complete", False, "none found")

    print("\n" + "=" * 70)
    print(f"API checks: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    for s in SKIP:
        print("   SKIP:", s)
    print("=" * 70)
    sys.exit(1 if FAIL else 0)


def _token_for(who: str, *tokens: str) -> str:
    """The token belonging to a patient id (P01...) or doctor id (D1...)."""
    for tok in tokens:
        if not tok:
            continue
        status, me = call("GET", "/api/me", token=tok)
        if status == 200 and who in {me.get("patient_id"), me.get("doctor_id")}:
            return tok
    return tokens[0]


if __name__ == "__main__":
    main()
