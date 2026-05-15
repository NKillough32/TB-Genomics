import scripts.validate_ingest_files as validate
from scripts.prepare_ni_data import _rewrite_fasta_headers, _stable_uuid


def _reset_validate_findings():
    validate._findings.clear()
    validate._fail_count = 0
    validate._warn_count = 0


def test_rewrite_fasta_headers_maps_lab_ids_to_case_uuids(tmp_path):
    case_uuid = _stable_uuid("NIBT-2026-0001")
    source = tmp_path / "source.fasta"
    source.write_text(">NIBT-2026-0001 description\nACGT\n", encoding="utf-8")

    out_dir = tmp_path / "bundle"
    _rewrite_fasta_headers(source, out_dir, {"NIBT-2026-0001": case_uuid})

    assert (out_dir / "dna.fasta").read_text(encoding="utf-8") == (
        f">{case_uuid} description\nACGT\n"
    )


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
