import base64
import csv
import hashlib
import html as html_lib
import json
import logging
import os
import re
from datetime import datetime
from itertools import combinations

from sqlalchemy import text
from sqlalchemy.orm import Session
from backend.routers.dependencies import get_db
from backend.runtime_paths import export_path

logger = logging.getLogger(__name__)


def _export_path(*parts: str) -> str:
    """Return an absolute path under the repository export directory."""
    return export_path(*parts)


# -- Pipeline validation sign-off helpers --------------------------------------

def _ensure_signoff_table(db: Session) -> None:
    """Create the signoffs table if it doesn't exist yet (idempotent)."""
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS pipeline_validation_signoffs (
            id                SERIAL PRIMARY KEY,
            pipeline          TEXT NOT NULL DEFAULT 'resistance_validation',
            decision          TEXT NOT NULL,
            reviewer          TEXT NOT NULL,
            notes             TEXT,
            catalogue_version TEXT,
            signed_off_at     TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()


def _get_latest_signoff(db: Session) -> dict | None:
    """Return the most recent signoff row for the resistance pipeline, or None."""
    try:
        _ensure_signoff_table(db)
        row = db.execute(text("""
            SELECT id, pipeline, decision, reviewer, notes, catalogue_version, signed_off_at
            FROM pipeline_validation_signoffs
            WHERE pipeline = 'resistance_validation'
            ORDER BY signed_off_at DESC
            LIMIT 1
        """)).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def _lineage_analysis_summary(db: Session) -> dict:
    """Return live lineage/DR interpretation counts from tb_interpretation."""
    summary = {
        "interpreted_samples": 0,
        "samples_with_lineage": 0,
        "samples_with_resistance_calls": 0,
        "samples_with_interpretation_summary": 0,
    }

    try:
        summary_row = db.execute(
            text(
                """
                SELECT
                    COUNT(*)::int AS interpreted_samples,
                    COUNT(*) FILTER (WHERE lineage IS NOT NULL AND BTRIM(lineage) <> '')::int AS samples_with_lineage,
                    COUNT(*) FILTER (
                        WHERE predicted_drug_resistance IS NOT NULL
                        AND predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
                    )::int AS samples_with_resistance_calls,
                    COUNT(*) FILTER (
                        WHERE interpretation_summary IS NOT NULL AND BTRIM(interpretation_summary) <> ''
                    )::int AS samples_with_interpretation_summary
                FROM tb_interpretation
                """
            )
        ).mappings().first()
        if summary_row:
            summary = {
                "interpreted_samples": int(summary_row["interpreted_samples"] or 0),
                "samples_with_lineage": int(summary_row["samples_with_lineage"] or 0),
                "samples_with_resistance_calls": int(summary_row["samples_with_resistance_calls"] or 0),
                "samples_with_interpretation_summary": int(summary_row["samples_with_interpretation_summary"] or 0),
            }
    except Exception as exc:
        summary["error"] = str(exc)

    return summary


def _lineage_epi_summary(db: Session) -> dict:
    """Return epidemiology-oriented lineage/DR indicators for decision support."""
    summary = {
        "samples_with_any_resistance_signal": 0,
        "rifampicin_resistant_suspected": 0,
        "isoniazid_resistant_suspected": 0,
        "mdr_suspected": 0,
        "fluoroquinolone_resistant_suspected": 0,
        "top_lineages": [],
        "top_lineage_region_pairs": [],
    }

    try:
        base_row = db.execute(
            text(
                """
                WITH dr AS (
                    SELECT LOWER(CAST(predicted_drug_resistance AS TEXT)) AS dr_text
                    FROM tb_interpretation
                    WHERE predicted_drug_resistance IS NOT NULL
                    AND predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
                )
                SELECT
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%resistant%'
                        OR dr_text LIKE '%\"r\"%'
                    )::int AS samples_with_any_resistance_signal,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%rifamp%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS rifampicin_resistant_suspected,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%isoniazid%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS isoniazid_resistant_suspected,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%fluoro%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS fluoroquinolone_resistant_suspected,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%rifamp%'
                        AND dr_text LIKE '%isoniazid%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS mdr_suspected
                FROM dr
                """
            )
        ).mappings().first()

        if base_row:
            summary.update(
                {
                    "samples_with_any_resistance_signal": int(base_row["samples_with_any_resistance_signal"] or 0),
                    "rifampicin_resistant_suspected": int(base_row["rifampicin_resistant_suspected"] or 0),
                    "isoniazid_resistant_suspected": int(base_row["isoniazid_resistant_suspected"] or 0),
                    "mdr_suspected": int(base_row["mdr_suspected"] or 0),
                    "fluoroquinolone_resistant_suspected": int(base_row["fluoroquinolone_resistant_suspected"] or 0),
                }
            )

        lineage_rows = db.execute(
            text(
                """
                SELECT lineage, COUNT(*)::int AS n
                FROM tb_interpretation
                WHERE lineage IS NOT NULL AND BTRIM(lineage) <> ''
                GROUP BY lineage
                ORDER BY n DESC, lineage
                LIMIT 5
                """
            )
        ).mappings().all()
        summary["top_lineages"] = [
            {"lineage": str(r["lineage"]), "count": int(r["n"] or 0)}
            for r in lineage_rows
        ]

        pair_rows = db.execute(
            text(
                """
                SELECT c.geographic_region AS region, ti.lineage, COUNT(*)::int AS n
                FROM tb_interpretation ti
                JOIN cases c ON c.pseudonymised_case_id = ti.sample_id
                WHERE ti.lineage IS NOT NULL AND BTRIM(ti.lineage) <> ''
                GROUP BY c.geographic_region, ti.lineage
                ORDER BY n DESC, c.geographic_region, ti.lineage
                LIMIT 8
                """
            )
        ).mappings().all()
        summary["top_lineage_region_pairs"] = [
            {
                "region": str(r["region"]),
                "lineage": str(r["lineage"]),
                "count": int(r["n"] or 0),
            }
            for r in pair_rows
        ]
    except Exception as exc:
        summary["error"] = str(exc)

    return summary


def _derive_effective_engine_status(payload: dict) -> dict:
    """Compute user-facing engine statuses from local + fallback execution context."""
    payload = payload or {}
    engines = payload.get("engines") or {}
    tb_local = (engines.get("tb_profiler") or {}).get("status", "unknown")
    mykrobe_local = (engines.get("mykrobe") or {}).get("status", "unknown")

    tb_run = payload.get("tbprofiler_run") or {}
    tb_wsl_run = payload.get("tbprofiler_wsl_run") or {}
    tb_docker_run = payload.get("tbprofiler_docker_run") or {}

    mykrobe_run = payload.get("mykrobe_run") or {}
    mykrobe_wsl_run = payload.get("mykrobe_wsl_run") or {}

    wsl_info = payload.get("wsl") or {}
    wsl_tb = (wsl_info.get("tbprofiler") or {}).get("status", "unknown")
    wsl_mykrobe = (wsl_info.get("mykrobe") or {}).get("status", "unknown")

    tb_effective = tb_local
    if tb_run.get("status") == "completed":
        tb_effective = f"available_via_{tb_run.get('runner', 'runner')}"
    elif tb_wsl_run.get("status") == "completed" or wsl_tb == "installed":
        tb_effective = "available_via_wsl"
    elif tb_docker_run.get("status") == "completed":
        tb_effective = "available_via_docker"

    mykrobe_effective = mykrobe_local
    if mykrobe_run.get("status") == "completed":
        mykrobe_effective = f"available_via_{mykrobe_run.get('runner', 'runner')}"
    elif mykrobe_wsl_run.get("status") == "completed" or wsl_mykrobe == "installed":
        mykrobe_effective = "available_via_wsl"

    return {
        "tb_profiler": tb_effective,
        "mykrobe": mykrobe_effective,
        "tb_profiler_local": tb_local,
        "mykrobe_local": mykrobe_local,
    }


def _secondary_epi_summary(
    secondary_validation_data: dict | None,
    transmission_data: dict | None,
    method_comparison_data: dict | None,
) -> dict:
    """Summarize usable epidemiology from secondary-validation context."""
    secondary_validation_data = secondary_validation_data or {}
    transmission_data = transmission_data or {}
    method_comparison_data = method_comparison_data or {}

    prerequisites = secondary_validation_data.get("prerequisites") or {}
    consensus = secondary_validation_data.get("consensus") or {}
    agreement = method_comparison_data.get("agreement") or {}

    high_confidence_edges = int(transmission_data.get("high_confidence_edges") or 0)
    key_nodes = transmission_data.get("key_nodes") or []

    return {
        "consensus_state": str(consensus.get("status") or "unknown"),
        "tree_input_available": bool(prerequisites.get("has_tree_newick")),
        "cases_input_available": bool(prerequisites.get("has_cases_csv")),
        "primary_network_available": bool(transmission_data),
        "high_confidence_links": high_confidence_edges,
        "priority_nodes_flagged": len(key_nodes),
        "pairwise_precision": round(float(agreement.get("pairwise_precision_outbreaker_vs_sequence") or 0.0), 3),
        "pairwise_recall": round(float(agreement.get("pairwise_recall_outbreaker_vs_sequence") or 0.0), 3),
        "pairwise_jaccard": round(float(agreement.get("pairwise_jaccard") or 0.0), 3),
    }


def _short_case_id(case_id: str | None) -> str:
    if not case_id:
        return "n/a"
    return str(case_id)[:8]


def _resistance_profile_text(predicted_dr: object) -> str:
    """Render concise drug-resistance profile from JSON-like field."""
    if not predicted_dr:
        return "none"

    if isinstance(predicted_dr, str):
        return predicted_dr[:64]

    if isinstance(predicted_dr, dict):
        resistant = []
        for drug, phenotype in predicted_dr.items():
            phen = str(phenotype).lower()
            if "resistant" in phen or phen == "r":
                resistant.append(str(drug))
        return ", ".join(resistant[:4]) if resistant else "susceptible"

    return str(predicted_dr)[:64]


def _iter_resistance_mutations(mutations: object):
    """Yield normalized mutation rows from flexible JSON structures."""
    if not mutations:
        return

    if isinstance(mutations, dict):
        for drug, value in mutations.items():
            if isinstance(value, list):
                for idx, item in enumerate(value):
                    if isinstance(item, dict):
                        has_drug_from_tool = bool(item.get("drug"))
                        yield {
                            "drug": str(item.get("drug") or drug),
                            "mutation": str(item.get("mutation") or item.get("variant") or item.get("change") or item),
                            "gene": str(item.get("gene") or "n/a"),
                            "confidence": str(item.get("confidence") or item.get("support") or "n/a"),
                            "source_json_path": str(item.get("source_json_path") or f"{drug}[{idx}]"),
                            "drug_from_tool": has_drug_from_tool,
                            "drug_inferred_from_sample_level": not has_drug_from_tool,
                        }
                    else:
                        yield {
                            "drug": str(drug),
                            "mutation": str(item),
                            "gene": "n/a",
                            "confidence": "n/a",
                            "source_json_path": f"{drug}[{idx}]",
                            "drug_from_tool": False,
                            "drug_inferred_from_sample_level": True,
                        }
            elif isinstance(value, dict):
                has_drug_from_tool = bool(value.get("drug"))
                yield {
                    "drug": str(value.get("drug") or drug),
                    "mutation": str(value.get("mutation") or value.get("variant") or value.get("change") or value),
                    "gene": str(value.get("gene") or "n/a"),
                    "confidence": str(value.get("confidence") or value.get("support") or "n/a"),
                    "source_json_path": str(value.get("source_json_path") or str(drug)),
                    "drug_from_tool": has_drug_from_tool,
                    "drug_inferred_from_sample_level": not has_drug_from_tool,
                }
            else:
                yield {
                    "drug": str(drug),
                    "mutation": str(value),
                    "gene": "n/a",
                    "confidence": "n/a",
                    "source_json_path": str(drug),
                    "drug_from_tool": False,
                    "drug_inferred_from_sample_level": True,
                }
        return

    if isinstance(mutations, list):
        for idx, item in enumerate(mutations):
            if isinstance(item, dict):
                has_drug_from_tool = bool(item.get("drug"))
                yield {
                    "drug": str(item.get("drug") or "n/a"),
                    "mutation": str(item.get("mutation") or item.get("variant") or item.get("change") or item),
                    "gene": str(item.get("gene") or "n/a"),
                    "confidence": str(item.get("confidence") or item.get("support") or "n/a"),
                    "source_json_path": str(item.get("source_json_path") or f"[{idx}]"),
                    "drug_from_tool": has_drug_from_tool,
                    "drug_inferred_from_sample_level": not has_drug_from_tool,
                }
            else:
                yield {
                    "drug": "n/a",
                    "mutation": str(item),
                    "gene": "n/a",
                    "confidence": "n/a",
                    "source_json_path": f"[{idx}]",
                    "drug_from_tool": False,
                    "drug_inferred_from_sample_level": True,
                }


def _snp_distance(seq_a: str, seq_b: str) -> int | None:
    if not seq_a or not seq_b:
        return None

    a = str(seq_a).upper()
    b = str(seq_b).upper()
    common = min(len(a), len(b))
    mismatches = sum(1 for i in range(common) if a[i] != b[i])
    return mismatches + abs(len(a) - len(b))


def _normalise_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _expected_genes_for_drug(drug: object) -> list[str]:
    """Return the local expected-gene screen used to catch obvious drug mapping errors.

    Ordered from most-specific to least-specific substring so that e.g.
    "rifabutin" is matched before the broader "rifamp" prefix.
    This is a conservative report safety screen, not a substitute for a curated
    WHO catalogue lookup or formal pipeline validation.
    """
    drug_text = str(drug or "").lower()
    # (substring_marker, [expected_genes])  - first match wins
    mapping = [
        # -- Rifamycins ------------------------------------------------------
        ("rifabutin",        ["rpoB"]),
        ("rifapentine",      ["rpoB"]),
        ("rifamp",           ["rpoB"]),                          # rifampicin / rifampin
        # -- Isoniazid -------------------------------------------------------
        ("isoniazid",        ["katG", "inhA", "fabG1", "ahpC", "kasA"]),
        # -- Ethionamide / Prothionamide (share inhA/fabG1 with isoniazid) --
        ("prothionamide",    ["ethA", "ethR", "inhA", "fabG1", "mshA"]),
        ("ethionamide",      ["ethA", "ethR", "inhA", "fabG1", "mshA"]),
        # -- Pyrazinamide ----------------------------------------------------
        ("pyrazinamide",     ["pncA", "rpsA", "panD"]),
        # -- Ethambutol ------------------------------------------------------
        ("ethambutol",       ["embB", "embA", "embC", "embR", "iniB"]),
        # -- Fluoroquinolones (individual agents before generic class) -------
        ("moxifloxacin",     ["gyrA", "gyrB"]),
        ("levofloxacin",     ["gyrA", "gyrB"]),
        ("ciprofloxacin",    ["gyrA", "gyrB"]),
        ("ofloxacin",        ["gyrA", "gyrB"]),
        ("gatifloxacin",     ["gyrA", "gyrB"]),
        ("fluoroquinolone",  ["gyrA", "gyrB"]),                  # generic class
        ("fluoroquin",       ["gyrA", "gyrB"]),                  # abbreviation
        # -- Aminoglycosides / injectable second-line agents -----------------
        ("amikacin",         ["rrs", "eis"]),
        ("kanamycin",        ["rrs", "eis"]),
        ("capreomycin",      ["rrs", "tlyA"]),
        ("streptomycin",     ["rpsL", "rrs", "gid"]),
        # -- Bedaquiline -----------------------------------------------------
        ("bedaquiline",      ["atpE", "Rv0678", "pepQ", "mmpL5", "mmpS5"]),
        # -- Linezolid -------------------------------------------------------
        ("linezolid",        ["rrl", "rplC"]),
        # -- Clofazimine -----------------------------------------------------
        ("clofazimine",      ["Rv0678", "pepQ", "mmpL5", "mmpS5"]),
        # -- Delamanid -------------------------------------------------------
        ("delamanid",        ["ddn", "fgd1", "fbiA", "fbiB", "fbiC"]),
        # -- Pretomanid ------------------------------------------------------
        ("pretomanid",       ["ddn", "fgd1", "fbiA", "fbiB", "fbiC", "Rv3547"]),
        # -- Para-aminosalicylic acid (PAS) -----------------------------------
        ("aminosalicylic",   ["thyA", "folC", "thyX"]),
        ("para-amino",       ["thyA", "folC", "thyX"]),
        # -- Cycloserine / Terizidone -----------------------------------------
        ("terizidone",       ["ald", "alr"]),
        ("cycloserine",      ["ald", "alr"]),
        # -- Carbapenems (used in BPaL regimens) -----------------------------
        ("imipenem",         ["blaC"]),
        ("meropenem",        ["blaC"]),
        # -- Clavam ----------------------------------------------------------
        ("clavulanate",      ["blaC"]),
    ]
    for marker, genes in mapping:
        if marker in drug_text:
            return genes
    return []


def _drug_gene_compatibility(drug: object, gene: object) -> str:
    expected = _expected_genes_for_drug(drug)
    gene_text = _normalise_token(gene)
    if not expected or not gene_text or gene_text == "na":
        return "not_available"
    if any(_normalise_token(expected_gene) in gene_text for expected_gene in expected):
        return "compatible"
    return "check_catalogue"


def _drug_gene_status_label(drug: object, gene: object) -> str:
    """Human-readable status for operational resistance interpretation."""
    compatibility = _drug_gene_compatibility(drug, gene)
    if compatibility == "compatible":
        return "Valid expected gene"
    if compatibility == "check_catalogue":
        return "Unusual gene-drug mapping"
    return "Unknown / catalogue not available"


def _resistance_validation_key(sample_id: object, drug: object, gene: object, mutation: object) -> tuple[str, str, str, str]:
    return (
        str(sample_id or "").strip().lower(),
        _normalise_token(drug),
        _normalise_token(gene),
        _normalise_token(mutation),
    )


def _resistance_validation_lookup(validation_data: object) -> dict[tuple[str, str, str, str], dict]:
    if not isinstance(validation_data, dict):
        return {}
    records = validation_data.get("records")
    if not isinstance(records, list):
        return {}
    lookup: dict[tuple[str, str, str, str], dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = _resistance_validation_key(
            record.get("sample_id"),
            record.get("drug"),
            record.get("gene"),
            record.get("mutation"),
        )
        lookup[key] = record
    return lookup


def _resistance_report_status_label(validation_record: object, drug: object, gene: object) -> str:
    if isinstance(validation_record, dict):
        status = str(validation_record.get("report_status") or "").strip().lower()
        if status == "not_assessable":
            return "Not assessable"
        if status == "suppressed":
            return "Suppressed"
        if status == "validated":
            return "Validated"
        if status == "not_validated":
            return "Not validated"

    compatibility = _drug_gene_compatibility(drug, gene)
    if compatibility == "check_catalogue":
        return "Suppressed"
    return "Not validated"


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((str(left), str(right))))


def _pairwise_matrix(sequence_by_case: dict[str, str]) -> dict[tuple[str, str], int]:
    matrix: dict[tuple[str, str], int] = {}
    sequenced_cases = sorted(case_id for case_id, seq in sequence_by_case.items() if seq)
    for left, right in combinations(sequenced_cases, 2):
        dist = _snp_distance(sequence_by_case[left], sequence_by_case[right])
        if dist is not None:
            matrix[_pair_key(left, right)] = int(dist)
    return matrix


def _sha256_of_file(path: str) -> str:
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return "n/a"
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return "n/a"


def _write_csv_rows(path: str, rows: list[list[object]]) -> None:
    """Write UTF-8 CSV rows for appendix companion files without aborting report generation."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for row in rows:
                writer.writerow(["" if value is None else str(value) for value in row])
    except Exception:
        logger.exception("Unable to write companion CSV export to %s", path)


def _confidence_tier(
    *,
    qc_status: str,
    contamination_flag: bool,
    pairwise_distance: int | None,
    outbreaker_probability: float | None,
    same_cluster: bool,
) -> str:
    qc_ok = str(qc_status or "").lower() in ("pass", "passed") and not contamination_flag
    if not qc_ok:
        return "Exploratory"
    if pairwise_distance is not None and pairwise_distance <= 5 and same_cluster:
        return "High confidence"
    if pairwise_distance is not None and pairwise_distance <= 12 and same_cluster:
        return "Moderate confidence"
    return "Exploratory"


def _format_table_caption(caption_text: str, table_number: int) -> str:
    raw_caption = str(caption_text or "").strip()
    if re.match(r"^\s*Table\s+A\d+\.\s*", raw_caption):
        return raw_caption
    raw = re.sub(r"^\s*Table\s+\d+\.\s*", "", raw_caption).strip()
    return f"Table {table_number}. {raw}" if raw else f"Table {table_number}."


def _safe_html(value) -> str:
    """Escape a value for safe insertion into the static HTML report."""
    return html_lib.escape("" if value is None else str(value), quote=True)


def _load_export_json(filename: str):
    path = _export_path(filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        return {"error": f"Could not read {filename}: {exc}"}


def _load_export_csv(filename: str, limit: int | None = 50) -> list[dict]:
    path = _export_path(filename)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if limit is None:
                return list(reader)
            return [row for _, row in zip(range(limit), reader)]
    except Exception:
        return []


def _html_kv_table(mapping: dict | None, fields: list[tuple[str, str]]) -> str:
    if not mapping:
        return '<p class="muted">No data available.</p>'
    rows = []
    for label, key in fields:
        value = mapping.get(key)
        if isinstance(value, float):
            value = f"{value:.2f}"
        rows.append(f"<tr><th>{_safe_html(label)}</th><td>{_safe_html(value if value is not None else 'Not available')}</td></tr>")
    return '<table class="kv"><tbody>' + ''.join(rows) + '</tbody></table>'


def _html_data_table(rows: list[dict], columns: list[tuple[str, str]], empty_message: str) -> str:
    if not rows:
        return f'<p class="muted">{_safe_html(empty_message)}</p>'
    header = ''.join(f'<th>{_safe_html(label)}</th>' for label, _ in columns)
    body = []
    for row in rows:
        body.append('<tr>' + ''.join(f'<td>{_safe_html(row.get(key, ""))}</td>' for _, key in columns) + '</tr>')
    return '<table><thead><tr>' + header + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table>'


def _image_data_uri(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


def _pct(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if denominator in (None, 0):
        return None
    try:
        return (float(numerator or 0) / float(denominator)) * 100.0
    except Exception:
        return None


def _pct_label(value: float | None) -> str:
    return f"{float(value):.1f}%" if value is not None else "n/a"



