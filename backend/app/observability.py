"""Iteration 5 (R1): Opik tracing for every LLM feature.

One trace per chat turn, per red-flag check on a booking reason, per doctor
summary and per Fact of the Day, with tags (feature, role) and metadata
(synthetic patient id, thread id) so the Opik project view can be filtered.

Two rules from the brief shape this module:

* **The app must work without Opik.** If OPIK_API_KEY is missing, Opik is
  not installed, or anything at all goes wrong while building a tracer,
  every helper here returns an empty list / an identity decorator and the
  call runs untraced. No exception ever escapes to the caller.
* **One project for the app, one for eval runs.** netracare-dev by
  default; run_evals.py sets OPIK_PROJECT_NAME=netracare-evals so its
  traces (including those of the eval server it spawns) land in the other
  project.

A tracer is cheap to build (the Opik client is cached inside the SDK) and
Opik's context storage is contextvars-based, so a fresh tracer per call is
both safe and accurate: spans created inside an existing Opik context (a
red-flag check during a chat turn) attach to the chat trace instead of
starting a second one.
"""
from __future__ import annotations

import os
import threading
import warnings

from . import config  # noqa: F401  (importing config loads .env into os.environ)

DEV_PROJECT = "netracare-dev"
EVALS_PROJECT = "netracare-evals"

_lock = threading.Lock()
_probed = False
_available = False
_warned = False


def _once(message: str) -> None:
    global _warned
    with _lock:
        if _warned:
            return
        _warned = True
    warnings.warn(message, RuntimeWarning, stacklevel=2)


def available() -> bool:
    """True when tracing can actually record something (key + SDK present)."""
    global _probed, _available
    if _probed:
        return _available
    with _lock:
        if _probed:
            return _available
        _available = False
        if os.environ.get("OPIK_API_KEY", "").strip():
            try:
                import opik  # noqa: F401

                _available = True
            except Exception as exc:  # SDK missing/broken: never fatal
                _once(f"Opik SDK unavailable, tracing disabled: {exc}")
        _probed = True
    return _available


def project_name() -> str:
    """netracare-dev for the app, netracare-evals for run_evals.py."""
    return os.environ.get("OPIK_PROJECT_NAME", "").strip() or DEV_PROJECT


def callbacks(
    feature: str,
    *,
    role: str | None = None,
    **metadata: object,
) -> list:
    """LangChain callbacks for one invoke: a trace tagged with `feature`.

    Pass as ``config={"callbacks": observability.callbacks(...)}``. Empty
    when tracing is off, which is exactly what LangChain expects.
    """
    if not available():
        return []
    try:
        from opik.integrations.langchain import OpikTracer

        tags = [feature]
        meta = {"feature": feature}
        if role:
            tags.append(f"role:{role}")
            meta["role"] = role
        meta.update({k: v for k, v in metadata.items() if v not in (None, "")})
        return [
            OpikTracer(
                tags=tags,
                metadata=meta,
                project_name=project_name(),
            )
        ]
    except Exception as exc:  # Opik down, bad config: app keeps working
        _once(f"Opik tracer unavailable, tracing disabled: {exc}")
        return []


def tracked(feature: str, *, role: str | None = None, name: str | None = None, **metadata: object):
    """`@track` when Opik is on, an identity decorator when it is off.

    Used on the app's own functions (retrieval, answering) so their inputs
    and outputs show up as spans inside the surrounding chat trace.
    """
    def decorate(fn):
        if not available():
            return fn
        try:
            from opik import track

            return track(
                name=name or fn.__name__,
                tags=[feature] + ([f"role:{role}"] if role else []),
                metadata={"feature": feature, **({"role": role} if role else {}),
                          **{k: v for k, v in metadata.items() if v not in (None, "")}},
                project_name=project_name(),
            )(fn)
        except Exception as exc:
            _once(f"Opik @track unavailable, tracing disabled: {exc}")
            return fn

    return decorate
