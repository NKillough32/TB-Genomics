import json
from pathlib import Path

from scripts.tb_wgs_reference_pipeline import run_pipeline
from scripts.validate_tb_wgs_outputs import REQUIRED_OUTPUTS, validate_contract, validate_outputs


class Args:
    sample_sheet = "validation/tb_wgs/samples.csv"
    reference = "validation/tb_wgs/reference/ref.fasta"
    mask_bed = "validation/tb_wgs/reference/mask.bed"
    min_reads = 1
    min_bases = 20
    max_ambiguous_percent = 5.0
    min_mean_depth = 1.0

    def __init__(self, outdir: Path):
        self.outdir = str(outdir)


def test_tb_wgs_reference_pipeline_matches_expected_fixture(tmp_path):
    observed = tmp_path / "observed"

    run_pipeline(Args(observed))

    failures = validate_outputs(observed, Path("validation/tb_wgs/expected"))
    assert failures == []


def test_tb_wgs_pipeline_manifest_records_required_outputs(tmp_path):
    observed = tmp_path / "observed"

    run_pipeline(Args(observed))

    manifest = json.loads((observed / "pipeline_manifest.json").read_text(encoding="utf-8"))
    assert manifest["workflow_engine"] == "snakemake"
    assert manifest["sample_count"] == 3
    for output_name in REQUIRED_OUTPUTS:
        if output_name != "pipeline_manifest.json":
            assert output_name in manifest["outputs"]


def test_tb_wgs_contract_validation_rejects_non_symmetric_matrix(tmp_path):
    observed = tmp_path / "observed"
    run_pipeline(Args(observed))
    matrix = observed / "snp_distance_matrix.tsv"
    matrix.write_text(
        "sample_id\tSAMPLE_A\tSAMPLE_B\tSAMPLE_C\n"
        "SAMPLE_A\t0\t1\t2\n"
        "SAMPLE_B\t5\t0\t1\n"
        "SAMPLE_C\t2\t1\t0\n",
        encoding="utf-8",
    )

    failures = validate_contract(observed)

    assert any("not symmetric" in failure for failure in failures)


def test_tb_wgs_contract_validation_rejects_bad_qc_status(tmp_path):
    observed = tmp_path / "observed"
    run_pipeline(Args(observed))
    qc_path = observed / "sample_qc_metrics.csv"
    qc_path.write_text(qc_path.read_text(encoding="utf-8").replace(",pass,", ",review,"), encoding="utf-8")

    failures = validate_contract(observed)

    assert any("invalid qc_status" in failure for failure in failures)


def test_tb_wgs_snakemake_contract_files_exist():
    assert Path("pipelines/tb_wgs/Snakefile").exists()
    assert Path("pipelines/tb_wgs/config/default.yml").exists()
    assert Path("pipelines/tb_wgs/profiles/singularity/config.yaml").exists()
    assert Path("validation/tb_wgs/schemas/pipeline_manifest.schema.json").exists()
