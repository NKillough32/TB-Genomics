process TBPROFILER {
  tag "tbprofiler"
  container "quay.io/biocontainers/tb-profiler:6.6.5--pyhdfd78af_0"

  input:
  path cleaned_reads

  output:
  path "lineage_calls.csv", emit: lineage
  path "resistance_calls.csv", emit: resistance

  script:
  """
  # Production command placeholder:
  # tb-profiler profile --read1 <R1> --read2 <R2> --prefix <sample>
  echo "sample_id,lineage,sublineage,source_tool" > lineage_calls.csv
  echo "sample_id,drug,gene,mutation,prediction,confidence,source_tool" > resistance_calls.csv
  """
}
