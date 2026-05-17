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
- Synthesis and investigation sign-off routes use token-based RBAC when `TB_AUTH_REQUIRED`
  or `TB_AUTH_TOKENS` is configured. This is not a full user-login or NHS identity
  governance layer.
- The synthesis layer combines genomic, timing, geography, resistance, and model outputs into a plain-language operational summary
- Cluster-risk scores need calibration before real-world use

Plain-language use note:
- Use the synthesis layer to help reviewers decide what to look at first.
- Do not treat the scores as confirmed transmission truth.
- If important epidemiology fields are missing, expect the synthesis layer to rely on weaker proxy signals.
