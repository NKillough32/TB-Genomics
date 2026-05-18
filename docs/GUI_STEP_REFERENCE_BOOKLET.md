# TB Genomics GUI Reference Booklet

## Purpose

This booklet explains each GUI step in plain language and gives practical guidance on how to interpret the outputs.

Use it as an operational companion while running routine surveillance and investigation workflows.

## Before you start

- This platform supports public health surveillance and investigation prioritization.
- Several analytics outputs are heuristic and non-validated; treat them as decision support, not proof.
- Genomic resistance predictions are not a replacement for phenotypic DST.

## Step map at a glance

- Step 1: System status
- Data readiness panel (between Steps 1 and 2)
- Step 2: Upload files (or generate synthetic data)
- Step 3: Run analysis
- Step 4: Results and exports
- Step 4A: Case search and case reports
- Step 5: Transmission synthesis
- Step 6: Cluster Investigation Centre
- Step 7: Phylogenetic and visual analytics
- Step 8: Final actionable report
- Step 9: Runtime and supporting status
- Step 10: Resistance validation sign-off
- Step 11: Epidemiology reference records
- Step 12: Governance and compliance

---

## Step 1 - System status

### What it does
Shows whether the backend is reachable and key services are responding.

### How to interpret
- Good: status checks return normally, no repeated failures.
- Caution: intermittent warnings may indicate environment instability.
- Action if bad: check API process, database availability, and auth settings before proceeding.

---

## Data readiness panel

### What it does
Shows whether required data elements are present and complete enough for analysis.

### How to interpret
- High readiness: key tables and fields are populated; analysis is likely meaningful.
- Partial readiness: some outputs may still run but confidence and completeness are reduced.
- Low readiness: run ingest/validation first; do not rely on downstream interpretation.

---

## Step 2 - Upload files / synthetic data

### What it does
Lets you upload ingest files or generate synthetic demo data.

### How to interpret
- Upload success messages indicate files were accepted by the server.
- Synthetic mode is for training/demo, not operational decisions.
- Same seed value recreates the same synthetic patterns.

### Practical interpretation tips
- If using real data, confirm operational safety before running public-health actions.
- If using synthetic data, label outputs clearly as demo/training.

---

## Step 3 - Run analysis

### What it does
Runs the full pipeline (or individual jobs): lineage/DR validation, clustering, outbreaker exports/inference, and comparison workflows.

### How to interpret
- Completed job with no critical warnings: proceed to Steps 4-8.
- Completed with warnings: proceed, but annotate caveats in investigation notes.
- Failed job: do not interpret downstream outputs until fixed.

### Workflow confidence panel
- Pass/ready signals: dependencies and gates are acceptable.
- Warning signals: outputs may be usable but should be reviewed with caution.
- Fail signals: pause interpretation and resolve blockers first.

---

## Step 4 - Results and exports

### What it does
Displays core outputs and report/export actions.

### How to interpret
- Outbreaker summary gives transmission-model context; treat as probabilistic support.
- Lineage/DR validation highlights agreement/discordance and quality concerns.
- Export bundles are snapshots of current run state.

### Practical interpretation tips
- Use this step to check whether outputs exist and are coherent before deep investigation.
- If lineage/DR discordance is high, escalate review before sign-off.

---

## Step 4A - Case search and case reports

### What it does
Allows targeted filtering and case-level history/report generation.

### How to interpret
- Useful for checking individual case context inside wider cluster signals.
- Time/region/resistance filters help separate broad cluster trends from case-specific exceptions.

### Practical interpretation tips
- Confirm outlier cases (date, region, lineage mismatch) before concluding cluster spread.
- Use generated case reports as supporting evidence in Step 6 notes.

---

## Step 5 - Transmission synthesis

### What it does
Combines SNP, posterior, temporal, and region evidence into cluster and pairwise synthesis outputs.

### How to interpret
- High-priority clusters/pairs: stronger combined evidence, earlier review target.
- Contradictory pairs: important review queue; often signal data mismatch or complex epidemiology.
- Validation notice: synthesis is heuristic and requires calibrated pipelines for real-world claims.

### Parameter interpretation
- SNP threshold: lower values are stricter for likely linkage.
- Temporal window: wider windows include more plausible links but can add noise.
- Posterior minimum: raises/lowers inclusion strictness for model links.

---

## Step 6 - Cluster Investigation Centre

### What it does
Operational workspace for assigning reviewers, recording epi evidence, actions, sign-off, and analytics review.

### How to interpret
- Members tab: confirms who is in the cluster and core metadata.
- Epi notes/evidence tabs: captures real-world context that genomics alone cannot provide.
- Actions tab: documents what public-health interventions were taken.
- Sign-off tab: records formal investigation outcomes.
- Analytics tab: explains cluster prioritization, pair evidence, and reviewer classifications.

