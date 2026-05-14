from scripts.run_lineage_dr_validation import _discover_fasta_inputs, _import_status


def test_import_status_does_not_report_success_when_all_rows_skipped():
    status, message = _import_status("tb-profiler", imported=0, skipped=10)

    assert status == "no_matching_cases"
    assert "none matched cases" in message


def test_import_status_reports_partial_imports_as_warnings():
    status, message = _import_status("mykrobe", imported=8, skipped=2)

    assert status == "imported_with_warnings"
    assert "skipped unlinked samples" in message


def test_discover_fasta_inputs_prefers_export_dna(monkeypatch, tmp_path):
    root = tmp_path
    exports = root / "exports"
    uploads = root / "uploads"
    exports.mkdir()
    uploads.mkdir()
    export_dna = exports / "dna.fasta"
    export_dna.write_text(">current\nACGT\n", encoding="utf-8")
    upload_dna = uploads / "dna.fasta"
    upload_dna.write_text(">stale\nACGT\n", encoding="utf-8")

    import scripts.run_lineage_dr_validation as mod

    monkeypatch.setattr(mod, "EXPORTS", exports)
    monkeypatch.setattr(mod, "UPLOADS", uploads)
    monkeypatch.delenv("LINEAGE_DR_FASTA", raising=False)
    monkeypatch.delenv("TB_LINEAGE_DR_FASTA", raising=False)

    assert _discover_fasta_inputs() == [export_dna]
