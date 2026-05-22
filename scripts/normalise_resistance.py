#!/usr/bin/env python3
"""Normalise TB-Profiler resistance outputs into resistance_calls.

This script deliberately does not call resistance itself. It ingests outputs
from specialist tools, primarily TB-Profiler, and stores auditable per-drug /
per-mutation evidence for dashboards, alerts, and reports.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from backend.database import SessionLocal


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _tool_version(payload: dict[str, Any]) -> str | None:
    version = payload.get("tbprofiler_version") or payload.get("version") or payload.get("pipeline_version")
    if not version and isinstance(payload.get("pipeline"), dict):
        version = payload["pipeline"].get("software_version")
    if isinstance(version, dict):
        return str(_first(version.get("tbprofiler"), version.get("version"), version.get("name")))
    return str(version) if version else None


def _database_version(payload: dict[str, Any]) -> str | None:
    db_version = payload.get("db_version") or payload.get("database_version") or payload.get("catalogue")
    if not db_version and isinstance(payload.get("pipeline"), dict):
        db_version = payload["pipeline"].get("db_version")
    if isinstance(db_version, dict):
        return str(_first(db_version.get("name"), db_version.get("version"), db_version.get("commit")))
    return str(db_version) if db_version else None


def _lineage(payload: dict[str, Any]) -> str | None:
    lineage = payload.get("main_lineage") or payload.get("lineage") or payload.get("lineage_name")
    if isinstance(lineage, list):
        first = lineage[0] if lineage else None
        if isinstance(first, dict):
            return str(_first(first.get("lineage"), first.get("id")))
        return str(first) if first else None
    if isinstance(lineage, dict):
        return str(_first(lineage.get("lineage"), lineage.get("id")))
    return str(lineage) if lineage else None


def _drug_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value is None:
        return []
    text_value = str(value).strip()
    if not text_value:
        return []
    if "," in text_value:
        return [part.strip() for part in text_value.split(",") if part.strip()]
    return [text_value]


def _mutation_name(call: dict[str, Any]) -> str | None:
    return str(
        _first(
            call.get("mutation"),
            call.get("change"),
            call.get("protein_change"),
            call.get("nucleotide_change"),
            call.get("variant"),
            call.get("name"),
        )
        or ""
    ) or None


def normalise_tbprofiler_json(path: Path, sample_id: str | None = None) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return []

    resolved_sample = sample_id or str(_first(payload.get("sample_id"), payload.get("id"), path.stem.replace(".results", "")))
    tool_version = _tool_version(payload)
    database_version = _database_version(payload)
    lineage = _lineage(payload)
    calls: list[dict[str, Any]] = []

    variants = payload.get("dr_variants") or payload.get("resistance_variants") or []
    if isinstance(variants, dict):
        variants = variants.get("variants") or variants.get("calls") or []

    for call in variants if isinstance(variants, list) else []:
        if not isinstance(call, dict):
            continue
        drugs = _drug_values(_first(call.get("drug"), call.get("drugs"), call.get("Drug")))
        if not drugs:
            drugs = ["unknown"]
        for drug in drugs:
            calls.append(
                {
                    "sample_id": resolved_sample,
                    "drug": drug,
                    "gene": _first(call.get("gene"), call.get("Gene")),
                    "mutation": _mutation_name(call),
                    "prediction": _first(call.get("prediction"), call.get("predict"), "R"),
                    "confidence": _as_float(_first(call.get("confidence"), call.get("freq_confidence"))),
                    "depth": _as_float(_first(call.get("depth"), call.get("read_depth"), call.get("coverage"))),
                    "alt_fraction": _as_float(_first(call.get("freq"), call.get("alt_fraction"), call.get("support_fraction"))),
                    "lineage": lineage,
                    "source_tool": "tbprofiler",
                    "tool_version": tool_version,
                    "database_version": database_version,
                    "source_path": str(path.as_posix()),
                    "raw_call": call,
                }
            )

    dr_payload = payload.get("dr") or payload.get("drug_resistance") or payload.get("resistance")
    if isinstance(dr_payload, dict):
        for drug, value in dr_payload.items():
            prediction = value.get("predict") if isinstance(value, dict) else value
            if prediction is None:
                continue
            calls.append(
                {
                    "sample_id": resolved_sample,
                    "drug": str(drug),
                    "gene": None,
                    "mutation": None,
                    "prediction": str(prediction),
                    "confidence": None,
                    "depth": None,
                    "alt_fraction": None,
                    "lineage": lineage,
                    "source_tool": "tbprofiler",
                    "tool_version": tool_version,
                    "database_version": database_version,
                    "source_path": str(path.as_posix()),
                    "raw_call": value if isinstance(value, dict) else {"prediction": value},
                }
            )

    drtype = payload.get("drtype")
    if not calls and drtype:
        calls.append(
            {
                "sample_id": resolved_sample,
                "drug": "overall",
                "gene": None,
                "mutation": None,
                "prediction": str(drtype),
                "confidence": None,
                "depth": None,
                "alt_fraction": None,
                "lineage": lineage,
                "source_tool": "tbprofiler",
                "tool_version": tool_version,
                "database_version": database_version,
                "source_path": str(path.as_posix()),
                "raw_call": {
                    "drtype": drtype,
                    "schema_version": payload.get("schema_version"),
                    "qc": payload.get("qc"),
                },
            }
        )

    return calls


def normalise_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sample_id = _first(row.get("sample_id"), row.get("Sample"), row.get("sample"))
            drug = _first(row.get("drug"), row.get("Drug"))
            if not sample_id or not drug:
                continue
            rows.append(
                {
                    "sample_id": str(sample_id),
                    "drug": str(drug),
                    "gene": _first(row.get("gene"), row.get("Gene")),
                    "mutation": _first(row.get("mutation"), row.get("change"), row.get("variant")),
                    "prediction": _first(row.get("prediction"), row.get("predict"), row.get("resistance"), row.get("call")),
                    "confidence": _as_float(row.get("confidence")),
                    "depth": _as_float(_first(row.get("depth"), row.get("read_depth"))),
                    "alt_fraction": _as_float(_first(row.get("alt_fraction"), row.get("freq"))),
                    "lineage": row.get("lineage"),
                    "source_tool": row.get("source_tool") or "tbprofiler",
                    "tool_version": row.get("tool_version"),
                    "database_version": row.get("database_version"),
                    "source_path": str(path.as_posix()),
                    "raw_call": row,
                }
            )
    return rows


def upsert_resistance_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    db = SessionLocal()
    imported = 0
    try:
        for call in calls:
            params = {
                **call,
                "gene": call.get("gene") or "",
                "mutation": call.get("mutation") or "",
                "raw_call": json.dumps(call.get("raw_call") or {}),
            }
            db.execute(
                text(
                    """
                    INSERT INTO resistance_calls (
                        sample_id, drug, gene, mutation, prediction, confidence,
                        depth, alt_fraction, lineage, source_tool, tool_version,
                        database_version, source_path, raw_call
                    )
                    VALUES (
                        CAST(:sample_id AS uuid), :drug, :gene, :mutation, :prediction,
                        :confidence, :depth, :alt_fraction, :lineage, :source_tool,
                        :tool_version, :database_version, :source_path, CAST(:raw_call AS jsonb)
                    )
                    ON CONFLICT (sample_id, drug, gene, mutation, source_tool) DO UPDATE SET
                        prediction = EXCLUDED.prediction,
                        confidence = EXCLUDED.confidence,
                        depth = EXCLUDED.depth,
                        alt_fraction = EXCLUDED.alt_fraction,
                        lineage = EXCLUDED.lineage,
                        tool_version = EXCLUDED.tool_version,
                        database_version = EXCLUDED.database_version,
                        source_path = EXCLUDED.source_path,
                        raw_call = EXCLUDED.raw_call,
                        created_at = NOW()
                    """
                ),
                params,
            )
            imported += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"status": "completed", "imported_rows": imported}


def normalise_paths(paths: list[Path]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix.lower() == ".json":
            calls.extend(normalise_tbprofiler_json(path))
        elif path.suffix.lower() == ".csv":
            calls.extend(normalise_csv(path))
    return calls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    calls = normalise_paths(args.paths)
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "normalised_rows": len(calls), "calls": calls}, indent=2, default=str))
        return 0

    result = upsert_resistance_calls(calls)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
