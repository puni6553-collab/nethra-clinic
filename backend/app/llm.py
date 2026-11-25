"""All LLM code for NetraCare — iteration 2 (handout §07).

Three LangChain chains, each `prompt | model | parser`:

1. check_red_flags(text)  — Pydantic parser; safety gate before every booking
2. fact_of_the_day(previous) — plain string; patient card + Refresh
3. summarise_patient(...)   — plain string; doctor-only patient summary

Rules that live in this file (not in the prompts):
- temperature 0, a timeout on every call
- fact / summary failures return fixed fallbacks from config
- red-flag failure raises LlmUnavailable so the booking route can refuse
- the emergency sentence and the fact caveat come from config, never the model

Iterations 3 (chat) and 4 (agent) call check_red_flags as-is — keep it here.
"""
from __future__ import annotations

import json
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import config, observability

# Free-tier Gemini is 5/min and 20/day; these transient codes are worth a
# short polite wait rather than failing the booking immediately. Matched
# against the LOWER-CASED error text, so "Quota exceeded" matches too.
_RETRY_MARKERS = (
    "429", "resource_exhausted", "503", "unavailable", "504", "deadline",
    "quota", "rate limit", "rate_limit", "too many requests",
    # transient connection drops mid-call (WinError 10054, reset, aborted)
    "10054", "forcibly closed", "connection reset", "connection aborted",
)
# The 45s tail exists to outlast a per-minute rate-limit window: without it
# a burst of calls (e.g. every booking attempt red-flag checks first) fails
# at ~31s even though the window would have cleared at ~60s.
_RETRY_DELAYS = (0, 3, 8, 20, 45)  # seconds before each attempt (~76s total)


class LlmUnavailable(Exception):
    """The red-flag check could not run (timeout, bad key, unparseable reply)."""


class RedFlagResult(BaseModel):
    """What the red-flag chain returns — data, not free text."""

    red_flag: bool = Field(description="True if the reason describes a red flag happening now")
    category: Optional[
        Literal[
            "sudden_vision_loss",
            "curtain_or_shadow",
            "floaters_or_flashes",
            "chemical_or_injury",
            "severe_pain_headache_haloes",
        ]
    ] = Field(default=None, description="Which of the five categories, or null")
    why: str = Field(default="", description="One short line explaining the decision")


# --- model factory ----------------------------------------------------------

def _chat_model():
    """Build the chat model from .env. Raises LlmUnavailable if unusable."""
    provider = config.LLM_PROVIDER
    try:
        if provider == "gemini":
            if not config.GEMINI_API_KEY:
                raise LlmUnavailable("GEMINI_API_KEY is not set")
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=config.LLM_MODEL,
                google_api_key=config.GEMINI_API_KEY,
                temperature=0,
                timeout=config.LLM_TIMEOUT_SECONDS,
                max_retries=0,
            )
        if provider == "openai":
            import os

            key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                raise LlmUnavailable("OPENAI_API_KEY is not set")
            from langchain_openai import ChatOpenAI # type: ignore

            return ChatOpenAI(
                model=config.LLM_MODEL,
                api_key=key,
                temperature=0,
                timeout=config.LLM_TIMEOUT_SECONDS,
                max_retries=1,
            )
        raise LlmUnavailable(f"Unknown LLM_PROVIDER {provider!r}")
    except LlmUnavailable:
        raise
    except Exception as exc:  # import errors, bad config
        raise LlmUnavailable(str(exc)) from exc


def chat_model():
    """The chat model for the iteration-4 agent. Raises LlmUnavailable."""
    return _chat_model()


def _string_chain(system: str, human_template: str):
    """prompt | model | str — for Fact of the Day and the doctor summary."""
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages(
        [("system", system), ("human", human_template)]
    )
    return prompt | _chat_model() | StrOutputParser()


def _redflag_chain():
    """prompt | model | PydanticOutputParser — for check_red_flags."""
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    parser = PydanticOutputParser(pydantic_object=RedFlagResult)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are a safety screener for an eye clinic. You decide whether "
                    "an appointment reason describes a red flag that needs the patient "
                    "to come in today.\n\n"
                    "A red flag may mean the patient could lose sight if they wait for a "
                    "normal appointment. When unsure, set red_flag to true.\n\n"
                    "The five categories:\n"
                    "1. sudden_vision_loss — vision dropping or going dark suddenly "
                    "(minutes, hours or a day), one or both eyes. NOT slow blurring over "
                    "months or years.\n"
                    "2. curtain_or_shadow — a curtain, veil or shadow coming over or "
                    "moving across part of the vision. NOT a small fixed spot for years.\n"
                    "3. floaters_or_flashes — a sudden shower of new floaters (dots, "
                    "spots, cobwebs) or flashes/sparks of light. NOT a few unchanged "
                    "old floaters.\n"
                    "4. chemical_or_injury — any chemical in the eye (cleaner, bleach, "
                    "acid, cement) or any injury (hit, poked, something flew in). NOT "
                    "mild dust or smoke irritation that has settled.\n"
                    "5. severe_pain_headache_haloes — severe eye pain, especially with "
                    "headache, feeling sick or vomiting, or haloes/rainbows around "
                    "lights. NOT mild itching, grittiness, dryness or tired eyes.\n\n"
                    "The patient must describe it as happening now. Phrases such as "
                    "'No sudden loss of vision' or 'Gradual loss of vision' are NOT "
                    "red flags.\n\n"
                    "Return only the structured answer."
                ),
            ),
            (
                "human",
                "Appointment reason:\n{reason}\n\n{format_instructions}",
            ),
        ]
    )
    return prompt | _chat_model() | parser


