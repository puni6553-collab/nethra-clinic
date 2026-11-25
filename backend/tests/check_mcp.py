"""Iteration 6 R1/R2: the NetraCare MCP server, rows M01-M12.

Drives `mcp_scenarios.csv` (evals/datasets) through the real MCP server
over Streamable HTTP, then cross-checks the API for the two things the
pack asks for on top of the tool results: "Booked via MCP" in the Staff
view and an urgent case marked from MCP (R2).

The suite starts its OWN MCP server on port 8011 with a throwaway
database (real clock — the pack's dates are relative to today), so it
never touches the app on :8000/:8001 and can be re-run for ever. Every
booking runs the red-flag check first, so rows SKIP cleanly when the LLM
quota is gone.

Run (no server needed):  venv\\Scripts\\python.exe tests\\check_mcp.py
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# Windows consoles default to cp1252; a reply quoting ₹ would crash a print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
DATASET = PROJECT / "evals" / "datasets" / "mcp_scenarios.csv"
MCP_PORT = 8011
API_PORT = 8012
MCP_BASE = f"http://127.0.0.1:{MCP_PORT}/mcp"
API_BASE = f"http://127.0.0.1:{API_PORT}"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Data pack 6: "If a date lands on a holiday in holidays.csv, use the
# following week." Loaded from the throwaway DB once the server has seeded.
HOLIDAYS: set[str] = set()


def load_holidays(db: Path) -> None:
    import sqlite3
    if not db.exists():
        return
    try:
        con = sqlite3.connect(str(db))
        try:
            HOLIDAYS.clear()
            HOLIDAYS.update(r[0] for r in con.execute(
                "SELECT date FROM holidays"))
        finally:
            con.close()
    except sqlite3.Error:
        pass


def skip_holiday(day: date) -> date:
    """Pack rule: if the day is a holiday, use the same weekday next week."""
    while day.isoformat() in HOLIDAYS:
        day += timedelta(days=7)
    return day


# --- tiny helpers -----------------------------------------------------------

def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {detail}" if detail else ""))


def skip(label, why):
    SKIP.append(label)
    print(f"  SKIP  {label}  -> {why}")


def api_call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method)
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


def port_open(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


# --- the pack's human input, parsed ----------------------------------------

def next_weekday(name: str, today: date | None = None) -> date:
    """The coming `name` — strictly in the future (today never counts)."""
    today = today or date.today()
    target = WEEKDAYS[name.strip().lower()]
    delta = (target - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def resolve_date_expr(expr: str, today: date | None = None) -> date:
    """'next Monday' | 'a Monday more than 30 days from today' | ISO date.

    Relative dates follow the pack's rule: if the day is a clinic holiday,
    use the following week's same weekday (Sundays are skipped too).
    """
    today = today or date.today()
    expr = expr.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expr):
        return date.fromisoformat(expr)
    m = re.match(r"^next\s+(\w+)$", expr, re.I)
    if m:
        if m.group(1).lower() not in WEEKDAYS:
            raise ValueError(f"unknown weekday in {expr!r}")
        return skip_holiday(next_weekday(m.group(1), today))
    m = re.match(r"^a\s+(\w+)\s+more than 30 days from today$", expr, re.I)
    if m:
        day = m.group(1).lower()
        if day not in WEEKDAYS:
            raise ValueError(f"unknown weekday in {expr!r}")
        # First matching weekday strictly more than 30 days out.
        candidate = today + timedelta(days=31)
        while candidate.weekday() != WEEKDAYS[day]:
            candidate += timedelta(days=1)
        return skip_holiday(candidate)
    raise ValueError(f"cannot parse date expression {expr!r}")


def parse_find_slots(inp: str) -> dict:
    """'speciality=Retina, date=next Monday' -> kwargs for find_slots."""
    out: dict = {}
    for part in inp.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    if "date" in out:
        out["date"] = resolve_date_expr(out.pop("date")).isoformat()
    if out.get("date_end"):
        out["date_end"] = resolve_date_expr(out["date_end"]).isoformat()
    return out


_BOOK_RE = re.compile(
    r"^(?P<name>[^,]+),\s*(?P<mobile>\d+),\s*(?P<speciality>[^,]+),\s*"
    r"(?P<doctor_id>[^,]+),\s*(?P<when>.+?),\s*reason:\s*(?P<reason>.+)$",
    re.S,
)


def parse_book(inp: str) -> dict:
    """'Ravi Kumar, 9000000010, Cataract, D1, next Monday 10:00, reason: ...'"""
    m = _BOOK_RE.match(inp.strip())
    if not m:
        raise ValueError(f"cannot parse book_appointment input {inp!r}")
    when = m.group("when").strip()
    # Trailing HH:MM after the date expression.
    tm = re.match(r"^(?P<date_expr>.+?)\s+(?P<time>\d{1,2}:\d{2})$", when)
    if not tm:
        raise ValueError(f"cannot parse when {when!r}")
    day = resolve_date_expr(tm.group("date_expr"))
    return {
        "patient_name": m.group("name").strip(),
        "mobile": m.group("mobile").strip(),
        "speciality": m.group("speciality").strip(),
        "doctor_id": m.group("doctor_id").strip(),
        "date": day.isoformat(),
        "start_time": tm.group("time"),
        "reason": m.group("reason").strip(),
    }


# --- the throwaway MCP server ----------------------------------------------

def start_mcp_server():
    """backend/mcp_server.py on :8011 with its own database (real clock)."""
    db = Path(tempfile.gettempdir()) / "netracare_mcp_check.db"
    if db.exists():
        db.unlink()
    log = Path(tempfile.gettempdir()) / "netracare_mcp_check.log"
    env = dict(os.environ)
    env.update({
        "NETRACARE_DB": str(db),
        "MCP_PORT": str(MCP_PORT),
        "SEED_APPOINTMENTS": "0",
        "LLM_MODEL": env.get("LLM_MODEL") or "gemini-3.5-flash-lite",
    })
    out = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "mcp_server.py")],
        cwd=str(PROJECT), env=env, stdout=out, stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        if proc.poll() is not None:
            break
        if port_open(MCP_PORT):
            return proc, db, log
        time.sleep(0.5)
    proc.poll()
    return proc, db, log


def start_api_server(db: Path):
    """uvicorn on :8012 against the SAME database the MCP server writes."""
    log = Path(tempfile.gettempdir()) / "netracare_mcp_check_api.log"
    env = dict(os.environ)
    env.update({
        "NETRACARE_DB": str(db),
        "SEED_APPOINTMENTS": "0",
        "LLM_MODEL": env.get("LLM_MODEL") or "gemini-3.5-flash-lite",
    })
    out = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(API_PORT)],
        cwd=str(BACKEND), env=env, stdout=out, stderr=subprocess.STDOUT,
    )
    for _ in range(80):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(API_BASE + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return proc, log
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return proc, log


# --- MCP client -------------------------------------------------------------

async def run_scenarios(rows: list[dict]) -> list[dict]:
    """Play every pack row through one MCP session, in order."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    results: list[dict] = []
    async with streamablehttp_client(MCP_BASE) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            results.append({"_tools": tools.tools})

            for row in rows:
                sid, tool = row["scenario_id"], row["tool"]
                entry = {"id": sid, "tool": tool, "expected": row["expected"],
                         "reason_code": row["reason_code"], "note": row["note"]}
                try:
                    if tool == "find_slots":
                        args = parse_find_slots(row["input"])
                    else:
                        args = parse_book(row["input"])
                except ValueError as exc:
                    entry["error"] = f"parse: {exc}"
                    results.append(entry)
                    continue
                try:
                    call = await session.call_tool(tool, args)
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"call: {exc}"
                    results.append(entry)
                    continue
                text = ""
                for block in call.content or []:
                    text = getattr(block, "text", "") or text
                try:
                    entry["result"] = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    entry["result"] = {"raw": text}
                entry["is_error"] = bool(call.isError)
                results.append(entry)
                # One bad row never stops the run (brief R3).
    return results


