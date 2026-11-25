"""The three write actions, shared by the HTTP routes and the agent's tools.

Iteration 4 gives the chat agent the power to change the database, so the
book / cancel / reschedule logic lives here once and is called by both the
FastAPI routes and `tools.py`. A tool therefore cannot apply a rule the
website would not, and never writes to the database on its own.

Rules still come first and only from `rules.py`: everything here either
calls `validate_booking` / `validate_cancel` / `validate_reschedule`, or
performs the plain bookkeeping those checks have already authorised (the
urgent case, the approval request, the commit).

The one piece of iteration 4 policy expressed here rather than in the rules
engine is *what happens* when a rule raises INSIDE_2_HOURS for a patient:
the brief says save an approval request and return `to_staff` instead of a
flat refusal. The rules engine still decides that the cut-off applies; this
module only translates it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from . import config, llm
from .llm import LlmUnavailable
from .models import ApprovalRequest, Appointment, UrgentCase
from .rules import Refusal, validate_booking, validate_cancel, validate_reschedule


@dataclass
class Outcome:
    """What one write action did.

    `result` is the value the brief's scenario file uses:
    booked | cancelled | rescheduled | to_staff.
    """

    result: str
    appointment: Appointment | None = None
    approval: ApprovalRequest | None = None
    message: str = ""
    emergency_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


TO_STAFF_CANCEL = (
    "Your request has gone to Staff for approval. The appointment stays "
    "booked until they approve it."
)
TO_STAFF_RESCHEDULE = (
    "Your request to change this appointment has gone to Staff for "
    "approval. It stays at its current time until they approve it."
)


def _now() -> str:
    return config.now().isoformat(timespec="seconds")


def pending_approval(db: Session, appointment_id: int, action: str) -> ApprovalRequest | None:
    """An already-waiting request for this appointment, so asking twice
    does not queue the same change twice."""
    return db.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.appointment_id == appointment_id,
            ApprovalRequest.action == action,
            ApprovalRequest.status == "pending",
        )
    ).first()


def _queue(
    db: Session, appt: Appointment, *, action: str, payload: dict | None = None
) -> ApprovalRequest:
    existing = pending_approval(db, appt.id, action)
    if existing is not None:
        if payload is not None:
            existing.payload = json.dumps(payload)
            db.add(existing)
            db.commit()
            db.refresh(existing)
        return existing

    approval = ApprovalRequest(
        patient_id=appt.patient_id,
        appointment_id=appt.id,
        action=action,
        payload=json.dumps(payload) if payload is not None else None,
        status="pending",
        created_at=_now(),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


# --- book ------------------------------------------------------------------

def book(
    db: Session,
    *,
    patient_id: str,
    doctor_id: str,
    speciality: str,
    visit_type: str,
    date: str,
    start_time: str,
    reason: str,
    trace_role: str | None = None,
    via: str = "Website",
    intake_notes: str | None = None,
    urgent_source: str = "booking",
    block_on_red_flag: bool = False,
) -> Outcome:
    """Red-flag check first, then the one rules check, then commit.

    Identical to what `POST /api/appointments` did: a red flag on the
    reason is saved as an urgent case and answered with the emergency
    response even when a clinic rule then refuses the booking.

    `trace_role` only labels the Opik trace (iteration 5 R1):
    patient | staff | partner.
    Iteration 6 (R2): `via` is stamped on the booking (Website | Chat |
    Staff | MCP), `urgent_source` marks where a red flag came from, and
    the MCP server passes `block_on_red_flag=True` so a partner booking
    never goes through on a red flag — the website's own behaviour
    (urgent booking holds the slot) is unchanged.
    """
    reason = (reason or "").strip()
    if not reason:
        raise Refusal("MISSING_REASON", "Please give a reason for the visit.")
    if len(reason) > 300:
        raise Refusal("REASON_TOO_LONG", "The reason must be 300 characters or fewer.")

    emergency: str | None = None
    try:
        verdict = llm.check_red_flags(reason, role=trace_role)
    except LlmUnavailable as exc:
        # Never book silently when the safety check cannot run.
        print(f"[redflag] booking check failed: {exc}", flush=True)
        raise Refusal(
            "REDFLAG_CHECK_FAILED", config.REDFLAG_CHECK_FAILED, status=503
        ) from exc

    if verdict.red_flag:
        emergency = config.EMERGENCY_MESSAGE
        db.add(UrgentCase(
            patient_id=patient_id,
            reason=reason,
            category=verdict.category,
            why=verdict.why or "",
            created_at=_now(),
            seen=False,
            source=urgent_source,
        ))
        db.commit()
        if block_on_red_flag:
            # Iteration 6 (R2): a partner booking stops here — urgent case
            # saved, emergency response returned, no appointment row.
            return Outcome(result="red_flag", emergency_message=emergency)

    try:
        appt = validate_booking(
            db,
            patient_id=patient_id,
            doctor_id=doctor_id,
            speciality=speciality,
            visit_type=visit_type,
            date=date,
            start_time=start_time,
        )
    except Refusal as err:
        # The urgent case (if any) already stands. The emergency response
        # still comes first, riding along on the refusal (handout §09).
        err.emergency_message = emergency
        raise

    appt.reason = reason
    appt.urgent = bool(verdict.red_flag)
    appt.booked_via = via
    if intake_notes:
        appt.intake_notes = intake_notes
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return Outcome(result="booked", appointment=appt, emergency_message=emergency)


# --- cancel ----------------------------------------------------------------

def cancel(
    db: Session, *, appointment_id: int, acting_patient_id: str | None
) -> Outcome:
    """Cancel one appointment.

    A patient inside the 2-hour cut-off does not fail: the request is
    queued for Staff and the appointment stays `booked` (`to_staff`).
    Staff (`acting_patient_id=None`) cancel immediately, as before.
    """
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise Refusal("NOT_FOUND", "That appointment does not exist.", status=404)

    try:
        validate_cancel(db, appt, acting_patient_id=acting_patient_id)
    except Refusal as err:
        if err.code == "INSIDE_2_HOURS" and acting_patient_id is not None:
            approval = _queue(db, appt, action="cancel")
            return Outcome(
                result="to_staff",
                appointment=appt,
                approval=approval,
                message=TO_STAFF_CANCEL,
            )
        raise

    appt.status = "cancelled"  # the slot becomes free for anyone
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return Outcome(result="cancelled", appointment=appt)


# --- reschedule ------------------------------------------------------------

def reschedule(
    db: Session,
    *,
    appointment_id: int,
    date: str,
    start_time: str,
    doctor_id: str | None = None,
    acting_patient_id: str | None = None,
) -> Outcome:
    """Move one appointment: validate the new booking, then swap the rows.

    If any rule refuses, `validate_reschedule` raises before anything is
    written and the original appointment is untouched. A patient moving
    something inside the 2-hour cut-off queues the move for Staff instead
    (`to_staff`); Staff apply it immediately.
    """
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise Refusal("NOT_FOUND", "That appointment does not exist.", status=404)

    payload = {"date": date, "start_time": start_time, "doctor_id": doctor_id or appt.doctor_id}

    try:
        new = validate_reschedule(
            db,
            appt,
            date=date,
            start_time=start_time,
            doctor_id=doctor_id,
            acting_patient_id=acting_patient_id,
        )
    except Refusal as err:
        if err.code == "INSIDE_2_HOURS" and acting_patient_id is not None:
            approval = _queue(db, appt, action="reschedule", payload=payload)
            return Outcome(
                result="to_staff",
                appointment=appt,
                approval=approval,
                message=TO_STAFF_RESCHEDULE,
            )
        err.emergency_message = None
        raise

    # Both halves in one transaction: the old row is freed and the new one
    # takes its place, or neither happens.
    appt.status = "cancelled"
    new.reason = appt.reason
    new.urgent = appt.urgent
    new.booked_via = appt.booked_via  # a move keeps the way it was booked
    new.intake_notes = appt.intake_notes
    db.add(appt)
    db.add(new)
    db.commit()
    db.refresh(appt)
    db.refresh(new)
    return Outcome(result="rescheduled", appointment=new, extra={"old_appointment_id": appt.id})


# --- Staff approvals -------------------------------------------------------

def approve(db: Session, approval: ApprovalRequest) -> Outcome:
    """Staff click Approve: carry the queued action out as Staff.

    Re-runs the same action with `acting_patient_id=None`, so the rules are
    applied again against the state of the database *now* — a slot that has
    since been taken is still refused, and the request simply stays pending
    for Staff to reject.
    """
    if approval.status != "pending":
        raise Refusal(
            "ALREADY_DECIDED",
            f"This request was already {approval.status}.",
            status=409,
        )

    if approval.action == "cancel":
        outcome = cancel(db, appointment_id=approval.appointment_id, acting_patient_id=None)
    else:
        payload = json.loads(approval.payload or "{}")
        outcome = reschedule(
            db,
            appointment_id=approval.appointment_id,
            date=payload.get("date", ""),
            start_time=payload.get("start_time", ""),
            doctor_id=payload.get("doctor_id"),
            acting_patient_id=None,
        )

    approval.status = "approved"
    approval.decided_at = _now()
    db.add(approval)
    db.commit()
    db.refresh(approval)
    outcome.approval = approval
    outcome.extra["approval_id"] = approval.id
    return outcome


def reject(db: Session, approval: ApprovalRequest) -> Outcome:
    """Staff click Reject: the appointment is left exactly as it was."""
    if approval.status != "pending":
        raise Refusal(
            "ALREADY_DECIDED",
            f"This request was already {approval.status}.",
            status=409,
        )
    appt = db.get(Appointment, approval.appointment_id)
    approval.status = "rejected"
    approval.decided_at = _now()
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return Outcome(result="rejected", appointment=appt, approval=approval)
