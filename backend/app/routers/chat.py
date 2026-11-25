"""Iteration 4 patient chat — the booking agent (handout R3-R6).

One endpoint, `POST /api/chat`, takes either a typed message or the answer
to a confirmation card, plus the thread_id the browser holds:

    {thread_id, message}          → one agent turn
    {thread_id, confirm: true}    → resume: run the waiting action
    {thread_id, confirm: false}   → resume: the patient pressed No
    a typed message while an
    action is waiting             → counts as No, then handles the message

Everything that is not the LLM's to decide stays in this file or in
`agent.py`: the red-flag check (agent node), the sources line and the
"I don't know" wording come from code, never from the model (R6). The
Staff search debug (iteration 3, R2) is unchanged.
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command

from .. import agent, config, observability, rag
from ..schemas import ChatIn, ChatOut, SearchDebugOut, SearchHit
from ..security import Identity, current_patient, current_staff

router = APIRouter(prefix="/api", tags=["chat"])

# What `notice` does to the response type; anything else answers normally.
_NOTICE_TYPE = {
    "emergency": "emergency",
    "check_failed": "check_failed",
    "unavailable": "unavailable",
}


@router.post("/chat", response_model=ChatOut)
def chat(
    body: ChatIn,
    identity: Identity = Depends(current_patient),
) -> ChatOut:
    """One turn of the booking agent (patients only)."""
    patient_id = identity.patient_id or ""
    # Namespaced with the patient so nobody can open another's thread.
    thread = (body.thread_id or "").strip() or uuid4().hex
    cfg = agent.thread_config(patient_id, thread)
    # Iteration 5 R1: one Opik trace for this turn — red-flag check, the
    # agent's LLM calls and every tool call nest inside it.
    cfg["callbacks"] = observability.callbacks(
        "chat", role="patient", patient_id=patient_id, thread_id=thread
    )

    message = (body.message or "").strip()
    confirm = body.confirm
    if confirm is None and not message:
        raise HTTPException(400, detail={
            "code": "MISSING_MESSAGE",
            "message": "Please type a message first.",
        })

    before = agent.message_count(cfg)
    waiting = agent.pending_action(cfg)

    try:
        if confirm is not None:
            if waiting is None:
                raise HTTPException(400, detail={
                    "code": "NOTHING_PENDING",
                    "message": "There is no action waiting to confirm.",
                })
            agent.graph.invoke(Command(resume=bool(confirm)), cfg)
        elif waiting is not None:
            # A typed message while an action waits counts as No (R5);
            # the agent then handles the new message in the same request.
            agent.graph.invoke(Command(resume=False), cfg)
            agent.graph.invoke(agent.fresh_input(message), cfg)
        else:
            agent.graph.invoke(agent.fresh_input(message), cfg)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — never surface a stack trace
        print(f"[chat] agent run failed: {exc}", flush=True)
        return ChatOut(type="unavailable", message=config.CHAT_UNAVAILABLE, thread_id=thread)

    # --- what the run produced ------------------------------------------
    state = agent.graph.get_state(cfg)
    values = state.values or {}
    messages = values.get("messages") or []
    notice = str(values.get("notice") or "")

    pending: str | None = None
    if state.next:
        for task in state.tasks:
            for it in getattr(task, "interrupts", None) or ():
                value = it.value
                pending = str(value.get("summary") or "") if isinstance(value, dict) else str(value)
                break
            if pending is not None:
                break

    text = agent.final_text(messages)

    type_ = _NOTICE_TYPE.get(notice, "answer")
    sources: list[str] = []
    skills_used: list[str] = []
    if not notice:
        # R6: sources and the "I don't know" wording come from the tool's
        # result in THIS run, never from the model.
        payload = agent.search_result(messages[before:])
        if payload:
            result = payload.get("result")
            if result == "idk":
                type_, sources = "idk", []
                # R6: the fixed wording comes from code. Rule 8 lets the
                # model add one short line after it (who can assess), but
                # only when it opened with the exact wording — otherwise
                # the code text alone is the reply.
                if not text.startswith(config.IDK_MESSAGE):
                    text = config.IDK_MESSAGE
            elif result == "unavailable":
                type_, text, sources = "unavailable", config.CHAT_UNAVAILABLE, []
            elif result == "answered":
                sources = [str(s) for s in payload.get("sources") or []]

        # Iteration 6 R3/R4: partner-lab source and skills loaded this turn.
        lab_payload = agent.lab_result(messages[before:])
        if lab_payload:
            if lab_payload.get("result") == "ok":
                if config.LAB_SOURCE not in sources:
                    sources.append(config.LAB_SOURCE)
            elif lab_payload.get("result") == "unavailable" and not payload:
                type_, text = "unavailable", config.LAB_UNAVAILABLE
        skills_used = agent.skill_names(messages[before:])

    return ChatOut(
        type=type_,  # type: ignore[arg-type]
        message=text,
        sources=sources,
        skills=skills_used,
        thread_id=thread,
        pending_action=pending,
    )


@router.get("/staff/search-debug", response_model=SearchDebugOut)
def search_debug(
    q: str = "",
    identity: Identity = Depends(current_staff),
) -> SearchDebugOut:
    """Top-4 chunks from keyword, vector and hybrid search (Staff only)."""
    _ = identity
    query = q.strip()
    if not query:
        raise HTTPException(400, detail={
            "code": "MISSING_QUERY",
            "message": "Type a question to search the clinic documents.",
        })
    try:
        raw = rag.search_debug(query)
    except rag.LlmUnavailable as exc:
        raise HTTPException(503, detail={
            "code": "INDEX_NOT_READY",
            "message": str(exc),
        }) from exc
    return SearchDebugOut(
        query=raw["query"],
        keyword=[SearchHit(**h) for h in raw["keyword"]],
        vector=[SearchHit(**h) for h in raw["vector"]],
        hybrid=[SearchHit(**h) for h in raw["hybrid"]],
    )
