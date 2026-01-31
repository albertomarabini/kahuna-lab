# classes/pending_charge_recorder.py

from uuid import uuid4
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from classes.entities import Project, PendingCharge


def record_pending_charge(
    session_factory: sessionmaker,
    *,
    project_id: str,
    amount: Decimal,
    currency: str,
    job_id: Optional[str] = None,
) -> str:
    """
    Create a PendingCharge row in state PENDING and return its idempotency_key.
    """
    session: Session = session_factory()
    try:
        project = (
            session.query(Project)
                .filter(Project.project_id == str(project_id))
                .one_or_none()
        )
        if project is None:
            raise ValueError(f"Project not found: {project_id}")

        key = str(uuid4())

        pending = PendingCharge(
            idempotency_key=key,
            user_id=project.user_id,
            project_id=project.project_id,
            job_id=job_id,
            amount=amount,
            currency=currency,
            status="PENDING",
        )
        session.add(pending)
        session.commit()
        return key
    finally:
        session.close()
