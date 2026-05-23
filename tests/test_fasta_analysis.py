from scripts import run_fasta_analysis as fasta_analysis


def test_read_fasta_headers_uses_first_token(tmp_path):
    fasta = tmp_path / "input.fasta"
    fasta.write_text(">case-1 description\nACGT\n>case-2\nAAGT\n", encoding="utf-8")

    assert fasta_analysis._read_fasta_headers(fasta) == ["case-1", "case-2"]


def test_select_primary_fasta_prefers_explicit_existing_path(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.fasta"
    explicit.write_text(">case-1\nACGT\n", encoding="utf-8")
    monkeypatch.setenv("FASTA_ANALYSIS_FASTA", str(explicit))

    assert fasta_analysis._select_primary_fasta() == explicit


def test_tool_records_mark_missing_tools_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("FASTA_ANALYSIS_WSL_FALLBACK", "0")
    monkeypatch.setattr(fasta_analysis, "ROOT", tmp_path)

    records = fasta_analysis._tool_records()

    assert records["snp_dists"]["status"] == "missing"
    assert records["iqtree"]["status"] == "missing"
