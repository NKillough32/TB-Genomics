import os
from typing import Dict, Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

SYNTHETIC_INTERPRETATION_MARKER = "Synthetic interpretation grounded in public incidence trends%"


def get_data_safety_status(db: Session) -> Dict[str, Any]:
    """Return whether current dataset is safe for operational public-health actions."""
    total_cases = int(db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0)

    synthetic_case_count = int(
        db.execute(
            text(
                """
                SELECT CASE
                    WHEN to_regclass('public.tb_interpretation') IS NULL THEN 0
                    ELSE (
                        SELECT COUNT(*)
                        FROM tb_interpretation
                        WHERE COALESCE(interpretation_summary, '') ILIKE :marker
                    )
                END
                """
            ),
            {"marker": SYNTHETIC_INTERPRETATION_MARKER},
        ).scalar()
        or 0
    )

    synthetic_seed_events = int(
        db.execute(
            text(
                """
                SELECT CASE
                    WHEN to_regclass('public.audit_log') IS NULL THEN 0
                    ELSE (
                        SELECT COUNT(*)
                        FROM audit_log
                        WHERE action = 'seed_synthetic_dataset'
                    )
                END
                """
            )
        ).scalar()
        or 0
    )

    operational_safe = (synthetic_case_count == 0) and (synthetic_seed_events == 0)
    mode = "operational" if operational_safe else "demo"

    message = (
        "Dataset cleared for operational use."
        if operational_safe
        else "Synthetic/demo data detected. Operational public-health actions are blocked by policy."
    )

    return {
        "mode": mode,
        "mode_label": "Operational" if operational_safe else "Demo",
        "allowed_modes": ["demo", "validation", "operational"],
        "operational_safe": operational_safe,
        "total_cases": total_cases,
        "synthetic_case_count": synthetic_case_count,
        "synthetic_seed_events": synthetic_seed_events,
        "message": message,
    }


def enforce_operational_dataset(db: Session, action_label: str) -> Dict[str, Any]:
    """Raise HTTP 409 when dataset is not operational-safe unless explicitly overridden."""
    status = get_data_safety_status(db)
    if status["operational_safe"]:
        return status

    if os.getenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS", "0") == "1":
        return status

    raise HTTPException(
        status_code=409,
        detail={
            "error": "blocked_non_operational_dataset",
            "action": action_label,
            "message": status["message"],
            "data_safety": status,
            "override_env": "TB_ALLOW_NON_OPERATIONAL_ACTIONS=1",
        },
    )

