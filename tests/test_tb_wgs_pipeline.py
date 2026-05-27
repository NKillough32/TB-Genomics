import json
import gzip
from pathlib import Path

from scripts.tb_wgs_aggregate_outputs import aggregate_sample_outputs
from scripts.tb_wgs_reference_pipeline import run_pipeline
from scripts.validate_tb_wgs_outputs import REQUIRED_OUTPUTS, validate_contract, validate_outputs
from validation.run_validation import run_validation


class Args:
    sample_sheet = "validation/tb_wgs/samples.csv"
    reference = "validation/tb_wgs/reference/ref.fasta"
    mask_bed = "validation/tb_wgs/reference/mask.bed"
    min_reads = 1
    min_bases = 20
    max_ambiguous_percent = 5.0
    min_mean_depth = 1.0
    cluster_threshold = 12

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
    assert manifest["reference"] == "H37Rv NC_000962.3"
    assert manifest["mask_file_sha256"]
    assert manifest["tb_profiler_version"] == "fixture"
    assert manifest["sample_count"] == 3
    for output_name in REQUIRED_OUTPUTS:
        if output_name != "pipeline_manifest.json":
            assert output_name in manifest["output_hashes"]


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
    assert Path("pipelines/tb_wgs/snakemake_profiles/singularity/config.yaml").exists()
    assert Path("validation/tb_wgs/schemas/pipeline_manifest.schema.json").exists()


def test_tb_wgs_nextflow_contract_files_exist():
    required = [
        "pipelines/tb_wgs/main.nf",
        "pipelines/tb_wgs/nextflow.config",
        "pipelines/tb_wgs/modules/fastp.nf",
        "pipelines/tb_wgs/modules/bwa_mem.nf",
        "pipelines/tb_wgs/modules/samtools_qc.nf",
        "pipelines/tb_wgs/modules/bcftools_call.nf",
        "pipelines/tb_wgs/modules/aggregate_sample_outputs.nf",
        "pipelines/tb_wgs/modules/mask_regions.nf",
        "pipelines/tb_wgs/modules/snp_dists.nf",
        "pipelines/tb_wgs/modules/cluster_assignments.nf",
        "pipelines/tb_wgs/modules/tbprofiler.nf",
        "pipelines/tb_wgs/modules/pipeline_manifest.nf",
    ]
    for path in required:
        assert Path(path).exists()


def test_tb_wgs_nextflow_placeholders_match_validation_contract():
    main_nf = Path("pipelines/tb_wgs/main.nf").read_text(encoding="utf-8")
    cluster_nf = Path("pipelines/tb_wgs/modules/cluster_assignments.nf").read_text(encoding="utf-8")
    samtools_nf = Path("pipelines/tb_wgs/modules/samtools_qc.nf").read_text(encoding="utf-8")
    manifest_nf = Path("pipelines/tb_wgs/modules/pipeline_manifest.nf").read_text(encoding="utf-8")
    fastp_nf = Path("pipelines/tb_wgs/modules/fastp.nf").read_text(encoding="utf-8")

    assert "CLUSTER_ASSIGNMENTS(SNP_DISTS.out.snp_matrix, params.cluster_threshold)" in main_nf
    assert "val cluster_threshold" in cluster_nf
    assert "sample_id,read_count,total_bases,mean_depth" in samtools_nf
    assert 'path("${sample_id}_R*.fastq")' in fastp_nf
    assert "write_text(\"\")" not in fastp_nf
    for field in [
        "pipeline_name",
        "generated_at",
        "workflow_engine",
        "reference_path",
        "mask_bed",
        "sample_sheet",
        "sample_count",
        "steps",
        "parameters",
    ]:
        assert f'"{field}"' in manifest_nf


def test_root_validation_harness_compares_reportable_outputs(tmp_path):
    report = run_validation(
        Path("validation/test_data/tb_wgs"),
        Path("validation/tb_wgs/expected"),
        tmp_path / "observed",
    )

    assert report["status"] == "pass"
    assert report["qc_pass"] == 3
    assert report["resistance_calls"] == 1
    assert report["clustered_samples"] == 3


def test_tb_wgs_aggregate_outputs_preserves_rows_and_standard_names(tmp_path):
    input_dir = tmp_path / "per_sample"
    output_dir = tmp_path / "aggregated"
    input_dir.mkdir()

    qc_header = (
        "sample_id,read_count,total_bases,mean_depth,coverage_breadth,"
        "ambiguous_base_percent,contamination_flag,qc_status,qc_failure_reason\n"
    )
    (input_dir / "A.sample_qc_metrics.csv").write_text(qc_header + "A,10,400,10,100,0,false,pass,\n", encoding="utf-8")
    (input_dir / "B.sample_qc_metrics.csv").write_text(qc_header + "B,12,480,12,100,0,false,pass,\n", encoding="utf-8")
    (input_dir / "A.mapping_summary.csv").write_text(
        "sample_id,reference,read_count,mapped_reads,mapped_percent\nA,H37Rv,10,10,100\n",
        encoding="utf-8",
    )
    (input_dir / "B.mapping_summary.csv").write_text(
        "sample_id,reference,read_count,mapped_reads,mapped_percent\nB,H37Rv,12,12,100\n",
        encoding="utf-8",
    )
    (input_dir / "A.lineage_calls.csv").write_text("sample_id,lineage,sublineage,source_tool\nA,4,4.1,fixture\n", encoding="utf-8")
    (input_dir / "B.lineage_calls.csv").write_text("sample_id,lineage,sublineage,source_tool\nB,4,4.1,fixture\n", encoding="utf-8")
    (input_dir / "A.resistance_calls.csv").write_text(
        "sample_id,drug,gene,mutation,prediction,confidence,source_tool\n",
        encoding="utf-8",
    )
    (input_dir / "B.resistance_calls.csv").write_text(
        "sample_id,drug,gene,mutation,prediction,confidence,source_tool\nB,rifampicin,rpoB,S450L,resistant,high,fixture\n",
        encoding="utf-8",
    )
    for sample, pos in [("A", 17), ("B", 28)]:
        with gzip.open(input_dir / f"{sample}.variants.vcf.gz", "wt", encoding="utf-8") as handle:
            handle.write("##fileformat=VCFv4.2\n")
            handle.write("##INFO=<ID=SAMPLEID,Number=1,Type=String,Description=\"Sample ID\">\n")
            handle.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            handle.write(f"ref\t{pos}\t.\tA\tT\t.\tPASS\tSAMPLEID={sample}\n")

    aggregate_sample_outputs(input_dir, output_dir)

    qc_rows = (output_dir / "sample_qc_metrics.csv").read_text(encoding="utf-8").splitlines()
    resistance_rows = (output_dir / "resistance_calls.csv").read_text(encoding="utf-8").splitlines()
    with gzip.open(output_dir / "variants.vcf.gz", "rt", encoding="utf-8") as handle:
        vcf_records = [line for line in handle.read().splitlines() if not line.startswith("#")]

    assert len(qc_rows) == 3
    assert qc_rows[0].startswith("sample_id,read_count,total_bases")
    assert len(resistance_rows) == 2
    assert vcf_records == [
        "ref\t17\t.\tA\tT\t.\tPASS\tSAMPLEID=A",
        "ref\t28\t.\tA\tT\t.\tPASS\tSAMPLEID=B",
    ]
