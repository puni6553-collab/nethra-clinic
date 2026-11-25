"""End-to-end smoke test for the patient chat — the iteration 4 agent.

Every row goes through POST /api/chat with a thread_id (handout R4/R5).
Rows that need the model SKIP when the LLM quota is gone, in the same
spirit as check_redflag.
"""
import json
import sys
import urllib.request
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
    """One POST /api/chat. Returns (status, body) — body carries thread_id."""
    body = {"thread_id": thread or uuid4().hex}
    if confirm is not None:
        body["confirm"] = confirm
    else:
        body["message"] = message if message is not None else ""
    return call("POST", "/api/chat", body, token=token)


def llm_down(body) -> bool:
    """The chat could not reach the model (quota) — server answers gracefully."""
    return isinstance(body, dict) and body.get("type") in ("check_failed", "unavailable")


def main():
    status, sess = call("POST", "/api/login", {"role": "patient", "mobile": "9000000001", "name": "Asha Verma"})
    tok = sess.get("token")
    check("patient login", status == 200 and tok, str(status))
    if not tok:
        return 1

    # Role check: staff cannot chat.
    status, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    stok = staff.get("token")
    status, body = call("POST", "/api/chat", {"message": "hello"}, token=stok)
    check("staff cannot chat", status == 403, f"{status} {body}")

    # Regular answer with sources (the agent calls search_clinic_docs).
    status, body = chat(tok, message="What time does the clinic open on Saturday?")
    if llm_down(body):
        skip("Q01 answer with sources", f"LLM unavailable ({body.get('type')})")
    else:
        ok = status == 200 and body.get("type") == "answer" and body.get("sources")
        check("Q01 answer with sources", ok, f"{status} {body.get('type')} {body.get('sources')}")
        print("      text:", (body.get("message") or "")[:160])

    # Follow-up in the SAME thread — conversation memory replaces the
    # iteration 3 rewrite step (handout R4).
    thread = body.get("thread_id") or uuid4().hex
    status, body2 = chat(tok, thread=thread, message="What should I avoid after it?")
    if llm_down(body2):
        skip("follow-up in same thread answers", f"LLM unavailable ({body2.get('type')})")
    else:
        ok = status == 200 and body2.get("type") == "answer" and body2.get("sources")
        check("follow-up in same thread answers", ok,
              f"{status} {body2.get('type')} {body2.get('sources')}")
        print("      text:", (body2.get("message") or "")[:200])

    # I don't know (Q16 parking) — the fixed wording, no sources (R6).
    status, body3 = chat(tok, message="Is there parking at the clinic?")
    if llm_down(body3):
        skip("Q16 -> I don't know", f"LLM unavailable ({body3.get('type')})")
    else:
        check("Q16 -> I don't know",
              status == 200 and body3.get("type") == "idk" and not body3.get("sources"),
              f"{status} {body3.get('type')}")

    # Booking request: the agent now books — after the Confirm card (R5).
    status, body4 = chat(tok, message="Can you book me with Dr. Shah tomorrow at 10?")
    thread4 = body4.get("thread_id") or uuid4().hex
    if llm_down(body4):
        skip("booking request books after Confirm", f"LLM unavailable ({body4.get('type')})")
    else:
        check("booking reply is a card or an explanation",
              status == 200 and body4.get("type") == "answer",
              f"{status} {body4.get('type')} pending={bool(body4.get('pending_action'))}")
        if body4.get("pending_action"):
            print("      card:", body4["pending_action"])
            status, body4b = chat(tok, thread=thread4, confirm=True)
            check("Confirm runs the booking",
                  status == 200 and body4b.get("type") == "answer"
                  and not body4b.get("pending_action"),
                  f"{status} {body4b.get('message', '')[:120]}")
            print("      text:", (body4b.get("message") or "")[:200])

    # Red flag -> emergency + urgent case from the chat (R6).
    status, body5 = chat(tok, message="A curtain came down over my left eye an hour ago")
    if llm_down(body5) and body5.get("type") == "check_failed":
        skip("red flag -> emergency", "red-flag check unavailable (LLM quota)")
    else:
        check("red flag -> emergency",
              status == 200 and body5.get("type") == "emergency"
              and "urgent attention" in (body5.get("message") or ""),
              f"{status} {body5.get('type')}")

    status, cases = call("GET", "/api/urgent-cases", token=stok)
    top = cases[0] if cases else {}
    check("urgent case from chat on top",
          status == 200 and top.get("source") == "chat"
          and "curtain" in (top.get("reason") or "").lower(),
          f"{status} source={top.get('source')}")

    # Missing message.
    status, body6 = chat(tok, message="  ")
    check("empty message refused",
          status == 400 and body6.get("detail", {}).get("code") == "MISSING_MESSAGE",
          str(status))

    # Staff search debug (unchanged since iteration 3).
    from urllib.parse import quote
    status, dbg = call("GET", f"/api/staff/search-debug?q={quote('Should I bring my HbA1c report?')}", token=stok)
    kw = dbg.get("keyword") or []
    hy = dbg.get("hybrid") or []
    check("debug: keyword ranks Diabetic eye check first",
          status == 200 and kw and kw[0].get("section") == "Diabetic eye check",
          f"{status} {kw[0].get('section') if kw else None}")
    check("debug: hybrid has it top 4",
          any(h.get("section") == "Diabetic eye check" for h in hy), str(len(hy)))

    status, dbg2 = call("GET",
                        "/api/staff/search-debug?q=" + quote("Can my kid have breakfast on the morning of the operation to straighten his eye?"),
                        token=stok)
    vec = dbg2.get("vector") or []
    hy2 = dbg2.get("hybrid") or []
    check("debug: vector finds Squint surgery top 4",
          status == 200 and any(v.get("section") == "Squint surgery" for v in vec),
          f"{status} {[v.get('section') for v in vec]}")
    check("debug: hybrid has Squint surgery top 4",
          status == 200 and any(h.get("section") == "Squint surgery" for h in hy2))

    # Patient cannot open the debug view.
    status, body7 = call("GET", "/api/staff/search-debug?q=x", token=tok)
    check("patient cannot use search debug", status == 403, str(status))

    print(f"\nchat smoke: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