### Analytics tab interpretation
- Cluster prioritization reasons: plain-language rationale for why this cluster appears important.
- Pair evidence table:
  - Genomic support, epi support, and temporal plausibility should be read together.
  - "Contradictory" or "insufficient evidence" rows require reviewer judgment, not automatic dismissal.
- Saved pair reviews:
  - Shows reviewer decisions over time.
  - Use filter/search to find specific pair decisions quickly.

### Practical interpretation tips
- Reviewer classifications are governance artifacts; they do not retroactively validate model outputs.
- When genomic and epi evidence disagree, document rationale in notes before sign-off.

---

## Step 7 - Phylogenetic and visual analytics

### What it does
Provides SNP matrix, phylogenetic view, timeline, map, growth curves, genomic-vs-epi comparison, calibration dashboard, and dossier export.

### How to interpret each view

#### SNP matrix
- Lower pairwise distances suggest closer genomic similarity.
- Distance alone does not prove direct transmission.

#### Phylogenetic tree/graph
- Supports understanding relatedness and likely link structure.
- Use with metadata and exposure evidence; avoid tree-only conclusions.

#### Timeline
- Identifies clustering in time and potential outbreak windows.
- Large time gaps can weaken direct-link assumptions.

#### Geography map
- Highlights concentration and spread across regions.
- Multi-region clusters may indicate wider transmission chains or movement.

#### Cluster growth curves
- Rapid growth can indicate active transmission or detection bursts.
- Always check for ascertainment or reporting effects.

#### Genomic vs epi links
- "Both supported" is strongest operationally.
- "Genomic only" and "Epi only" require targeted review.
- "Neither" usually low priority unless other evidence exists.

#### Calibration dashboard
- Compares model labels with reviewer labels.
- Higher exact/binary agreement means better alignment with reviewer practice.
- Low agreement is a quality signal to review rules/thresholds and recent evidence capture.

---

## Step 8 - Final actionable report

### What it does
Produces reviewer-facing consolidated report with safety/readiness/KPIs/priorities/provenance context.

### How to interpret
- Use as decision brief, not as sole evidence source.
- Ensure caveats from Steps 3, 5, 6, and 7 are reflected in conclusions.

---

## Step 9 - Runtime and supporting status

### What it does
Shows detailed KPI payloads, outbreaker readiness, and current/recent job logs.

### How to interpret
- Confirms whether reports are backed by complete and recent execution state.
- Logs are key for explaining missing outputs or unexpected shifts.

---

## Step 10 - Resistance validation sign-off

### What it does
Captures reviewer sign-off and notes around resistance validation status.

### How to interpret
- Sign-off indicates governance review completion for current evidence state.
- It does not convert genomic prediction into clinical confirmation.

---

## Step 11 - Epidemiology reference records

### What it does
Maintains reusable exposure/contact/location records as a shared library.

### How to interpret
- This step stores reusable reference entities.
- It does not by itself attach evidence to a case.

### Important distinction from Step 6
- Step 6 records case-linked investigation evidence.
- Step 11 manages reusable dictionaries/reference records.

---

## Step 12 - Governance and compliance

### What it does
Shows audit trail and governance-related controls/records.

### How to interpret
- Use for traceability: who changed what, and when.
- Required for defensible sign-off and review accountability.

---

## Result interpretation quick guide

### Confidence pattern guide
- Strongest operational confidence: genomic + epi + temporal alignment with reviewer agreement.
- Moderate confidence: two evidence domains align, third is missing/weak.
- Low confidence: contradictory domains or missing critical evidence.

### Typical escalation triggers
- Sudden rise in contradictory pairs.
- High-priority cluster with sparse or conflicting epi evidence.
- Drop in calibration agreement.
- High lineage/DR discordance.
- Frequent workflow gate failures.

### Suggested reviewer wording (plain language)
- "Evidence is supportive but not conclusive; further field investigation recommended."
- "Signals are contradictory; classification remains uncertain pending additional evidence."
- "Current model and epidemiology are aligned; prioritize intervention review."

---

## Recommended routine workflow

1. Check Step 1 and Data readiness.
2. Run Step 3 pipeline.
3. Verify baseline outputs in Step 4 and Step 4A.
4. Prioritize in Step 5.
5. Investigate and document in Step 6.
6. Validate visually and comparatively in Step 7.
7. Produce and review Step 8 report.
8. Confirm runtime/supporting status in Step 9.
9. Record formal resistance sign-off in Step 10.
10. Maintain reference entities in Step 11 as needed.
11. Confirm governance traceability in Step 12.

---

## Final reminder

Use this platform as a structured evidence and prioritization tool. Final public-health decisions should combine platform outputs with expert epidemiology, laboratory context, and governance review.
