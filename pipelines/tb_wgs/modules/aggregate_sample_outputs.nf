process AGGREGATE_SAMPLE_OUTPUTS {
  tag "aggregate_sample_outputs"
  container "quay.io/biocontainers/bcftools:1.21--h3a4d415_1"

  input:
  path sample_qc_files
  path mapping_summary_files
  path variant_files
  path lineage_files
  path resistance_files

  output:
  path "sample_qc_metrics.csv", emit: sample_qc
  path "mapping_summary.csv", emit: mapping_summary
  path "variants.vcf.gz", emit: variants
  path "lineage_calls.csv", emit: lineage
  path "resistance_calls.csv", emit: resistance

  script:
  """
  merge_csv() {
    first=1
    output="\$1"
    shift
    : > "\${output}"
    for file in "\$@"; do
      if [[ "\${first}" -eq 1 ]]; then
        cat "\${file}" >> "\${output}"
        first=0
      else
        tail -n +2 "\${file}" >> "\${output}"
      fi
    done
  }

  merge_csv sample_qc_metrics.csv *.sample_qc_metrics.csv
  merge_csv mapping_summary.csv *.mapping_summary.csv
  merge_csv lineage_calls.csv *.lineage_calls.csv
  merge_csv resistance_calls.csv *.resistance_calls.csv

  shopt -s nullglob
  vcfs=( *.variants.vcf.gz )
  if [[ "\${#vcfs[@]}" -eq 1 ]]; then
    cp "\${vcfs[0]}" variants.vcf.gz
  else
    for vcf in "\${vcfs[@]}"; do
      bcftools index -t --force "\${vcf}"
    done
    bcftools merge --force-samples -Oz -o variants.vcf.gz "\${vcfs[@]}"
  fi
  bcftools index -t variants.vcf.gz
  """
}
