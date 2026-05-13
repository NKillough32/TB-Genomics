Governance summary:
- Pseudonymised identifiers only (UUID v5 stable IDs from lab sample IDs)
- No automated decision making
- Analyst-led outbreaker2 execution
- Actions logged and auditable (audit_log table; discordant DR calls flagged automatically)
- Data safety gate: `GET /cases/data-safety` confirms operational_safe before sensitive actions
- Synthetic/demo data blocked from public-health reporting by default
- Ingest authentication supported via TB_INGEST_API_KEY (X-API-Key header)
- NI live data transition requires full DB reset before loading (no synthetic seed events in audit_log)
