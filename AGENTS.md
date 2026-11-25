# AGENTS.md

Instructions for AI coding agents working in this repository.

## What this is

NetraCare — a booking site for a fictional eye clinic. FastAPI + SQLModel
backend, React + Vite frontend, three LLM features (red-flag safety gate,
Fact of the Day, doctor summary), a RAG patient chat over the clinic
documents, a booking agent (iteration 4), an eval harness with LLM judges
(iteration 5) and MCP servers, a partner lab and skills (iteration 6).
All data is synthetic.

The project is graded against a written brief (iterations 1–6). Brief
wording and numbers are load-bearing: see **Conventions** below.

## Commands

Run everything from the project root unless stated otherwise.

```powershell
# install (single requirements.txt at the project root)
python -m pip install -r requirements.txt

# start the API (from backend\) — the standard way
cd backend
..\venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
#   or, from the project root: python run.py (same thing, backend\ on the path)

# start the web UI dev server (from frontend\, hot reload on :5173)
cd frontend
npm install        # first time only
npm run dev        # proxies /api to :8000

# rebuild the UI served by FastAPI on :8000 after ANY change under frontend\src
cd frontend
npm run build

# iteration 5 — the eval suites (project root; rebuilds evals/results/summary.md)
python run_evals.py                    # all six suites, small sets
python run_evals.py --suite safety     # one suite (repeatable)
python run_evals.py --recalibrate      # re-check the safety judge
```

`python run.py` and `uvicorn app.main:app` from `backend\` are equivalent;
`run.py` additionally limits `--reload` to `backend\app` when you pass the
opt-in `--reload` flag.

**NEVER use `--reload` on this machine**: the reloader's child process
trips Windows Application Control ("DLL load failed while importing
uuid_utils/ormsgpack: An Application Control policy has blocked this
file"). Plain uvicorn and plain `run.py` import the same DLLs fine.

For UI work prefer `npm run dev` on `:5173` (hot reload, no rebuild);
`npm run build` is only needed when FastAPI on `:8000` should serve the
UI itself.

### Verify

All suites run from `backend\`. The venv lives at the project root, so the
interpreter is `..\venv\Scripts\python`.

```powershell
cd backend

..\venv\Scripts\python tests\verify_rules.py      # no server needed (it owns the DB)
..\venv\Scripts\python tests\check_api.py         # needs a running server
..\venv\Scripts\python tests\check_approvals.py   # needs a running server
..\venv\Scripts\python tests\check_followups.py   # needs a running server
..\venv\Scripts\python tests\check_register.py    # needs a running server
..\venv\Scripts\python tests\check_chat.py        # needs a running server + built index
..\venv\Scripts\python tests\check_agent.py       # needs a running server
..\venv\Scripts\python tests\check_leave.py       # starts its OWN server on :8010
..\venv\Scripts\python tests\check_mcp.py         # starts its OWN servers on :8011/:8012
..\venv\Scripts\python tests\check_lab.py         # needs :8000; starts lab :8002 if absent
..\venv\Scripts\python tests\check_skills.py      # needs a running server
..\venv\Scripts\python tests\check_redflag.py 0 20   # LLM only, no server
```

Current results — treat any deviation as a regression:

| Suite | Result |
|---|---|
| `verify_rules` | 38 passed, 0 failed, 0 skipped |
| `check_api` | 47 passed, 0 failed, 0 skipped in clinic hours; 42/0/5 outside them (the five "inside 2 hours" checks — see note) |
| `check_approvals` | 35 passed, 0 failed, 0 skipped |
| `check_followups` | 5 passed, 0 failed, 0 skipped |
| `check_register` | 10 passed, 0 failed |
| `check_chat` | 14 passed, 0 failed |
| `check_agent` | 9 passed, 0 failed, 0 skipped |
| `check_leave` | 36 passed, 0 failed, 0 skipped (own server, fixed clock) |
| `check_mcp` | 18 passed, 0 failed, 0 skipped (own servers) |
| `check_lab` | 9 passed, 0 failed, 0 skipped |
| `check_skills` | 16 passed, 1 failed (T08), bar 15/16 ≥14 MET |
| `check_redflag` | 35/35, 0 missed red flags, 0 false alarms |
| `run_evals` | **all five brief targets MET**; safety 0/36 unsafe; judge calibration 12/12; report in `evals/results/summary.md` |

Note on `check_api`: the five 2-hour checks need a free slot *within two
hours of now*, so they only run in clinic hours (Mon–Sat 09:00–16:00-ish).
Outside them the suite reports 42/0/5 with `no free slot inside the 2-hour
window today` — that is the clock, not a regression.

Note on `check_skills`: T08 ("what to do before my glaucoma appointment")
answers correctly from the documents but does not load
`prepare-for-visit`; one miss is inside the pack's ≥14/16 bar. T10 varies
run to run — if three or more rows fail, something regressed in rule 11.

**There is no linter, formatter or type checker configured** (no ruff,
mypy, eslint or prettier), and no `npm run lint` / `typecheck` script. Do
not invent one; just run the suites above.

## Traps

These cost real time. Read them before changing anything.

1. **LLM quota is per model, per day, on the free tier.** Every booking
   attempt runs `check_red_flags` *first*, so when the quota is gone the
   whole booking path returns `503 REDFLAG_CHECK_FAILED` — that is the
   quota, not your code. The suites print `SKIP ... red-flag check
   unavailable (LLM quota)` and pass; do not "fix" that skip.
   - `.env` sets `LLM_MODEL=gemini-3.5-flash-lite` (own quota bucket;
     `gemini-3.5-flash` exhausts at ~20 calls/day).
   - **This agent shell exports `LLM_MODEL=gemini-3.5-flash`, which shadows
     `.env`** because `load_dotenv` does not override existing variables.
     Prefix test/server commands with:
     ```powershell
     $env:LLM_MODEL='gemini-3.5-flash-lite'
     ```
     The repo owner's own terminal is unaffected — it has no such variable.

2. **Port 8000.** Never leave a background server running between tasks.
   `WinError 10013` or `10048` on startup means something still holds the
   port. Find and stop it:
   ```powershell
   Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess
   Get-CimInstance Win32_Process -Filter "Name='uvicorn.exe' or Name='python.exe'" |
     Where-Object { $_.CommandLine -match 'uvicorn|spawn_main' } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
   Note `uvicorn --reload` forks a child; killing only the parent leaves an
   orphan holding the port. `check_leave.py` runs a second server on
   **:8010** with its own throwaway database (see trap 7).

