from scripts.run_lineage_dr_validation import (
    _discover_fasta_inputs,
    _import_status,
    _resistance_validation_record,
)


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


def test_resistance_validation_suppresses_unusual_gene_drug_mapping():
    record = _resistance_validation_record(
        sample_id="case-1",
        drug="pyrazinamide",
        gene="gyrA",
        mutation="A90V",
        confidence="high",
        predicted_drug_resistance={"pyrazinamide": "resistant"},
        catalogue="local-test",
    )

    assert record["mapping_status"] == "unusual_gene_drug_mapping"
    assert record["report_status"] == "suppressed"
    assert record["clinical_status"] == "do_not_report_mapping_error"


def test_resistance_validation_keeps_expected_gene_not_validated():
    record = _resistance_validation_record(
        sample_id="case-2",
        drug="rifampicin",
        gene="rpoB",
        mutation="S450L",
        confidence="high",
        predicted_drug_resistance={"rifampicin": "resistant"},
        catalogue="local-test",
    )

    assert record["mapping_status"] == "expected_gene"
    assert record["report_status"] == "not_validated"
    assert record["clinical_status"] == "requires_phenotypic_dst_confirmation"

