# Validation Benchmark Scenarios

This folder contains structured benchmark scenarios for validating and calibrating
pairwise transmission evidence logic.

These datasets are NOT intended as operational truth.
They are synthetic or reviewer-curated reference scenarios used to:

- test scoring behaviour
- evaluate contradiction handling
- assess calibration drift
- compare reviewer agreement
- benchmark future scoring-profile versions

## Suggested workflow

1. Load or simulate the benchmark cases.
2. Run:

```text
GET /analytics/transmission-evidence/{case_a}/{case_b}
GET /analytics/case-pair-calibration
GET /analytics/case-pair-calibration/sweep
```

3. Compare model outputs with expected reviewer classifications.
4. Review disagreements before changing thresholds.

## Included benchmark types

- household_transmission.json
- workplace_cluster.json
- false_genomic_cluster.json
- high_snp_contradiction.json
- sparse_epi_data.json

## Important limitations

- These scenarios are heuristic calibration aids only.
- They do not replace formal epidemiological validation.
- SNP thresholds are context dependent and should not be treated as proof of transmission.
