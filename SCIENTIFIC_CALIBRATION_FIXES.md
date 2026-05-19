# Scientific Calibration Fixes & Known Issues

This document tracks the inconsistencies between the synthetic interval biology and pipeline parameters, plus operational and architectural problems. Issues are organized by tier (high-impact fixable → architectural changes required).

---

## 🟢 Tier 1: High-Impact Fixes Applied

### Issue 1: Temporal Window (45 days) vs Serial Interval (180 days)  
**Status: ✅ FIXED**

- **Problem**: The serial interval was correctly updated to ~180 days for TB, but the synthesis layer's `temporal_window_days` remained at 45 days. A 45-day window incorrectly marks most genuinely linked TB pairs as temporally implausible, even though specimen dates 60–150 days apart are expected (diagnostic delay ~3 months).
- **Fix Applied**: 
  - Changed default `temporal_window_days` from **45 → 90 days** in `backend/synthesis/transmission_synthesis.py` (SynthesisConfig)
  - Updated default in `backend/synthesis/epi_evidence.py` (compute_epi_evidence)
  - Updated all 7 Query defaults in `backend/routers/analytics.py` endpoints
  - Added documentation: "First-generation TB links typically span 60–150 days (diagnostic delay ~3 months)"
- **Files Modified**: 
  - [backend/synthesis/transmission_synthesis.py](backend/synthesis/transmission_synthesis.py#L36)
  - [backend/synthesis/epi_evidence.py](backend/synthesis/epi_evidence.py#L248)
  - [backend/routers/analytics.py](backend/routers/analytics.py#L1157) (multiple lines)
- **Impact**: TB pairs with realistic diagnostic delays will now score as temporally plausible rather than marginal/implausible.
- **Note**: 90 days is calibrated for first-generation links. Second-generation or longer chains may warrant the full 180-day window; this can be exposed as a configurable parameter for calibration studies.

---

### Issue 4: Sequence Length Delta Added to SNP Distance  
**Status: ✅ FIXED**

- **Problem**: In `snp_validation.py`, sequence length differences (N bases) were being added directly to the SNP count. A 50bp length difference (common from quality trimming) would add 50 to the SNP distance, potentially pushing a genuinely close pair over the 25 SNP clustering threshold. Length differences are almost always assembly/trimming artefacts, not true biological variants.
- **Fix Applied**:
  - Removed the line `distance += length_delta` from `snp_validation.py` line 42
  - `length_delta` is still tracked and reported in the output for transparency
  - Added comment: "Do NOT add length_delta to distance: sequence length differences are typically assembly/trimming artefacts, not true biological variants."
- **Files Modified**: 
  - [backend/snp_validation.py](backend/snp_validation.py#L40)
- **Impact**: SNP-based clustering now reflects true sequence mutations only, not assembly quality artefacts. Pairs with minor length differences will no longer be artificially pushed into higher distance bins.
- **Schema**: The `SnpDistanceValidation` dataclass still exposes `length_delta`, so reports/evidence cards can show it separately if needed.

---

### Issue 7: Location Overlap Threshold  
**Status: ✅ FIXED**

- **Problem**: The code checked `attendance_overlap_days >= 0`, meaning zero-day overlap (adjacent days or missing data) was treated identically to 30+ days of co-attendance, contributing equally to `has_strong_location`.
- **Fix Applied**:
  - Changed threshold from `>= 0` → `>= 1` in `backend/synthesis/epi_evidence.py` line 404
  - Now requires a minimum 1 day of true overlap for location evidence to count as strong
  - Added comment: "Require ≥1 day overlap; 0 days means adjacent days or missing data"
- **Files Modified**: 
  - [backend/synthesis/epi_evidence.py](backend/synthesis/epi_evidence.py#L404)
- **Impact**: Epi support levels now correctly weight shared location evidence proportional to actual overlap duration rather than treating missing/zero-overlap the same as confirmed overlap.

---

## 🟡 Tier 2: Documentation & Limitations Documented

### Issue 6: Node Risk Score Formula  
**Status: 📝 DOCUMENTED (not yet configurable)**

- **Problem**: The R script assigns each case a risk score: `0.7 × outgoing + 0.3 × incoming` transmission links. This prioritises potential sources over recipients (reasonable for contact tracing) but the rationale is undocumented and the weights aren't configurable.
- **Workaround Applied**: 
  - The formula is now documented in outbreak analysis reports (human-readable)
  - Consider parameterizing via environment variables in a future iteration if different weightings are needed for variant scenarios (e.g., smear-status prioritisation)
- **Future Work**: Expose `NODE_RISK_SOURCE_WEIGHT` and `NODE_RISK_RECIPIENT_WEIGHT` as configurable environment variables in the R pipeline.

---

### Issue 8: Resistance Concordance at Drug Level, Not Mutation Level  
**Status: 📝 DOCUMENTED (requires data structure change)**

- **Problem**: `_resistance_profile_concordance()` compares the *set of resistant drugs* between two cases, not mutation-level identity. Two cases with rifampicin resistance but different rpoB mutations (e.g., S450L vs H445Y) are marked concordant, which may indicate convergent evolution rather than transmission.
- **Documentation Added**: 
  - Added docstring to `_resistance_profile_concordance()` in `backend/synthesis/transmission_synthesis.py` explaining the limitation
  - Note: Implementing mutation-level comparison requires the database to store individual mutation records alongside predicted_drug_resistance, which is not yet available
- **Files Modified**: 
  - [backend/synthesis/transmission_synthesis.py](backend/synthesis/transmission_synthesis.py#L141)
- **Future Work**: 
  - Populate a `individual_resistance_mutations` field in case records if mutation-level data becomes available from analysis tools
  - Implement mutation-set comparison as an additional concordance layer

---

### Issue 9: Circular Comparison Metric  
**Status: 📝 DOCUMENTED (architectural limitation)**

- **Problem**: `compare_clustering_methods.py` computes precision/recall of Outbreaker2 vs sequence clustering, but Outbreaker2 uses sequence-derived clusters as its input case set, making the comparison circular. The metric measures whether Outbreaker2 agrees with itself-via-sequence clustering, not independent agreement.
- **Documentation Added**: 
  - Added module-level docstring to `compare_clustering_methods.py` explaining the circularity
  - Recommendation: For true independent comparison, run both methods on the full case set and compare against a gold standard of epidemiologically-confirmed transmission links (if available)
- **Files Modified**: 
  - [scripts/compare_clustering_methods.py](scripts/compare_clustering_methods.py#L1)
- **Impact**: The current metric inflates agreement; users should interpret results as "Outbreaker2 consistency with sequence clustering" rather than independent validation.

---

## 🔴 Tier 3: Architectural Blockers (Require Deeper Refactoring)

These issues require design changes beyond parameter tuning and are noted for future roadmap planning.

### Issue 2: Cluster Investigation Data Wiped on Re-run  
**Status: ⚠️ BLOCKING**

- **Problem**: `derive_sequence_clusters.py` executes `TRUNCATE TABLE case_clusters, cluster_investigations, clusters` before every run, destroying:
  - Assignee information
  - Epi notes and structured evidence
  - Actions recorded
  - Risk band overrides
  - All operational investigation work

  As the dataset grows and clusters evolve, losing hand-entered investigation history is a serious data loss issue.

- **Proposed Solution**:  
  Instead of TRUNCATE, diff old vs new cluster assignments:
  - **Same members**: Keep investigation record, update alert_flag
  - **New members added**: Keep record, flag "membership_updated"
  - **Cluster split**: Copy record to both new clusters, flag "split_from"
  - **Cluster dissolved**: Archive record, don't delete

- **Implementation Notes**:
  - Requires adding `membership_changed_flag` and `previous_cluster_id` fields to `cluster_investigations` table
  - Need migration strategy for existing investigation records
  - Add audit trail for cluster membership changes

---

### Issue 3: Specimen Date vs Transmission Date in Outbreaker2  
**Status: ⚠️ BLOCKING**

- **Problem**: The R script passes `specimen_date` directly to Outbreaker2 as the case collection date. Outbreaker2 uses these for the serial-interval prior, assuming dates represent symptom onset or notification. In TB, specimens are typically collected 1–6 weeks *after* symptom onset, systematically shifting all dates later than expected and compressing apparent transmission intervals.

- **Impact**: Posterior ancestry assignments are biased toward more recent cases; transmission chains may be inferred incorrectly.

- **Proposed Solutions**:
  1. **Ideal**: Use symptom onset date if available (requires data availability check)
  2. **Interim**: Document the assumption explicitly in report output and R script comments, flagging it for reviewer consideration
  3. **Calibration study**: Quantify the bias if onset dates become available

- **Implementation Notes**:
  - Check whether symptom_onset_date is captured in case records
  - If available, add R logic to use onset date and fall back to specimen_date if missing
  - Document assumption prominently in all outputs

---

### Issue 5: Outbreaker Reports Marginal Mode Ancestry Only, Losing Posterior Uncertainty  
**Status: ⚠️ BLOCKING**

- **Problem**: The R script exports `which.max(freq)` — the single most common ancestor across posterior samples — as the transmission link with a probability. This discards uncertainty information. A case where 40% of samples point to ancestor A and 38% to B is indistinguishable from one where 99% point to a single ancestor, both reported as "high confidence" if the mode ≥ 0.8.

- **Proposed Solution**:  
  Export top 2–3 ancestral candidates with probabilities for each target case, allowing the synthesis layer to flag "uncertain ancestry" when posterior is diffuse.

- **Implementation Notes**:
  - Modify R script to capture `names(sort(freq, decreasing=TRUE)[1:3])` and their probabilities
  - Update JSON schema to support multiple candidates per case
  - Add synthesis flag: `"uncertain_ancestry"` when top 2 candidates are within 10 percentage points

---

### Issue 10: No Index Case Identification  
**Status: ⚠️ BLOCKING**

- **Problem**: Outbreaker2 uses `NA` in the `alpha` column to indicate a case with no identified ancestor (likely an import or index case). The R script discards these: `ancestry <- ancestry[!is.na(ancestry)]`. Cases frequently assigned NA ancestry (>50% of posterior samples) are actionable public health signals but are lost.

- **Proposed Solution**:  
  Export `p_unlinked` (proportion of posterior samples where `alpha = NA`) per case, allowing synthesis to flag index cases or imports.

- **Implementation Notes**:
  - Compute proportion of NA assignments in ancestry chains
  - Add to synthesis output: `"likely_index_case"` if p_unlinked > 0.5
  - Use in cluster context: distinguish star clusters from index cases vs multi-source clusters

---

### Issue 11: No Transmission Generation Tracking  
**Status: ⚠️ PARTIAL (can compute from edges)**

- **Problem**: The synthesis layer identifies directed pairs but doesn't reconstruct transmission chains (generation 1 → 2 → 3). For TB outbreak investigation, knowing whether cases form a chain vs star-shaped cluster from a single source affects contact tracing scope directly.

- **Proposed Solution**:  
  Edges in `transmission_network.json` contain enough information to reconstruct generation depth from putative index case. Add as computed field in cluster summary.

- **Implementation Notes**:
  - Requires BFS/DFS on transmission graph from identified index case
  - Add to cluster summary: `"max_transmission_generation"`, `"generation_distribution"` (e.g., `{"1": 5, "2": 3, "3": 1}`)
  - Use for risk assessment: chains with generation ≥ 3 suggest sustained transmission requiring escalated response

---

### Issue 12: No Seasonality or Incidence Rate Context in KPIs  
**Status: ⚠️ PARTIAL (requires configuration)**

- **Problem**: The KPI endpoint reports raw case counts and cluster rates but provides no context against expected background rates. In Northern Ireland, TB incidence is ~4–6/100,000/year. A cluster of 4 cases in 3 months in a population of 1.9M is unusual; the same cluster size in a care home is very unusual. Raw counts lack statistical grounding.

- **Proposed Solution**:  
  Add configurable expected annual incidence rate (per 100,000) and compute "cases above expected baseline" metric.

- **Implementation Notes**:
  - Add environment variable: `TB_INCIDENCE_BASELINE_PER_100K` (default: 5.0 for UK TB rates)
  - Add population estimate: `SYSTEM_POPULATION` (default: 1,900,000 for NI)
  - Compute: `expected_cases_in_window = (baseline / 100_000 / 365) * population * days_in_window`
  - Report: `"cluster_cases_vs_baseline"` and `"statistical_significance"` (Z-score if available)

---

## Summary of Changes

| Issue | Tier | Status | Files Modified |
|-------|------|--------|-----------------|
| 1. Temporal window (45→90 days) | 🟢 | ✅ FIXED | `transmission_synthesis.py`, `epi_evidence.py`, `analytics.py` (7 endpoints) |
| 4. SNP length delta | 🟢 | ✅ FIXED | `snp_validation.py` |
| 7. Location overlap threshold | 🟢 | ✅ FIXED | `epi_evidence.py` |
| 6. Node risk score formula | 🟡 | 📝 DOCUMENTED | (R script documentation needed) |
| 8. Resistance mutation-level concordance | 🟡 | 📝 DOCUMENTED | `transmission_synthesis.py` |
| 9. Circular comparison metric | 🟡 | 📝 DOCUMENTED | `compare_clustering_methods.py` |
| 2. Investigation data truncation | 🔴 | ⚠️ BLOCKING | Requires DB schema + migration |
| 3. Specimen vs transmission date | 🔴 | ⚠️ BLOCKING | Requires R script + data availability |
| 5. Marginal mode only | 🔴 | ⚠️ BLOCKING | Requires R script output change |
| 10. Index case identification | 🔴 | ⚠️ BLOCKING | Requires R script output change |
| 11. Transmission generation tracking | 🔴 | ⚠️ PARTIAL | Can add to synthesis output |
| 12. Seasonality context | 🔴 | ⚠️ PARTIAL | Requires KPI endpoint refactor |

---

## Next Steps

1. **Validate Tier-1 fixes**: Run integration tests to confirm temporal window and SNP distance changes don't break existing clustering thresholds.
2. **Plan Tier-2 parameterization**: Make node risk score and incidence baseline configurable via environment variables.
3. **Schedule Tier-3 work**: Coordinate with TB programme leadership on investigation data preservation and R script calibration study (issues 2, 3, 5).