# --- public API ---------------------------------------------------------------

def _invoke_with_retry(fn):
    """Call fn() with short waits on rate-limit / overload errors.

    Raises the last error if every attempt fails (LlmUnavailable wraps it).
    """
    last: Exception | None = None
    for delay in _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — classified below
            last = exc
            text = str(exc).lower()
            if not any(marker in text for marker in _RETRY_MARKERS):
                raise
            if "per day" in text or "per_day" in text:
                raise  # daily quota exhausted — waiting cannot help; fail fast
    raise last if last else LlmUnavailable("LLM call failed")


def check_red_flags(text: str, *, role: str | None = None) -> RedFlagResult:
    """Screen one appointment reason. Raises LlmUnavailable on any failure.

    Never returns a silent pass: if the model or parser fails, the booking
    route must refuse (handout R4).

    `role` tags the trace (patient | staff | partner) — iteration 5 R1.
    """
    reason = (text or "").strip()
    if not reason:
        raise LlmUnavailable("empty reason")
    try:
        from langchain_core.output_parsers import PydanticOutputParser

        instructions = PydanticOutputParser(
            pydantic_object=RedFlagResult
        ).get_format_instructions()
        trace_cfg = {
            "callbacks": observability.callbacks("red_flag", role=role or "patient"),
        }

        def _call():
            chain = _redflag_chain()
            result = chain.invoke(
                {"reason": reason, "format_instructions": instructions},
                config=trace_cfg,
            )
            if not isinstance(result, RedFlagResult):
                result = RedFlagResult.model_validate(result)
            return result

        return _invoke_with_retry(_call)
    except LlmUnavailable:
        raise
    except Exception as exc:
        raise LlmUnavailable(str(exc)) from exc


def fact_of_the_day(previous: str | None = None) -> str:
    """One short eye-health fact. On failure, config.FACT_FALLBACK (never raises)."""
    system = (
        "You write one short Fact of the Day about eye health for a clinic "
        "waiting-room card. General and friendly only: no conditions to watch "
        "for, no treatments, medicines or product names, no diagnosis, no advice "
        "to treat anything. At most two sentences. Plain wording."
    )
    if previous and previous.strip():
        human = (
            "Current fact (do not repeat it or a close variant):\n{previous}\n\n"
            "Give a different Fact of the Day."
        )
        try:
            chain = _string_chain(system, human)
            out = chain.invoke(
                {"previous": previous.strip()},
                config={"callbacks": observability.callbacks("fact", role="patient")},
            ).strip()
            return out or config.FACT_FALLBACK
        except Exception:
            return config.FACT_FALLBACK
    human = "Give today's Fact of the Day."
    try:
        chain = _string_chain(system, human)
        out = chain.invoke(
            {},
            config={"callbacks": observability.callbacks("fact", role="patient")},
        ).strip()
        return out or config.FACT_FALLBACK
    except Exception:
        return config.FACT_FALLBACK


def summarise_patient(
    *,
    name: str,
    age: int | None,
    general_health: str | None,
    allergies: str | None,
    today_reason: str | None,
    visits: list[dict],
) -> str:
    """<=120-word summary of RECORDED facts only. On failure, config.SUMMARY_FALLBACK.

    `visits` items: date, doctor, speciality, note (newest first).
    """
    lines = []
    for v in visits:
        lines.append(
            f"- {v.get('date')}: {v.get('doctor')} ({v.get('speciality')}): {v.get('note')}"
        )
    visits_block = "\n".join(lines) if lines else "(no previous visits on record)"

    system = (
        "You write a short clinical summary for an ophthalmologist from RECORDED "
        "notes only. Restate only what is written; you may repeat a diagnosis or "
        "treatment a doctor recorded and you should say which doctor. Never add "
        "your own diagnosis, guess, risk or advice. If something is not "
        "recorded, leave it out. At most 120 words. No preamble."
    )
    human = (
        "Patient: {name}\n"
        "Age: {age}\n"
        "General health: {health}\n"
        "Allergies: {allergies}\n"
        "Today's appointment reason: {reason}\n"
        "Completed visits (newest first):\n{visits}\n\n"
        "Write the summary."
    )
    try:
        chain = _string_chain(system, human)
        out = chain.invoke(
            {
                "name": name,
                "age": str(age) if age else "unknown",
                "health": general_health or "not recorded",
                "allergies": allergies or "not recorded",
                "reason": today_reason or "not recorded",
                "visits": visits_block,
            },
            config={"callbacks": observability.callbacks("summary", role="doctor")},
        ).strip()
        return out or config.SUMMARY_FALLBACK
    except Exception:
        return config.SUMMARY_FALLBACK


# Kept for the stretch-goal scorer and any caller that wants JSON text.
def check_red_flags_json(text: str) -> str:
    return json.dumps(check_red_flags(text).model_dump(), ensure_ascii=False)
