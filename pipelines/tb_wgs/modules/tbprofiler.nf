process TBPROFILER {
  tag "${sample_id}"
  container "quay.io/biocontainers/tb-profiler:6.6.5--pyhdfd78af_0"

  input:
  tuple val(sample_id), path(reads)

  output:
  tuple val(sample_id), path("${sample_id}.lineage_calls.csv"), emit: lineage
  tuple val(sample_id), path("${sample_id}.resistance_calls.csv"), emit: resistance

  script:
  """
  # Production command placeholder:
  # Use one or two FASTQs from ${reads}.
  # For paired-end data: tb-profiler profile --read1 <R1> --read2 <R2> --prefix ${sample_id}
  # For single-end data: tb-profiler profile --read1 <R1> --prefix ${sample_id}
  echo "sample_id,lineage,sublineage,source_tool" > ${sample_id}.lineage_calls.csv
  echo "sample_id,drug,gene,mutation,prediction,confidence,source_tool" > ${sample_id}.resistance_calls.csv
  """
}
