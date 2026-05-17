from io import BytesIO
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from backend.routers import ingest
import scripts.validate_ingest_files as validate
from scripts.prepare_ni_data import _apply_mapping, _rewrite_fasta_headers, _stable_uuid


def _reset_validate_findings():
    validate.reset_findings()


def test_rewrite_fasta_headers_maps_lab_ids_to_case_uuids(tmp_path):
    case_uuid = _stable_uuid("NIBT-2026-0001")
    source = tmp_path / "source.fasta"
    source.write_text(">NIBT-2026-0001 description\nACGT\n", encoding="utf-8")

    out_dir = tmp_path / "bundle"
    _rewrite_fasta_headers(source, out_dir, {"NIBT-2026-0001": case_uuid})

    assert (out_dir / "dna.fasta").read_text(encoding="utf-8") == (
        f">{case_uuid} description\nACGT\n"
    )


def test_column_mapping_handles_case_insensitive_columns_and_status_values():
    source = {"Lab Sample": "NIBT-2026-0001", "Status": "Confirmed"}
    field_map = {
        "pseudonymised_case_id": {"type": "uuid_from_column", "source_column": "lab sample"},
        "case_status": {
            "type": "map_values",
            "source_column": "status",
            "value_map": {"Confirmed": "confirmed"},
            "fallback": "under_review",
        },
    }

    assert _apply_mapping(source, field_map, "pseudonymised_case_id") == _stable_uuid(
        "NIBT-2026-0001"
    )
    assert _apply_mapping(source, field_map, "case_status") == "confirmed"


def test_strict_validation_fails_missing_analysis_files(tmp_path):
    _reset_validate_findings()
    validate.validate_interpretation(tmp_path / "tb_interpretation.csv", set(), strict=True)
    validate.validate_qc(tmp_path / "sample_qc_metrics.csv", set(), set(), strict=True)
    validate.validate_provenance(tmp_path / "analysis_provenance.csv", set(), strict=True)
    validate.validate_fasta(tmp_path / "dna.fasta", set(), strict=True)

    checks = {check for level, check, _ in validate._findings if level == validate.FAIL}
    assert {
        "tb_interpretation.csv:exists",
        "sample_qc_metrics.csv:exists",
        "analysis_provenance.csv:exists",
        "dna.fasta:exists",
    }.issubset(checks)


def test_strict_validation_fails_fasta_case_mismatch(tmp_path):
    _reset_validate_findings()
    fasta = tmp_path / "dna.fasta"
    fasta.write_text(">different-case\nACGTACGTACGT\n", encoding="utf-8")

    validate.validate_fasta(fasta, {"11111111-1111-4111-8111-111111111111"}, strict=True)

    checks = {check for level, check, _ in validate._findings if level == validate.FAIL}
    assert "dna.fasta:coverage" in checks
    assert "dna.fasta:orphan_seqs" in checks


def test_ingest_file_validates_and_loads_zip_bundle(monkeypatch):
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("cases.csv", "pseudonymised_case_id\n")
    payload.seek(0)

    monkeypatch.setattr(
        ingest,
        "validate_bundle",
        lambda bundle_dir, strict_analysis: {
            "summary": {"pass": 1, "warn": 0, "fail": 0},
            "findings": [],
        },
    )
    monkeypatch.setattr(
        ingest,
        "load_bundle",
        lambda bundle_dir, dry_run: [SimpleNamespace(table="cases", inserted=1, errors=[])],
    )

    response = ingest.ingest_file(
        UploadFile(file=payload, filename="bundle.zip"),
        load_to_database=True,
        dry_run=False,
        strict_analysis=True,
        _auth=None,
    )

    assert response["status"] == "loaded"
    assert response["database_loaded"] is True
    assert response["load"]["cases"]["rows_processed"] == 1


def test_ingest_file_blocks_zip_with_validation_failures(monkeypatch):
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("cases.csv", "pseudonymised_case_id\n")
    payload.seek(0)

    monkeypatch.setattr(
        ingest,
        "validate_bundle",
        lambda bundle_dir, strict_analysis: {
            "summary": {"pass": 0, "warn": 0, "fail": 1},
            "findings": [{"level": "FAIL", "check": "cases.csv:columns", "message": "bad"}],
        },
    )
    monkeypatch.setattr(
        ingest,
        "load_bundle",
        lambda bundle_dir, dry_run: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    response = ingest.ingest_file(
        UploadFile(file=payload, filename="bundle.zip"),
        load_to_database=True,
        dry_run=False,
        strict_analysis=True,
        _auth=None,
    )

    assert response["status"] == "validation_failed"
    assert response["database_loaded"] is False


def test_ingest_file_rejects_unsafe_zip_paths():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../outside.txt", "bad")
    payload.seek(0)

    with pytest.raises(Exception) as exc:
        ingest.ingest_file(
            UploadFile(file=payload, filename="bundle.zip"),
            load_to_database=True,
            dry_run=False,
            strict_analysis=True,
            _auth=None,
        )

    assert getattr(exc.value, "status_code", None) == 400
