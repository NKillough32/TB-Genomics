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
  read_array=( ${reads} )
  if [[ "\${#read_array[@]}" -gt 1 ]]; then
    tb-profiler profile --read1 "\${read_array[0]}" --read2 "\${read_array[1]}" --prefix "${sample_id}" --dir .
  else
    tb-profiler profile --read1 "\${read_array[0]}" --prefix "${sample_id}" --dir .
  fi

  python - <<'PY'
  import csv, json
  from pathlib import Path

  sample_id = "${sample_id}"
  candidates = sorted(Path(".").glob(f"**/{sample_id}*.results.json")) + sorted(Path(".").glob("**/results.json"))
  if not candidates:
      raise SystemExit("TBProfiler results JSON not found")
  result = json.loads(candidates[0].read_text(encoding="utf-8"))

  lineage = result.get("lineage") or result.get("main_lineage") or ""
  sublineage = result.get("sublineage") or result.get("sublin") or lineage
  if isinstance(lineage, list):
      lineage = lineage[0].get("lineage", "") if lineage and isinstance(lineage[0], dict) else ";".join(map(str, lineage))
  if isinstance(sublineage, list):
      sublineage = sublineage[0].get("lineage", "") if sublineage and isinstance(sublineage[0], dict) else ";".join(map(str, sublineage))

  with open(f"{sample_id}.lineage_calls.csv", "w", newline="", encoding="utf-8") as handle:
      writer = csv.DictWriter(handle, fieldnames=["sample_id", "lineage", "sublineage", "source_tool"])
      writer.writeheader()
      writer.writerow({
          "sample_id": sample_id,
          "lineage": lineage,
          "sublineage": sublineage,
          "source_tool": "tb-profiler",
      })

  rows = []
  resistance_blocks = result.get("dr_variants") or result.get("variants") or []
  if isinstance(resistance_blocks, dict):
      iterable = []
      for drug, variants in resistance_blocks.items():
          for variant in variants or []:
              if isinstance(variant, dict):
                  variant = {**variant, "drug": drug}
              iterable.append(variant)
      resistance_blocks = iterable
  for variant in resistance_blocks or []:
      if not isinstance(variant, dict):
          continue
      drug = variant.get("drug") or variant.get("drugs") or variant.get("drug_name") or ""
      if isinstance(drug, list):
          drug = ";".join(map(str, drug))
      prediction = variant.get("prediction") or variant.get("confers") or variant.get("type") or "resistant"
      rows.append({
          "sample_id": sample_id,
          "drug": drug,
          "gene": variant.get("gene") or variant.get("locus_tag") or "",
          "mutation": variant.get("change") or variant.get("mutation") or variant.get("hgvs") or "",
          "prediction": str(prediction).lower(),
          "confidence": variant.get("confidence") or variant.get("grade") or "",
          "source_tool": "tb-profiler",
      })

  with open(f"{sample_id}.resistance_calls.csv", "w", newline="", encoding="utf-8") as handle:
      writer = csv.DictWriter(handle, fieldnames=[
          "sample_id",
          "drug",
          "gene",
          "mutation",
          "prediction",
          "confidence",
          "source_tool",
      ])
      writer.writeheader()
      writer.writerows(rows)
  PY
  """
}
