process MASK_REGIONS {
  tag "mask_regions"
  container "python:3.11-slim"

  input:
  path vcf
  path reference
  path mask_bed

  output:
  path "masked_alignment.fasta", emit: masked_alignment

  script:
  """
  python - <<'PY'
  import gzip
  from pathlib import Path

  def read_fasta(path):
      name = None
      chunks = []
      for line in Path(path).read_text(encoding="utf-8").splitlines():
          if line.startswith(">"):
              if name is None:
                  name = line[1:].split()[0]
          else:
              chunks.append(line.strip())
      return name or "reference", list("".join(chunks).upper())

  def open_text(path):
      return gzip.open(path, "rt", encoding="utf-8") if str(path).endswith(".gz") else open(path, encoding="utf-8")

  _ref_name, reference = read_fasta("${reference}")
  masks = []
  mask_path = Path("${mask_bed}")
  if mask_path.exists():
      for line in mask_path.read_text(encoding="utf-8").splitlines():
          if not line.strip() or line.startswith("#"):
              continue
          fields = line.split("\\t")
          if len(fields) >= 3:
              masks.append((int(fields[1]), int(fields[2])))

  samples = []
  sequences = {}
  with open_text("${vcf}") as handle:
      for line in handle:
          line = line.rstrip("\\n")
          if line.startswith("#CHROM"):
              header = line.split("\\t")
              samples = header[9:] if len(header) > 9 else []
              for sample in samples:
                  sequences[sample] = reference.copy()
              continue
          if line.startswith("#") or not line:
              continue
          fields = line.split("\\t")
          if len(fields) < 8:
              continue
          pos = int(fields[1]) - 1
          ref = fields[3]
          alts = fields[4].split(",")
          if len(ref) != 1:
              continue
          if samples and len(fields) >= 10:
              fmt = fields[8].split(":")
              gt_index = fmt.index("GT") if "GT" in fmt else None
              for sample, sample_field in zip(samples, fields[9:]):
                  if gt_index is None:
                      continue
                  gt = sample_field.split(":")[gt_index].replace("|", "/")
                  allele_indexes = [part for part in gt.split("/") if part and part != "."]
                  if not allele_indexes:
                      continue
                  allele_index = max(int(part) for part in allele_indexes)
                  if allele_index > 0 and allele_index <= len(alts) and len(alts[allele_index - 1]) == 1:
                      sequences[sample][pos] = alts[allele_index - 1]
          elif "SAMPLEID=" in fields[7]:
              info = dict(item.split("=", 1) for item in fields[7].split(";") if "=" in item)
              sample = info.get("SAMPLEID")
              if sample:
                  sequences.setdefault(sample, reference.copy())
                  if alts and len(alts[0]) == 1:
                      sequences[sample][pos] = alts[0]

  if not sequences:
      sequences["reference"] = reference.copy()

  for sequence in sequences.values():
      for start, end in masks:
          for idx in range(max(0, start), min(len(sequence), end)):
              sequence[idx] = "N"

  with open("masked_alignment.fasta", "w", encoding="utf-8") as handle:
      for sample in sorted(sequences):
          handle.write(f">{sample}\\n{''.join(sequences[sample])}\\n")
  PY
  """
}
