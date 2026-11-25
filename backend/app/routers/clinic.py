"""Public clinic information for the landing page (R0: what a visitor sees
before they ever log in).

Everything here is deliberately public — no token — because it is what the
clinic would print on a poster. Patient data never appears in this file.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from .. import config
from ..database import get_db
from ..models import Doctor

router = APIRouter(prefix="/api", tags=["clinic"])

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _hours() -> list[dict]:
    """The weekly hours, derived from SESSIONS so the landing page can never
    drift from the times the rules engine actually enforces."""
    am, pm = config.SESSIONS["AM"], config.SESSIONS["PM"]
    out = []
    for index, day in enumerate(_DAYS):
        if index == config.CLOSED_WEEKDAY:
            out.append({"day": day, "hours": "Closed", "closed": True})
        else:
            out.append({
                "day": day,
                "hours": f"{am['start']}–{am['end']}  ·  {pm['start']}–{pm['end']}",
                "closed": False,
            })
    return out


@router.get("/clinic")
def clinic(db: Session = Depends(get_db)) -> dict:
    """Name, story, hours, doctors and the WhatsApp booking link."""
    doctors, specialities = [], []
    for doc in db.exec(select(Doctor).order_by(Doctor.doctor_id)).all():
        specs = [s.strip() for s in doc.specialities.split(";") if s.strip()]
        doctors.append({
            "doctor_id": doc.doctor_id,
            "name": doc.name,
            "specialities": specs,
        })
        for spec in specs:
            if spec != "General" and spec not in specialities:
                specialities.append(spec)

    am, pm = config.SESSIONS["AM"], config.SESSIONS["PM"]
    return {
        "name": config.CLINIC_NAME,
        "tagline": config.CLINIC_TAGLINE,
        "intro": config.CLINIC_INTRO,
        "address": config.CLINIC_ADDRESS,
        "email": config.CLINIC_EMAIL,
        "hours": _hours(),
        "doctors": doctors,
        "specialities": specialities,
        "visit_length": {
            "new": config.DURATION_MINUTES["new"],
            "follow_up": config.DURATION_MINUTES["follow_up"],
        },
        "booking": {
            "whatsapp": config.WHATSAPP_NUMBER,
            "message": config.WHATSAPP_MESSAGE,
            # Ready-to-open chat link; the UI just sets window.location.
            "url": f"https://wa.me/{config.WHATSAPP_NUMBER}"
                   f"?text={quote(config.WHATSAPP_MESSAGE)}",
        },
        "sessions": f"{am['start']}–{am['end']} morning, {pm['start']}–{pm['end']} afternoon",
    }
