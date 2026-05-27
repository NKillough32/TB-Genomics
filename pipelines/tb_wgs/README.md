# TB WGS Pipeline

This directory defines the auditable path from raw reads to the standard artefacts consumed by the reporting application. The production-facing workflow is `main.nf`; the Snakemake fixture and Python reference runner remain fast deterministic validation harnesses for CI and local checks.

Required outputs:

- `sample_qc_metrics.csv`
- `mapping_summary.csv`
- `variants.vcf.gz`
- `masked_alignment.fasta`
- `snp_distance_matrix.tsv`
- `cluster_assignments.csv`
- `lineage_calls.csv`
- `resistance_calls.csv`
- `pipeline_manifest.json`

The validation fixture uses a deterministic in-repository reference implementation so CI can run quickly without large external TB databases. The Nextflow modules use production tool commands for fastp, BWA, Samtools, Bcftools, TBProfiler, and snp-dists; deployments still need validated references, mask files, TBProfiler databases, container pinning, version-locked images, and environment-specific runtime testing before operational use.

The Nextflow skeleton is structured around per-sample channel boundaries for read QC, mapping, QC summarisation, variant calling, and TBProfiler profiling. Cohort-level steps begin once sample-level outputs are gathered for masking, SNP distance calculation, cluster assignment, and manifest generation.

Plain-language status:

- This folder defines the expected WGS output contract for the wider platform.
- It is not yet a fully validated clinical WGS pipeline by itself.
- The CI fixture is deliberately small and deterministic so output drift is caught quickly.
- Production deployments should keep the same output file names and schemas so the backend ingest, reporting, and validation code can consume results consistently.

Run the production-facing Nextflow workflow skeleton:

```bash
nextflow run pipelines/tb_wgs/main.nf -c pipelines/tb_wgs/nextflow.config -profile docker
```

Run the validation workflow:

```bash
python scripts/tb_wgs_reference_pipeline.py \
  --sample-sheet validation/tb_wgs/samples.csv \
  --reference validation/tb_wgs/reference/ref.fasta \
  --mask-bed validation/tb_wgs/reference/mask.bed \
  --outdir validation/tb_wgs/observed

python scripts/validate_tb_wgs_outputs.py \
  --observed validation/tb_wgs/observed \
  --expected validation/tb_wgs/expected
```

Run with Snakemake:

```bash
snakemake --snakefile pipelines/tb_wgs/Snakefile --cores 1 --use-singularity
```

Or with the bundled profile:

```bash
snakemake --snakefile pipelines/tb_wgs/Snakefile --profile pipelines/tb_wgs/snakemake_profiles/singularity
```