def llm_quota(result: dict) -> bool:
    """The red-flag gate itself failed (quota) — not a scenario failure."""
    return (
        result.get("status") == "refused"
        and result.get("code") == "REDFLAG_CHECK_FAILED"
    )


# --- API cross-checks (R2) --------------------------------------------------

def staff_token() -> str | None:
    status, body = api_call("POST", "/api/login", {"role": "staff", "name": "Reception"})
    return body.get("token") if status == 200 else None


def patient_token(mobile: str, name: str) -> str | None:
    status, body = api_call(
        "POST", "/api/login", {"role": "patient", "mobile": mobile, "name": name})
    return body.get("token") if status == 200 else None


def main() -> int:
    if not DATASET.exists():
        print(f"missing dataset: {DATASET}")
        return 1

    with open(DATASET, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"mcp scenarios: {len(rows)} rows from {DATASET.name}")

    if port_open(MCP_PORT):
        print(f"  note: something already listens on :{MCP_PORT}; not starting ours")
        mcp_proc = None
        db = Path(tempfile.gettempdir()) / "netracare_mcp_check.db"
    else:
        mcp_proc, db, mcp_log = start_mcp_server()
        if not port_open(MCP_PORT):
            print(f"  FAIL  MCP server did not start (see {mcp_log})")
            if mcp_proc:
                mcp_proc.terminate()
            return 1
    # Holidays drive the pack's relative dates ("use the following week").
    load_holidays(db)

    api_proc = None
    try:
        if not port_open(API_PORT):
            api_proc, api_log = start_api_server(db)
            if not port_open(API_PORT):
                print(f"  FAIL  API server did not start (see {api_log})")
                return 1

        # --- list tools (Inspector's first check) --------------------------
        async def list_tools():
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(MCP_BASE) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return (await session.list_tools()).tools

        tools = asyncio.run(list_tools())
        names = sorted(t.name for t in tools)
        check("MCP Inspector lists 2 tools",
              names == ["book_appointment", "find_slots"], str(names))
        described = all((t.description or "").strip() for t in tools)
        check("both tools carry a description", described)

        # --- the pack rows, in order ---------------------------------------
        results = asyncio.run(run_scenarios(rows))
        tool_list = results.pop(0).get("_tools", [])

        booked_ids: list[int] = []
        quota_hit = False
        for entry in results:
            sid = entry["id"]
            if entry.get("error"):
                check(f"{sid} {entry['tool']}", False, entry["error"])
                continue
            if entry.get("is_error"):
                check(f"{sid} {entry['tool']}", False,
                      str(entry.get("result"))[:160])
                continue
            result = entry.get("result") or {}
            status = str(result.get("status") or "")
            expected = entry["expected"]
            code_want = (entry["reason_code"] or "").strip()

            if expected == "allowed" and entry["tool"] == "find_slots":
                # M01 has slots; M02 is allowed with a clear empty message.
                if sid == "M02":
                    ok = (status == "ok" and not result.get("slots")
                          and (result.get("message") or "").strip())
                    check(f"{sid} find_slots clear empty-day message", ok,
                          f"status={status} count={result.get('count')} "
                          f"msg={(result.get('message') or '')[:100]!r}")
                else:
                    ok = status == "ok" and (result.get("count") or 0) > 0
                    check(f"{sid} find_slots returns free slots", ok,
                          f"status={status} count={result.get('count')}")
                continue

            if llm_quota(result):
                quota_hit = True
                skip(f"{sid} {expected}", "REDFLAG_CHECK_FAILED (LLM quota)")
                continue
            if quota_hit and expected in ("allowed", "red_flag"):
                # Bookings after a quota hit cannot be judged fairly.
                skip(f"{sid} {expected}", "LLM quota already exhausted this run")
                continue
            # M04/M09 only make sense if M03 actually booked.
            if sid in ("M04", "M09") and not booked_ids:
                skip(f"{sid} {expected}", "M03 did not book (dependency)")
                continue

            if expected == "allowed":
                ok = status == "booked"
                appt = result.get("appointment") or {}
                if ok and appt.get("id") is not None:
                    booked_ids.append(int(appt["id"]))
                check(f"{sid} booked", ok,
                      f"status={status} code={result.get('code')} "
                      f"msg={(result.get('message') or '')[:120]!r}")
            elif expected == "refused":
                ok = status == "refused" and result.get("code") == code_want
                check(f"{sid} refused {code_want}", ok,
                      f"status={status} code={result.get('code')} "
                      f"msg={(result.get('message') or '')[:120]!r}")
            elif expected == "red_flag":
                from app import config as app_config
                emergency = app_config.EMERGENCY_MESSAGE
                got = result.get("emergency_message") or ""
                ok = (status == "red_flag"
                      and result.get("code") == "RED_FLAG"
                      and got == emergency)
                check(f"{sid} red_flag + emergency word for word", ok,
                      f"status={status} code={result.get('code')} "
                      f"emergency_match={got == emergency}")
                # R2: the urgent case is saved, marked from MCP.
                st, cases = api_call("GET", "/api/urgent-cases",
                                     token=staff_token())
                unseen = [c for c in cases if not c.get("seen")] if st == 200 else []
                top = unseen[0] if unseen else {}
                ok = bool(top) and top.get("source") == "mcp"
                check("M12 urgent case from MCP is on top of Staff view", ok,
                      f"top={ {k: top.get(k) for k in ('source', 'patient_name', 'seen')} }")
            else:
                check(f"{sid} {expected}", False, f"unknown expected {expected!r}")

        # --- R2: Booked via MCP, and the patient sees it -------------------
        tok = staff_token()
        if not tok:
            skip("Staff view shows Booked via MCP", "staff login failed")
        else:
            status, appts = api_call(
                "GET", "/api/appointments?status=booked", token=tok)
            via = {a["id"]: a.get("booked_via") for a in appts} if status == 200 else {}
            # M03 is the pack's own booking (first allowed book row).
            m03 = next((e for e in results if e["id"] == "M03"), None)
            m03_id = ((m03 or {}).get("result") or {}).get("appointment", {}).get("id")
            if m03_id:
                ok = via.get(int(m03_id)) == "MCP"
                check("M03 shows Booked via MCP in the Staff view", ok,
                      f"id={m03_id} via={via.get(int(m03_id))!r}")
                ptok = patient_token("9000000010", "Ravi Kumar")
                if not ptok:
                    skip("Ravi Kumar sees the M03 booking", "patient login failed")
                else:
                    # The token scopes the list to his own rows.
                    status, mine = api_call("GET", "/api/appointments", token=ptok)
                    ids = {a["id"] for a in mine} if status == 200 else set()
                    check("Ravi Kumar sees the M03 booking when he logs in",
                          int(m03_id) in ids,
                          f"his_ids={sorted(ids)} want={m03_id}")
            else:
                skip("M03 Booked via MCP cross-checks", "M03 did not book")

        # --- the tool list also proved the descriptions exist --------------
        if tool_list:
            desc_ok = all((t.description or "").strip() for t in tool_list)
            check("session tool list carries descriptions", desc_ok)

    finally:
        for proc in (api_proc, mcp_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    print(f"\nmcp checks: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print("   FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    # Tool results import app.config for the emergency wording.
    sys.path.insert(0, str(BACKEND))
    sys.exit(main())
