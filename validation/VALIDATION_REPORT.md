# TB WGS Pipeline Validation Report

## Validation Status

- Status: Pass
- Validation date: 2026-05-28
- Validation scope: Local/regional pilot validation for the TB WGS output contract
- CI workflow: `.github/workflows/wgs-validation.yml`
- CI result reviewed: `pytest tests/test_tb_wgs_pipeline.py` passed; `python validation/run_validation.py` returned `status: pass`

## Pipeline Version

- Pipeline name: `tb_wgs_reference_validation`
- Pipeline version: `0.1.0`
- Workflow engine recorded by fixture manifest: `snakemake`
- Reference: `H37Rv NC_000962.3`
- Reference path: `validation/tb_wgs/reference/ref.fasta`
- Mask file: `validation/tb_wgs/reference/mask.bed`
- Mask file SHA-256: `e64f221b44ec24e5008f6855278eb091318851aacebac2717bd272f5fdcb107d`
- TBProfiler version: `fixture`
- TBProfiler database version: `fixture`
- SNP distance tool version: `fixture`

## Test Dataset Description

The current validation dataset is a deterministic in-repository fixture designed to verify the output contract quickly in CI. It contains three FASTQ-backed sample records:

| Sample | FASTQ input |
| --- | --- |
| `SAMPLE_A` | `validation/tb_wgs/fastq/SAMPLE_A_R1.fastq` |
| `SAMPLE_B` | `validation/tb_wgs/fastq/SAMPLE_B_R1.fastq` |
| `SAMPLE_C` | `validation/tb_wgs/fastq/SAMPLE_C_R1.fastq` |

Input files are recorded in `validation/tb_wgs/expected/pipeline_manifest.json` with SHA-256 hashes for the sample sheet, reference, mask file, and FASTQ files.

## Expected Outputs

Expected outputs are stored in `validation/tb_wgs/expected` and define the current validation truth set:

| Output | Purpose |
| --- | --- |
| `sample_qc_metrics.csv` | Per-sample read count, depth, breadth, ambiguity, contamination flag, and QC status |
| `mapping_summary.csv` | Per-sample mapping summary against the validation reference |
| `variants.vcf.gz` | Variant calls in VCF format |
| `masked_alignment.fasta` | Masked consensus alignment used for SNP distance calculation |
| `snp_distance_matrix.tsv` | Pairwise SNP distance matrix |
| `lineage_calls.csv` | Expected lineage/sublineage calls |
| `resistance_calls.csv` | Expected resistance call rows |
| `cluster_assignments.csv` | Expected SNP-threshold cluster assignments |
| `pipeline_manifest.json` | Pipeline metadata, parameters, and input/output hashes |

## Observed Outputs

The validation harness writes observed outputs to `validation/observed_outputs/tb_wgs` by default. The directory is generated during validation and should not be treated as a source-of-truth fixture.

Validation command:

```bash
python validation/run_validation.py
```

The observed outputs are compared against `validation/tb_wgs/expected` for required file presence, schemas, manifest fields, output hashes where applicable, and reportable output content.

## Pass/Fail Summary

Latest CI-observed validation summary:

| Metric | Result |
| --- | ---: |
| Overall status | Pass |
| Failure count | 0 |
| QC-pass samples | 3 |
| QC-fail samples | 0 |
| Lineage calls | 3 |
| Resistance calls | 1 |
| Clustered samples | 3 |

Required checks:

| Check | Result |
| --- | --- |
| `pytest tests/test_tb_wgs_pipeline.py` | Pass, 9 tests |
| `python validation/run_validation.py` | Pass |
| Expected reportable outputs match observed outputs | Pass |
| Required manifest fields present | Pass |
| SNP distance matrix contract checks | Pass |
| QC status contract checks | Pass |

## Known Limitations

- The current dataset is deliberately small and deterministic. It is suitable for CI drift detection but is not a full clinical validation truth set.
- TBProfiler, TBProfiler database, and SNP distance versions are fixture labels in the current validation manifest.
- The current validation set does not yet include a real-world known cluster, known unrelated isolates, known MDR/RR-TB sample, low-quality sample, or mixed/contaminated sample.
- The current validation set does not yet validate phenotypic DST concordance.
- The current fixture validates the platform output contract, not end-to-end clinical performance of a production WGS pipeline.
- Production or service deployment requires locked reference genome, mask file, TBProfiler database, resistance catalogue, container/tool versions, and documented change-control.

## Acceptance Decision

The current WGS validation/reproducibility step is accepted for a local/regional pilot output-contract validation, subject to the limitations above. It is not yet accepted as a complete clinical diagnostic validation.

Any change to SNP thresholds, reference genome, mask file, TBProfiler database, resistance rules, clustering logic, output schemas, or validation expected outputs requires documented review and revalidation.

## Reviewer Sign-Off

| Role | Name | Decision | Date | Signature / initials | Notes |
| --- | --- | --- | --- | --- | --- |
| Bioinformatics reviewer |  |  |  |  |  |
| Clinical / TB service reviewer |  |  |  |  |  |
| Governance / service owner |  |  |  |  |  |

## Revalidation Log

| Date | Change reviewed | Revalidation command/result | Reviewer | Decision |
| --- | --- | --- | --- | --- |
| 2026-05-28 | Initial formal validation report for CI-backed fixture validation | `pytest tests/test_tb_wgs_pipeline.py`: pass; `python validation/run_validation.py`: pass |  | Pending sign-off |