3. **`verify_rules.py` deletes `backend\netracare.db`.** Stop the server
   first or the delete fails on a file lock, and restart it afterwards for
   a clean database.

4. **Rebuild the frontend after editing `frontend\src`.** FastAPI serves the
   compiled `frontend\dist`, not the sources — `--reload` will not pick up
   JSX changes. `frontend\dist` is gitignored; it must be rebuilt locally.

5. **Never hardcode dates in tests.** Today's date moves, Sundays are closed
   and the data pack has clinic holidays (e.g. `2026-11-09`), which the
   closed-day check evaluates *before* the booking-window check — so a
   holiday date masks the rule you meant to test. Probe instead:
   `GET /api/slots?doctor_id=…&date=…` returns `notes` explaining why a day
   is shut. `check_api.first_bookable_day()` and `check_followups.find_slot()`
   show the pattern.

6. **The database is rebuilt from the data pack on startup.** `backend\data\`
   is the source of truth (pack 1, a SQLite file named `*.csv`); do not
   delete it. Packs 2 and 3 live under `iteration2\` and `iteration3\`, pack 4
   (doctor-leave scenarios) under `Iteration 4\Iteration4\data-pack-04\`.

7. **`check_leave.py` owns its own server.** It starts uvicorn on :8010 with
   `NETRACARE_DB` pointed at a temp file and `APP_NOW=2026-11-02T10:00` (the
   pack's fixed clock), so it never touches the app on :8000 and can be
   re-run for ever — it deletes its database in a `finally`. It still spends
   LLM calls (every booking runs the red-flag check), so a quota skip there
   is expected like everywhere else. A leave you mark by hand against the
   real database is *not* cleaned up: it shuts that doctor's session until
   you delete the `doctor_leave` row.

## Conventions

- **Every clinic rule lives in `backend/app/rules.py`** and nowhere else.
  Routers call `validate_booking` / `validate_cancel` and translate the
  raised `Refusal` — they must not re-implement logic. Refusal detail shape
  is `{"code", "message"}`, plus `"emergency_message"` when a red flag was
  seen before the rule fired.
- **The red-flag check runs before the rules engine** (`appointments.py`
  line ~137). Order matters: a red-flag reason on a slot the rules then
  refuse must still save the urgent case *and* return the emergency text
  first (handout §09).
- **Brief wording is verbatim and lives in `backend/app/config.py`** —
  `EMERGENCY_MESSAGE`, `REDFLAG_CHECK_FAILED`, `FACT_FALLBACK`,
  `SUMMARY_FALLBACK`, `IDK_MESSAGE`, `CHAT_UNAVAILABLE`. Tests match on
  substrings like "urgent attention" and "contact Staff"; never reword them.
- **`SEED_APPOINTMENTS` defaults to `0`.** Leave it that way: it gives
  Asha Verma (P01) exactly the 3 completed visits the brief asks for.
  `verify_rules.py` sets `SEED_APPOINTMENTS=1` itself before importing
  config, because the pack's own rule cases need the seeded rows.
- **The backend is the authority for roles.** Check the token's role server
  side (`current_doctor`, `current_patient`, `current_staff`) and never rely
  on the UI hiding a button.

## Map

```
backend/app/
  rules.py       THE rules engine — all clinic rules, one place
  slots.py       free-slot generation (same checks as booking)
  config.py      every brief number and exact string
  llm.py         red-flag check, Fact of the Day, doctor summary
  rag.py         iteration 3: chunking, hybrid search, rewrite + answer
  agent.py       iteration 4: the booking agent + _SYSTEM_RULES
  tools.py       iteration 4/6: the agent's tools (slots, docs, skills, lab)
  skills.py      iteration 6: skills loader (front matter + bodies)
  lab.py         iteration 6: partner-lab MCP client (:8002)
  observability.py  iteration 5: Opik tracing (never fatal)
  seed.py        loads data packs 1 + 2
  routers/       appointments, auth, llm_routes, chat, clinic, leave
backend/mcp_server.py  iteration 6: NetraCare MCP server (:8001, 2 tools)
backend/tests/   the suites listed above (incl. check_mcp/check_lab/check_skills)
evals/           iteration 5: run_evals, judges, datasets, results/summary.md
skills/          iteration 6: clinic skills (SKILL.md + optional files)
partner-lab/     iteration 6: CityCare lab MCP server (:8002)
frontend/src/    React app (screens/ + components/)
iteration2|3/    data packs 2 and 3
Iteration4|5|6/  data packs 4 (leave), 5 (evals), 6 (skills/lab/MCP)
```

`README.md` has the full walkthrough, the refusal-code table and the manual
browser checks for handout §09 and §10.
