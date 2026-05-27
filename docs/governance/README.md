Governance summary:
- Pseudonymised identifiers only (UUID v5 stable IDs from lab sample IDs)
- No automated decision making
- Analyst-led outbreaker2 execution
- Actions logged and auditable (audit_log table; discordant DR calls flagged automatically)
- Data safety gate: `GET /cases/data-safety` confirms operational_safe before sensitive actions
- Synthetic/demo data blocked from public-health reporting by default
- Ingest authentication supported via TB_INGEST_API_KEY (X-API-Key header)
- NI live data transition requires full DB reset before loading (no synthetic seed events in audit_log)
- New synthesis layer outputs are heuristic and non-validated; they are for review support, not final public-health decisions
- API routes use token-based RBAC when `TB_AUTH_REQUIRED` or `TB_AUTH_TOKENS` is configured.
  Roles are `viewer`, `analyst`, `operator`, and `admin`. This is not a full user-login or NHS
  identity governance layer.
- The synthesis layer combines genomic, timing, geography, resistance, and model outputs into a plain-language operational summary
- The GUI workflow is grouped into Prepare, Analyse, Investigate, and Report/Govern phases so reviewers can separate data preparation from analysis, case investigation, and governance tasks.
- Case-specific epidemiology evidence is recorded in Step 6, the Cluster Investigation Centre. Step 11 creates reusable reference records only and must not be treated as a case-assignment workflow.
- Case records, epidemiology records, and case-pair reviews are corrected through audited update, entered-in-error, and restore workflows rather than untracked deletion.
- Pair-review calibration and threshold sweep outputs are governance aids only; they require documented local review before thresholds are changed.
- Cluster-risk scores need calibration before real-world use

Plain-language use note:
- Use the synthesis layer to help reviewers decide what to look at first.
- Do not treat the scores as confirmed transmission truth.
- If important epidemiology fields are missing, expect the synthesis layer to rely on weaker proxy signals.
- To assign evidence to a case, open a cluster in the Cluster Investigation Centre, choose the Epi notes tab, select a case, and record a location event or contact link.
- To correct a mistake, mark the affected record entered in error with a reason, then restore it only when the correction is justified.

Pilot milestone:
- See `docs/governance/PILOT_READY_INTERNAL_DEMONSTRATOR.md` for the short-term internal demonstrator target, operating rules, and remaining production gaps.

