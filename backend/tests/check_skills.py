"""Iteration 6 R4/R5: skill triggers — rows of skill_triggers.csv.

Logs in as the pack's patient, sends one message on a fresh thread, and
checks the `skills` list the chat route returns (the "Skill: name" line).
The pack's bar: at least 14 of the 16 rows load the expected skill, or
none. T07/T08 get a booked appointment first, as the pack says.

Needs a running API server on :8000. Every row runs the red-flag check
and the agent, so rows SKIP when the LLM quota is gone (same convention
as check_agent).

Run:  venv\\Scripts\\python.exe tests\\check_skills.py   (server on :8000)
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

# Windows consoles default to cp1252; a reply quoting ₹ would crash a print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(__file__).resolve().parents[2]
DATASET = PROJECT / "evals" / "datasets" / "skill_triggers.csv"
BASE = "http://127.0.0.1:8000"
PASS, FAIL, SKIP = [], [], []
BAR = 14  # of 16, per data pack 6 / brief R4


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
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  SKIP  {name}  -> {why}")


def llm_down(body):
    return isinstance(body, dict) and body.get("type") in ("check_failed", "unavailable")


def login(patient_id: str) -> str | None:
    """Look the pack patient up by id, then log in with mobile + name."""
    # Staff can list the directory; patients log in with mobile + name.
    _, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    status, patients = call("GET", "/api/patients", token=staff.get("token"))
    if status != 200:
        return None
    row = next((p for p in patients if p.get("patient_id") == patient_id), None)
    if not row:
        return None
    status, sess = call("POST", "/api/login", {
        "role": "patient", "mobile": row["mobile"], "name": row["name"]})
    return sess.get("token") if status == 200 else None


def has_booked_appointment(tok: str, speciality: str | None = None) -> bool:
    status, rows = call("GET", "/api/appointments", token=tok)
    if status != 200:
        return False
    for r in rows:
        if r.get("status") != "booked":
            continue
        if speciality is None or r.get("speciality") == speciality:
            return True
    return False


def ensure_appointment(tok: str, speciality: str | None = None) -> bool:
    """Book one real slot if the pack patient has none (T07/T08 setup)."""
    if has_booked_appointment(tok, speciality):
        return True
    # Any doctor covering the speciality (or any doctor when unspecified).
    _, doctors = call("GET", "/api/doctors", token=tok)
    if not isinstance(doctors, list):
        return False
    candidates = []
    for d in doctors:
        raw = d.get("specialities") or []
        specs = ([s.strip() for s in raw] if isinstance(raw, list)
                 else [s.strip() for s in raw.split(";") if s.strip()])
        if speciality is None:
            candidates.append((d["doctor_id"], specs[0] if specs else "General"))
        elif speciality in specs:
            candidates.append((d["doctor_id"], speciality))
    today = date.today()
    for doctor_id, spec in candidates:
        for i in range(1, 28):
            day = (today + timedelta(days=i)).isoformat()
            status, body = call(
                "GET",
                f"/api/slots?doctor_id={doctor_id}&date={day}&visit_type=new",
                token=tok)
            if status == 200 and body.get("slots"):
                status, out = call("POST", "/api/appointments", {
                    "doctor_id": doctor_id,
                    "speciality": spec,
                    "visit_type": "new",
                    "date": day,
                    "start_time": body["slots"][0],
                    "reason": "Routine check-up before my visit.",
                }, token=tok)
                if status in (200, 201) and out.get("id"):
                    return True
                # Quota / rule refused — try the next slot.
    return False


def expected_set(row: dict) -> set[str]:
    want = (row.get("expected_skill") or "none").strip() or "none"
    also = (row.get("also_acceptable") or "").strip()
    out = {want}
    if also and also != "none":
        out.update(a.strip() for a in also.split(";") if a.strip())
    return out


def main() -> int:
    if not DATASET.exists():
        print(f"missing dataset: {DATASET}")
        return 1
    with open(DATASET, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"skill triggers: {len(rows)} rows from {DATASET.name}")

    # One health probe: is the server up at all?
    status, _ = call("GET", "/api/health")
    if status != 200:
        print("  FAIL  no server on :8000 — start it first (python run.py)")
        return 1

    judged = 0
    matched = 0
    for row in rows:
        tid = row["trigger_id"]
        patient_id = row["patient_id"]
        message = row["message"]
        want = expected_set(row)
        want_label = row.get("expected_skill") or "none"

        tok = login(patient_id)
        if not tok:
            skip(f"{tid} -> {want_label}", f"cannot log in as {patient_id}")
            continue

        # T07/T08: the pack wants a booked appointment first.
        if tid in ("T07", "T08"):
            speciality = "Glaucoma" if tid == "T08" else None
            if not ensure_appointment(tok, speciality):
                # Booking itself needs the LLM; treat quota as a skip.
                skip(f"{tid} -> {want_label}",
                     "could not book the setup appointment (quota or no slot)")
                continue

        status, body = call("POST", "/api/chat", {
            "thread_id": uuid4().hex, "message": message}, token=tok)
        if status != 200 or llm_down(body):
            why = body.get("type") or body.get("detail") or status
            skip(f"{tid} -> {want_label}", f"LLM unavailable ({why})")
            continue

        # T16 is a red flag: the emergency reply comes first, no skill.
        loaded = set(body.get("skills") or [])
        judged += 1

        if want_label == "none" and "also_acceptable" not in row:
            ok = not loaded
            detail = f"loaded={sorted(loaded) or '[]'} type={body.get('type')}"
        else:
            # `want` may be {"none", <skill>}: an empty load matches "none",
            # a load matching also_acceptable matches the skill (T06).
            others = want - {"none"}
            ok = bool(loaded & others) or ("none" in want and not loaded)
            detail = (f"loaded={sorted(loaded)} want={sorted(want)} "
                      f"type={body.get('type')} "
                      f"msg={(body.get('message') or '')[:80]!r}")

        # T16: emergency, and specifically no skill.
        if tid == "T16":
            ok = body.get("type") == "emergency" and not loaded
            detail = f"type={body.get('type')} loaded={sorted(loaded)}"

        if ok:
            matched += 1
        check(f"{tid} skill={want_label}", ok, detail)

    # The pack's bar: >= 14 of 16 (skipped rows count against it, as the
    # brief's "16 rows" does — but a quota-short run is reported honestly).
    total = len(rows)
    if judged == 0:
        skip("skill trigger bar >= 14/16", "no rows judged (LLM quota?)")
    else:
        check(f"skill trigger bar >= {BAR}/{total}",
              matched >= BAR,
              f"matched {matched} of {judged} judged "
              f"({total - judged} skipped)")

    print(f"\nskill checks: {len(PASS)} passed, {len(FAIL)} failed, "
          f"{len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
