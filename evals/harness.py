"""Shared plumbing for the iteration 5 eval runner (handout R3, R5).

One process runs the suites. `configure()` must be called before anything
imports `app.*`: it fixes the clinic clock, points the process at the eval
database and puts `backend/` on sys.path.

Two ways a row runs:

* **through the app** — the chat sets are played over HTTP against a
  throwaway uvicorn on :8012 with its own database, exactly as a patient
  would use the site. `EvalServer` starts and stops it.
* **in-process** — the booking-reason red flags, the policy answers and the
  doctor summaries call the app's own functions directly, on the in-process
  eval database, because the handout asks for their sources/chunks too.

Nothing here ever touches the real database on backend/netracare.db or the
server you run on :8000.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]   # project root
BACKEND = ROOT / "backend"
EVALS = ROOT / "evals"
DATASETS = EVALS / "datasets"
RESULTS = EVALS / "results"

PORT = 8012
BASE = f"http://127.0.0.1:{PORT}"
CLOCK = "2026-11-02T10:00:00"     # data pack 4's fixed clock (a Monday)

INPROCESS_DB = RESULTS / "_eval.db"        # summary suite (in-process)
CHAT_DB = RESULTS / "_eval_chat.db"        # one server for the chat sets
BOOKING_DB = RESULTS / "_eval_booking.db"  # rebuilt for every scenario

P20_MOBILE = None  # looked up from the seeded patient directory at run time


def configure() -> None:
    """Fix the eval environment BEFORE any `app` import.

    Mirrors what check_leave/verify_rules do, plus the eval project name so
    every trace the run produces lands in netracare-evals (iteration 5 R1).
    """
    RESULTS.mkdir(parents=True, exist_ok=True)
    os.environ["APP_NOW"] = CLOCK
    os.environ["SEED_APPOINTMENTS"] = "1"       # pack 1's rows, as pack 4 README says
    os.environ["OPIK_PROJECT_NAME"] = "netracare-evals"
    os.environ["NETRACARE_DB"] = str(INPROCESS_DB)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))


# --- datasets ---------------------------------------------------------------

def load_rows(name: str, large: bool = False) -> list[dict]:
    """One golden set. `name` is the file stem in evals/datasets/."""
    path = DATASETS / (f"{name}-large.csv" if large else f"{name}.csv")
    if not path.exists():
        raise FileNotFoundError(f"golden set not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def size_label(large: bool) -> str:
    return "large" if large else "small"


# --- HTTP -------------------------------------------------------------------

def call(method: str, path: str, body=None, token: str | None = None,
         base: str = BASE, timeout: float = 300.0):
    """One HTTP call. Returns (status, parsed_body)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001 — a dead server is a row error, not a crash
        return 0, {"error": str(e)}


def login(base: str, role: str, *, mobile: str | None = None, name: str = "",
          doctor_id: str | None = None) -> str | None:
    body: dict = {"role": role, "name": name}
    if mobile:
        body["mobile"] = mobile
    if doctor_id:
        body["doctor_id"] = doctor_id
    status, sess = call("POST", "/api/login", body, base=base)
    return sess.get("token") if status == 200 else None


def patient_by_id(base: str, staff_token: str, patient_id: str) -> dict | None:
    """The patient row from the directory (login needs the mobile)."""
    status, rows = call("GET", "/api/patients", token=staff_token, base=base)
    if status != 200 or not isinstance(rows, list):
        return None
    for row in rows:
        if row.get("patient_id") == patient_id:
            return row
    return None


def patient_token(base: str, staff_token: str, patient_id: str) -> str | None:
    person = patient_by_id(base, staff_token, patient_id)
    if not person:
        return None
    return login(base, "patient", mobile=person["mobile"], name=person["name"])


def chat(token: str, *, message: str | None = None, thread: str | None = None,
         confirm: bool | None = None, base: str = BASE) -> tuple[int, dict, float]:
    """One POST /api/chat turn. Returns (status, body, latency_ms)."""
    body: dict = {"thread_id": thread or uuid4().hex}
    if confirm is not None:
        body["confirm"] = confirm
    else:
        body["message"] = message if message is not None else ""
    started = time.perf_counter()
    status, out = call("POST", "/api/chat", body, token=token, base=base)
    return status, out, (time.perf_counter() - started) * 1000.0


def llm_down(body: dict) -> bool:
    """The model could not be reached (quota/timeout): a row error, not a verdict."""
    return isinstance(body, dict) and body.get("type") in ("check_failed", "unavailable")


def booked_ids(base: str, token: str, patient_id: str) -> set[int]:
    status, rows = call("GET", f"/api/appointments?patient_id={patient_id}",
                        token=token, base=base)
    if status != 200 or not isinstance(rows, list):
        return set()
    return {int(r["id"]) for r in rows if r.get("status") == "booked"}


# --- the throwaway servers --------------------------------------------------

class EvalServer:
    """uvicorn with its own database and the fixed clock.

    No --reload (it forks a child that can outlive the parent and keep the
    port — AGENTS trap 2), and `stop()` waits for the process to be gone.
    """

    def __init__(self, db: Path, port: int = PORT):
        self.db = Path(db)
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.log = RESULTS / f"_server_{port}.log"

    def start(self, *, seed_appointments: bool = True) -> "EvalServer":
        self.stop()
        if self.db.exists():
            # Windows releases the dead server's file handle a moment after
            # wait() returns; retry the unlink instead of failing the row.
            deadline = time.time() + 15
            while True:
                try:
                    self.db.unlink()
                    break
                except OSError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.25)
        env = dict(os.environ)
        env.update({
            "NETRACARE_DB": str(self.db),
            "APP_NOW": CLOCK,
            "SEED_APPOINTMENTS": "1" if seed_appointments else "0",
            "OPIK_PROJECT_NAME": "netracare-evals",
        })
        out = open(self.log, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=str(BACKEND), env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        for _ in range(120):
            if self.proc.poll() is not None:
                break
            status, _ = call("GET", "/api/health", base=self.base, timeout=3)
            if status == 200:
                return self
            time.sleep(0.5)
        tail = ""
        try:
            tail = self.log.read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            pass
        raise RuntimeError(f"eval server on :{self.port} did not start\n{tail}")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.proc = None


# --- in-process eval database ----------------------------------------------

def rebuild_inprocess_db() -> None:
    """Fresh eval database for the in-process suites (summary).

    The engine is created at import from config.DB_PATH, which `configure()`
    pointed at INPROCESS_DB — so this only ever touches that file.
    """
    from app import seed
    from app.database import Session, engine, init_db

    engine.dispose()
    if INPROCESS_DB.exists():
        INPROCESS_DB.unlink()
    init_db()
    with Session(engine) as db:
        seed.load_data(db)


def seed_id_map() -> dict[str, int]:
    """Pack 1's A001..A112 names -> the integer ids the database assigns.

    Row N of the seed table becomes row N of the appointments table (both
    start at 1), exactly as verify_rules.py bridges them.
    """
    import sqlite3

    from app import config

    source = next((p for p in config.SOURCE_SQLITE if os.path.exists(p)), None)
    if not source:
        return {}
    con = sqlite3.connect(source)
    try:
        rows = con.execute("SELECT * FROM appointments_seed").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    if not rows:
        return {}
    header = [str(c) for c in rows[0]]
    if "appointment_id" not in header:
        return {}
    col = header.index("appointment_id")
    return {str(r[col]): i + 1 for i, r in enumerate(rows[1:])}


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(ROOT), capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return round(ordered[idx], 1)
