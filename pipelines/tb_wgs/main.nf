nextflow.enable.dsl = 2

include { FASTP } from './modules/fastp'
include { BWA_MEM } from './modules/bwa_mem'
include { SAMTOOLS_QC } from './modules/samtools_qc'
include { BCFTOOLS_CALL } from './modules/bcftools_call'
include { AGGREGATE_SAMPLE_OUTPUTS } from './modules/aggregate_sample_outputs'
include { MASK_REGIONS } from './modules/mask_regions'
include { SNP_DISTS } from './modules/snp_dists'
include { CLUSTER_ASSIGNMENTS } from './modules/cluster_assignments'
include { TBPROFILER } from './modules/tbprofiler'
include { PIPELINE_MANIFEST } from './modules/pipeline_manifest'

workflow TB_WGS {
  /*
   * Production workflow skeleton:
   * FASTQ -> fastp QC -> BWA-MEM mapping -> Samtools QC -> Bcftools variants
   * -> masking/masked alignment -> snp-dists matrix -> TBProfiler lineage/DR
   * -> pipeline_manifest.json.
   *
   * The validation CI currently runs the deterministic Snakemake fixture in
   * this repository because it is small and does not require downloading TB
   * reference databases. These module boundaries define the production
   * Nextflow contract to be filled with validated command invocations.
   */
  samples = Channel
    .fromPath(params.sample_sheet)
    .splitCsv(header: true)
    .map { row -> tuple(row.sample_id, file(row.fastq_1), row.fastq_2 ? file(row.fastq_2) : []) }
  reference = Channel.fromPath(params.reference)
  mask_bed = Channel.fromPath(params.mask_bed)

  FASTP(samples)
  BWA_MEM(FASTP.out.cleaned_reads, reference)
  SAMTOOLS_QC(BWA_MEM.out.mapped)
  BCFTOOLS_CALL(BWA_MEM.out.mapped, reference)
  TBPROFILER(FASTP.out.cleaned_reads)

  sample_qc_files = SAMTOOLS_QC.out.sample_qc.map { sample_id, qc -> qc }.collect()
  mapping_summary_files = SAMTOOLS_QC.out.mapping_summary.map { sample_id, mapping -> mapping }.collect()
  variant_files = BCFTOOLS_CALL.out.vcf.map { sample_id, vcf -> vcf }.collect()
  lineage_files = TBPROFILER.out.lineage.map { sample_id, lineage -> lineage }.collect()
  resistance_files = TBPROFILER.out.resistance.map { sample_id, resistance -> resistance }.collect()

  AGGREGATE_SAMPLE_OUTPUTS(sample_qc_files, mapping_summary_files, variant_files, lineage_files, resistance_files)
  MASK_REGIONS(AGGREGATE_SAMPLE_OUTPUTS.out.variants, reference, mask_bed)
  SNP_DISTS(MASK_REGIONS.out.masked_alignment)
  CLUSTER_ASSIGNMENTS(SNP_DISTS.out.snp_matrix, params.cluster_threshold)
  PIPELINE_MANIFEST(
    AGGREGATE_SAMPLE_OUTPUTS.out.sample_qc,
    AGGREGATE_SAMPLE_OUTPUTS.out.mapping_summary,
    AGGREGATE_SAMPLE_OUTPUTS.out.variants,
    MASK_REGIONS.out.masked_alignment,
    SNP_DISTS.out.snp_matrix,
    CLUSTER_ASSIGNMENTS.out.cluster_assignments,
    AGGREGATE_SAMPLE_OUTPUTS.out.lineage,
    AGGREGATE_SAMPLE_OUTPUTS.out.resistance
  )
}

workflow {
  TB_WGS()
}
