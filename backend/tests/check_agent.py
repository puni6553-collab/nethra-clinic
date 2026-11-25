"""Iteration 4 agent checks: confirmation (R5), memory threads, the API's
rules holding when the chat can write (R2/R3), and the "other patient's
appointment" safety check.

Needs a running server. Every row that needs the model SKIPs when the LLM
quota is gone (same convention as check_redflag).
"""
import json
import sys
import urllib.request
from datetime import date, timedelta
from uuid import uuid4

BASE = "http://127.0.0.1:8000"
PASS, FAIL, SKIP = [], [], []


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  SKIP  {name}  -> {why}")


def chat(token, *, message=None, thread=None, confirm=None):
    body = {"thread_id": thread or uuid4().hex}
    if confirm is not None:
        body["confirm"] = confirm
    else:
        body["message"] = message if message is not None else ""
    return call("POST", "/api/chat", body, token=token)


def llm_down(body):
    return isinstance(body, dict) and body.get("type") in ("check_failed", "unavailable")


def booked_ids(tok, patient="P01"):
    status, rows = call("GET", "/api/appointments?patient_id=" + patient, token=tok)
    if status != 200:
        return set()
    return {r["id"] for r in rows if r.get("status") == "booked"}


def find_free_slot(tok, doctor_id="D1", days=25):
    """First free start for a new consultation with this doctor (GET /api/slots)."""
    today = date.today()
    for i in range(1, days + 1):
        d = (today + timedelta(days=i)).isoformat()
        status, body = call(
            "GET", f"/api/slots?doctor_id={doctor_id}&date={d}&visit_type=new", token=tok)
        if status == 200 and body.get("slots"):
            return d, body["slots"][0]
    return None, None


def other_patients_booked(staff_tok):
    """A booked appointment that belongs to somebody else (for the R2 check).

    A patient can only list their own rows, so the lookup runs with the
    Staff token — the patient still only ever types the id.
    """
    status, rows = call("GET", "/api/appointments?status=booked", token=staff_tok)
    if status != 200:
        return None
    for r in rows:
        if r.get("patient_id") != "P01":
            return r
    return None


def p01_has_cataract(tok):
    status, rows = call("GET", "/api/appointments?patient_id=P01", token=tok)
    return status == 200 and any(
        r.get("status") == "booked" and r.get("speciality") == "Cataract" for r in rows
    )


def main():
    status, sess = call("POST", "/api/login", {"role": "patient", "mobile": "9000000001", "name": "Asha Verma"})
    tok = sess.get("token")
    check("patient login", status == 200 and tok, str(status))
    if not tok:
        return 1
    _, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    staff_tok = staff.get("token")

    slot_date, slot_time = find_free_slot(tok)
    if not slot_date:
        skip("confirmation flow", "no free D1 slot found in the next 25 days")
    else:
        reason_msg = (
            f"Book me a General new consultation with Dr. Rao on {slot_date} "
            f"at {slot_time}. Reason: blurry screens after work."
        )

        # --- R5: nothing changes until Confirm ---------------------------
        before = booked_ids(tok)
        status, body = chat(tok, message=reason_msg)
        thread = body.get("thread_id") or uuid4().hex
        if llm_down(body):
            skip("book asks for confirmation", f"LLM unavailable ({body.get('type')})")
        else:
            check("book shows a confirmation card",
                  status == 200 and bool(body.get("pending_action")),
                  f"{status} pending={body.get('pending_action')!r} "
                  f"msg={(body.get('message') or '')[:80]!r}")
            print("      card:", body.get("pending_action"))
            check("nothing booked before Confirm", booked_ids(tok) == before)

            status, res = chat(tok, thread=thread, confirm=True)
            after = booked_ids(tok)
            ok = status == 200 and not res.get("pending_action") and after - before
            check("Confirm books the appointment", ok,
                  f"{status} new_ids={sorted(after - before)} "
                  f"msg={(res.get('message') or '')[:120]!r}")

            # Clean up: cancel whatever this row created.
            for appt_id in sorted(after - before):
                call("POST", f"/api/appointments/{appt_id}/cancel", {}, token=tok)

        # --- R5: No changes nothing, agent asks what instead -------------
        before = booked_ids(tok)
        status, body = chat(tok, message=reason_msg)
        thread = body.get("thread_id") or uuid4().hex
        if llm_down(body):
            skip("No declines the card", f"LLM unavailable ({body.get('type')})")
        elif not body.get("pending_action"):
            check("No declines the card", False,
                  f"no card came back: {(body.get('message') or '')[:120]!r}")
        else:
            status, res = chat(tok, thread=thread, confirm=False)
            check("No leaves the database untouched",
                  status == 200 and booked_ids(tok) == before
                  and not res.get("pending_action"),
                  f"{status} msg={(res.get('message') or '')[:120]!r}")

            # --- R5: a typed message while a card waits counts as No ------
            status, body = chat(tok, message=reason_msg)
            thread = body.get("thread_id") or uuid4().hex
            if llm_down(body):
                skip("typed message while pending counts as No",
                     f"LLM unavailable ({body.get('type')})")
            elif not body.get("pending_action"):
                check("typed message while pending counts as No", False,
                      f"no card came back: {(body.get('message') or '')[:120]!r}")
            else:
                status, res = chat(tok, thread=thread,
                                   message="No thanks — just tell me what time the clinic opens.")
                check("typed message while pending counts as No",
                      status == 200 and not res.get("pending_action")
                      and booked_ids(tok) == before,
                      f"{status} type={res.get('type')} "
                      f"msg={(res.get('message') or '')[:100]!r}")

    # --- R2/R5: another patient's appointment: refused, and no card -----
    other = other_patients_booked(staff_tok)
    if not other:
        skip("another patient's appointment", "no other patient has a booked appointment")
    else:
        status, body = chat(tok, message=f"Cancel appointment {other['id']}.")
        if llm_down(body):
            skip("another patient's appointment", f"LLM unavailable ({body.get('type')})")
        else:
            reply = body.get("message") or ""
            still = other_patients_booked(staff_tok)
            check("no confirmation card for someone else's appointment",
                  status == 200 and not body.get("pending_action"),
                  f"{status} pending={body.get('pending_action')!r}")
            check("the appointment is unchanged and unmentioned",
                  still is not None and still.get("id") == other["id"]
                  and other["date"] not in reply,
                  f"still={bool(still)} reply={reply[:120]!r}")

    # --- R3: a duplicate speciality is refused, DB unchanged ------------
    if not p01_has_cataract(tok):
        skip("duplicate speciality refused", "P01 has no booked Cataract appointment")
    else:
        before = booked_ids(tok)
        status, body = chat(
            tok,
            message="Book me another Cataract appointment with Dr. Rao next week. "
                    "Reason: I would like a second opinion.",
        )
        thread = body.get("thread_id") or uuid4().hex
        if llm_down(body):
            skip("duplicate speciality refused", f"LLM unavailable ({body.get('type')})")
        else:
            if body.get("pending_action"):
                status, body = chat(tok, thread=thread, confirm=True)
            check("duplicate speciality: the rules engine refuses",
                  status == 200 and booked_ids(tok) == before,
                  f"{status} msg={(body.get('message') or '')[:140]!r}")

    print(f"\nagent checks: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
