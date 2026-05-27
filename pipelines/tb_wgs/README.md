# TB WGS Pipeline

This Snakemake workflow defines the auditable path from raw reads to the standard artefacts consumed by the reporting application.

Required outputs:

- `sample_qc_metrics.csv`
- `masked_alignment.fasta`
- `snp_distance_matrix.tsv`
- `lineage_calls.csv`
- `resistance_calls.csv`
- `pipeline_manifest.json`

The validation fixture uses a deterministic in-repository reference implementation so CI can run quickly without large external TB databases. Production deployments should replace the reference implementation rule internals with locked BWA/Samtools/Bcftools/TB-Profiler container commands while preserving the same output contract.

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
snakemake --snakefile pipelines/tb_wgs/Snakefile --profile pipelines/tb_wgs/profiles/singularity
```
