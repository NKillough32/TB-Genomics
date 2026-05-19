from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import SessionLocal
from backend.synthesis.transmission_synthesis import build_transmission_synthesis


EXPORT_PATH = Path("exports") / "synthesis_output.json"


def _json_default(value: Any) -> str | float:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def main() -> int:
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        payload = build_transmission_synthesis(db=db, cluster_id=None)
    finally:
        db.close()

    EXPORT_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )

    summary = payload.get("summary") if isinstance(payload, dict) else {}
    print(
        "Transmission synthesis written to "
        f"{EXPORT_PATH} "
        f"({int((summary or {}).get('cluster_count') or 0)} clusters, "
        f"{int((summary or {}).get('pair_count') or 0)} pairs)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
