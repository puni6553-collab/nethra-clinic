"""SQLite engine and session dependency."""
from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine # type: ignore

from . import config

engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Create the tables (models must be imported so they register)."""
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Add columns that existing databases predate (create_all never alters).

    Iteration 3 added urgent_cases.source; iteration 4 (R7) added the two
    rebooking columns on appointments. A netracare.db created before them
    would otherwise 500 the moment a doctor marks leave.
    """
    import sqlalchemy as sa

    with engine.connect() as conn:
        cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(urgent_cases)")]
        if cols and "source" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE urgent_cases ADD COLUMN source TEXT NOT NULL DEFAULT 'booking'"
            )
            conn.commit()

        appt_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(appointments)")]
        if appt_cols:
            if "moved_from_id" not in appt_cols:
                conn.exec_driver_sql("ALTER TABLE appointments ADD COLUMN moved_from_id INTEGER")
            if "move_note" not in appt_cols:
                conn.exec_driver_sql("ALTER TABLE appointments ADD COLUMN move_note TEXT")
            # Iteration 6 (R2/R5): where the booking came from, and the
            # LASIK intake answers shown on the appointment.
            if "booked_via" not in appt_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE appointments ADD COLUMN booked_via TEXT NOT NULL DEFAULT 'Website'"
                )
            if "intake_notes" not in appt_cols:
                conn.exec_driver_sql("ALTER TABLE appointments ADD COLUMN intake_notes TEXT")
            conn.commit()


def get_db():
    """FastAPI dependency that yields a database session."""
    with Session(engine) as session:
        yield session
