"""Clinic configuration for NetraCare.

All the numbers that come from the capstone brief live here, in one place:
session times, slot length, visit durations, the cancel cut-off, the
booking window, data-pack paths and the LLM key. Change them here, never
inside the rules engine or the LLM module.
"""
from __future__ import annotations

import os
from datetime import datetime

from dotenv import load_dotenv

# Project root .env (and backend\.env if present) — API key stays out of git.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
_PROJECT = os.path.dirname(_ROOT)
load_dotenv(os.path.join(_PROJECT, ".env"))
load_dotenv(os.path.join(_ROOT, ".env"))

# --- Paths -----------------------------------------------------------------
BASE_DIR = _ROOT  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
# Tests that need their own database (a frozen clock, a wiped-and-rebuilt
# table) point NETRACARE_DB somewhere else before importing this module.
DB_PATH = os.environ.get("NETRACARE_DB") or os.path.join(BASE_DIR, "netracare.db")

# Data pack 1 ships as a SQLite file that is named "*.csv". We look for it
# in the project's data/ folder first, then fall back to the Downloads folder.
SOURCE_SQLITE = [
    os.path.join(DATA_DIR, "weekly_schedule.csv"),
    r"C:\Users\admin\Downloads\weekly_schedule.csv",
]
SOURCE_CSV_DIRS = [
    DATA_DIR,
    r"C:\Users\admin\Downloads",
]

# Data pack 2 (iteration 2): plain CSVs under iteration2/data-pack-02/.
PACK2_DIR = os.path.join(_PROJECT, "iteration2", "data-pack-02")

# Data pack 3 (iteration 3): clinic documents under iteration3/data-pack-03/.
PACK3_DIR = os.path.join(_PROJECT, "iteration3", "data-pack-03")
DOCS_DIR = os.path.join(PACK3_DIR, "documents")
# Chroma persistence folder — rebuilt by build_index.py, never per request.
CHROMA_DIR = os.path.join(_ROOT, ".chroma")

# --- Clock -----------------------------------------------------------------
# Real clock by default, per the brief ("pick dates in the next few weeks").
# Set APP_NOW (e.g. 2026-11-02T10:00) to replay a fixed point in time.
APP_NOW = os.environ.get("APP_NOW")


def now() -> datetime:
    """The clinic's current date and time."""
    if APP_NOW:
        return datetime.fromisoformat(APP_NOW)
    return datetime.now()


# --- Clinic sessions (24-hour clock) ---------------------------------------
SESSIONS: dict[str, dict[str, str]] = {
    "AM": {"start": "09:00", "end": "13:00"},
    "PM": {"start": "14:00", "end": "18:00"},
}

# --- Booking rules ---------------------------------------------------------
SLOT_STEP_MINUTES = 10                       # starts sit on 10-minute marks
DURATION_MINUTES = {"new": 20, "follow_up": 10}
CANCEL_CUTOFF_HOURS = 2                      # patients cancel until 2h before
BOOKING_WINDOW_DAYS = 30                     # how far ahead you may book
CLOSED_WEEKDAY = 6                           # Sunday (Monday=0 ... Sunday=6)

# Import the data pack's 18 sample appointments on first start. The brief's
# default is an empty appointments table — bookings are made through the app
# (and data pack 2's past_visits.csv then gives Asha Verma exactly the 3
# completed visits R1 asks for). Set SEED_APPOINTMENTS=1 to import the pack's
# sample rows instead — handy for demos; verify_rules.py does this itself.
SEED_APPOINTMENTS = os.environ.get("SEED_APPOINTMENTS", "0").lower() in {"1", "true", "yes"}

# --- LLM (iteration 2) -----------------------------------------------------
# Provider/model come from .env so switching models never touches code.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite").strip()
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))

# Shown to patients under every Fact of the Day. Added by THIS code, never
# asked of the model (handout R5).
FACT_CAVEAT = "General information, not medical advice"

# Exact emergency wording for a red flag (handout R4). Also from code, not
# the model, so it is always word-for-word.
EMERGENCY_MESSAGE = (
    "What you describe needs urgent attention. Please come to the clinic now "
    "or go to the nearest emergency department. Our staff have been notified."
)

