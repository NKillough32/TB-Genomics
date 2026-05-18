import json
import os

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.routers.cases import (
    _derive_effective_engine_status,
    _export_path,
    _lineage_analysis_summary,
    _lineage_epi_summary,
    get_db,
)


router = APIRouter(prefix="/cases", tags=["cases"])


@router.get("/outbreaker-analysis")
def outbreaker_analysis():
    """Return outbreaker2 analysis results including graphics and summary."""
    result = {
        "status": "no_results",
        "message": "Analysis has not been run yet",
        "graphics": [],
        "summary": None,
        "transmission_network": None,
        "secondary_validation": None,
        "provenance": None,
        "is_mock": None,
    }

    summary_path = _export_path("outbreaker_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r") as f:
                result["summary"] = json.load(f)
                result["status"] = "completed"
                summary_provenance = result["summary"].get("data_provenance") if isinstance(result["summary"], dict) else None
                if summary_provenance in {"real", "mock"}:
                    result["provenance"] = summary_provenance
                    result["is_mock"] = summary_provenance == "mock"
        except Exception as e:
            result["status"] = "error"
            result["message"] = str(e)

    network_path = _export_path("transmission_network.json")
    if os.path.exists(network_path):
        try:
            with open(network_path, "r", encoding="utf-8") as f:
                result["transmission_network"] = json.load(f)
                if result["provenance"] is None and isinstance(result["transmission_network"], dict):
                    network_provenance = result["transmission_network"].get("provenance")
                    if network_provenance in {"real", "mock"}:
                        result["provenance"] = network_provenance
                        result["is_mock"] = network_provenance == "mock"
        except Exception:
            result["transmission_network"] = None

    secondary_path = _export_path("secondary_engine_validation.json")
    if os.path.exists(secondary_path):
        try:
            with open(secondary_path, "r", encoding="utf-8") as f:
                result["secondary_validation"] = json.load(f)
        except Exception:
            result["secondary_validation"] = None

    if result["provenance"] is None:
        result["provenance"] = "unknown"
        result["is_mock"] = None

    graphics_dir = "exports"
    if os.path.exists(graphics_dir):
        graphics_files = [
            f for f in os.listdir(graphics_dir)
            if f.startswith("outbreaker_") and f.endswith(".png")
        ]
        order = {"outbreaker_trace.png": 0, "outbreaker_hist.png": 1, "outbreaker_tree.png": 2}
        graphics_files.sort(key=lambda f: order.get(f, 999))

        for file in graphics_files:
            result["graphics"].append({
                "name": file,
                "url": f"/cases/outbreaker-image/{file}",
                "type": file.replace("outbreaker_", "").replace(".png", ""),
            })

    return result


@router.get("/lineage-dr-validation")
def lineage_dr_validation(db: Session = Depends(get_db)):
    """Return lineage/drug-resistance integration validation artifact."""
    analysis_summary = _lineage_analysis_summary(db)
    analysis_epi_summary = _lineage_epi_summary(db)

    path = _export_path("lineage_dr_validation.json")
    if not os.path.exists(path):
        return {
            "status": "no_results",
            "message": "Lineage/DR validation has not been run yet",
            "artifact_path": path,
            "analysis_summary": analysis_summary,
            "analysis_epi_summary": analysis_epi_summary,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "artifact_path": path,
            "analysis_summary": analysis_summary,
            "analysis_epi_summary": analysis_epi_summary,
        }

    payload["artifact_path"] = path
    payload["analysis_summary"] = analysis_summary
    payload["analysis_epi_summary"] = analysis_epi_summary
    payload["effective_engines"] = _derive_effective_engine_status(payload)
    return payload