# Shown when the red-flag check itself cannot run — never book silently.
REDFLAG_CHECK_FAILED = (
    "We couldn't check your reason just now. Please try again, or call the "
    "clinic if it's urgent."
)

# Fact / summary fallbacks when the LLM call fails (handout §07).
FACT_FALLBACK = (
    "Blinking spreads tears across the eye and helps keep the surface comfortable."
)
SUMMARY_FALLBACK = "Summary unavailable. See the notes below."

# --- Chat (iteration 3) -----------------------------------------------------
# Embeddings for the clinic-documents index. Change => rebuild the index.
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001").strip()

# Chunking (handout R1): split sections longer than this, with overlap.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
TOP_K = 4                 # hybrid search returns this many chunks
CHAT_MAX_CHARS = 500      # one patient message
CHAT_HISTORY_MAX = 6      # messages sent with each request

IDK_MESSAGE = (
    "I don't know. Our clinic documents don't cover that. "
    "Please ask Staff at the front desk."
)
# Rewrite / search / answer step failed (handout §08).
CHAT_UNAVAILABLE = (
    "Sorry, I can't answer just now. Please try again, "
    "or ask Staff at the front desk."
)
# The red-flag check itself could not run (handout R4) — never answer anyway.
CHAT_REDFLAG_FAILED = (
    "We couldn't check your message just now. Please try again, "
    "or call the clinic if it's urgent."
)

# --- Booking agent (iteration 4) --------------------------------------------
# The agent's step limit: agent node -> tools -> agent counts as steps.
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
# Shown when the step limit is reached (handout R3 — exact wording).
AGENT_STEP_LIMIT_MESSAGE = (
    "Sorry, I can't finish that just now. Please try again, "
    "or ask Staff at the front desk."
)
# At most this many messages go to the model, so long chats stay fast (R4).
AGENT_HISTORY_MAX = 20

# --- Skills (iteration 6, R4) -----------------------------------------------
# Agent Skills folder format: <skills-dir>/<name>/SKILL.md with YAML front
# matter (name, description) and, in some skills, one extra file.
SKILLS_DIR = os.path.join(_PROJECT, "skills")

# --- Partner lab (iteration 6, R3) ------------------------------------------
# CityCare Hospital Lab's own MCP server. It is not ours: when it is not
# running, the chat still works and lab questions get the fixed message.
LAB_MCP_URL = os.environ.get("LAB_MCP_URL", "http://localhost:8002/mcp").strip()
LAB_TIMEOUT_SECONDS = float(os.environ.get("LAB_TIMEOUT_SECONDS", "10"))
# Shown under an answer that used the lab's tool (handout R3).
LAB_SOURCE = "CityCare Lab (partner)"
# The exact words for a lab question when the partner's server is down.
LAB_UNAVAILABLE = (
    "I can't check CityCare Lab just now. Please ask Staff at the front desk."
)

# --- NetraCare's own MCP server (iteration 6, R1) ---------------------------
# Partner apps connect here; tests may point it at another port.
MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))

# --- Public clinic details (the landing page) ------------------------------
# Everything a visitor reads before logging in lives here, so the clinic's
# details are changed in one place and never hard-coded into a screen.

CLINIC_NAME = "NetraCare Eye Clinic"
CLINIC_TAGLINE = "Specialist eye care for the whole family — book in a message."
CLINIC_INTRO = (
    "NetraCare is a consultant-led eye clinic covering cataract, glaucoma, "
    "retina, cornea and paediatric eye care. Every appointment is with a named "
    "consultant, consultations are unhurried, and follow-ups are booked with the "
    "same doctor who saw you first."
)
CLINIC_ADDRESS = "12 Lakeview Road, Bengaluru 560001"
CLINIC_EMAIL = "appointments@netracare.example"

# WhatsApp number behind the landing page's booking button: digits only,
# WITH the country code and WITHOUT "+" or spaces (e.g. 919876543210).
# Override without editing code:  $env:WHATSAPP_NUMBER = "919876543210"
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "919876543210")

# Pre-filled into the chat so reception knows what the visitor wants.
WHATSAPP_MESSAGE = (
    "Hello NetraCare, I would like to book an appointment. "
    "My name is ______ and the best number to reach me on is ______."
)
