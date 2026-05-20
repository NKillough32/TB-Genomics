#!/usr/bin/env python3
"""
Generate enhanced outbreak visualizations from TB case data.
"""

import sys
import os
from pathlib import Path

# Get project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import json
import numpy as np
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from scipy.cluster.hierarchy import dendrogram, linkage
from sqlalchemy import text
from backend.database import SessionLocal

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#cbd5e1",
    "axes.labelcolor": "#0f172a",
    "axes.titlecolor": "#0f172a",
    "xtick.color": "#334155",
    "ytick.color": "#334155",
    "font.size": 10,
    "savefig.dpi": 140,
})


def _to_iso_date(value) -> str:
    if value is None:
        return ""
    return str(value)[:10]


def _resistant_drug_count(resistance_payload) -> int:
    if resistance_payload is None:
        return 0

    resistance = resistance_payload
    if isinstance(resistance_payload, str):
        try:
            resistance = json.loads(resistance_payload)
        except Exception:
            return 0

    if not isinstance(resistance, dict):
        return 0

    count = 0
    for _, value in resistance.items():
        marker = str(value).strip().lower()
        if marker in {"r", "resistant"}:
            count += 1
    return count


def _short_id(full_id: str) -> str:
    return str(full_id)[:8]


def _display_cluster(cluster_id: str) -> str:
    if not cluster_id:
        return "Unclustered"
    return str(cluster_id)[:8] if len(str(cluster_id)) >= 8 else str(cluster_id)


def _title_case_drug(value: str) -> str:
    return str(value).replace("_", " ").replace("-", " ").title()


_DRUG_KEY_ALIASES = {
    "isoniazid": "isoniazid",
    "inh": "isoniazid",
    "rifampicin": "rifampicin",
    "rifampin": "rifampicin",
    "rif": "rifampicin",
    "ethambutol": "ethambutol",
    "emb": "ethambutol",
    "pyrazinamide": "pyrazinamide",
    "pza": "pyrazinamide",
    "fluoroquinolones": "fluoroquinolones",
    "fluoroquinolone": "fluoroquinolones",
    "fqs": "fluoroquinolones",
    "quinolones": "fluoroquinolones",
    "aminoglycosides": "aminoglycosides / injectables",
    "aminoglycoside": "aminoglycosides / injectables",
    "injectables": "aminoglycosides / injectables",
    "aminoglycosides / injectables": "aminoglycosides / injectables",
}

_DRUG_DISPLAY_ORDER = [
    "isoniazid",
    "rifampicin",
    "ethambutol",
    "pyrazinamide",
    "fluoroquinolones",
    "aminoglycosides / injectables",
]


def _normalise_resistance_drug_key(value: str) -> str | None:
    cleaned = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    cleaned = " ".join(cleaned.split())
    return _DRUG_KEY_ALIASES.get(cleaned)


def _display_resistance_drug(value: str) -> str:
    if value == "aminoglycosides / injectables":
        return "Injectables"
    return _title_case_drug(value)


def _estimate_transmission_probability(source: dict, target: dict) -> float:
    score = 0.55

    if source.get("region") and source.get("region") == target.get("region"):
        score += 0.2

    date_src = source.get("specimen_date")
    date_tgt = target.get("specimen_date")
    if date_src and date_tgt:
        try:
            delta = abs((date_tgt - date_src).days)
            if delta <= 14:
                score += 0.15
            elif delta <= 30:
                score += 0.08
            else:
                score -= 0.05
        except Exception:
            pass

    if source.get("lineage") and source.get("lineage") == target.get("lineage"):
        score += 0.07

    resistant_gap = abs(source.get("resistant_drug_count", 0) - target.get("resistant_drug_count", 0))
    if resistant_gap == 0:
        score += 0.05
    elif resistant_gap >= 3:
        score -= 0.07

    return round(float(max(0.05, min(score, 0.98))), 2)


def generate_transmission_network():
    """Generate enhanced transmission network image and structured JSON insights."""
    os.makedirs("exports", exist_ok=True)

    db = SessionLocal()
    try:
        clustered_rows = db.execute(
            text(
                """
                SELECT
                    c.pseudonymised_case_id::text AS case_id,
                    c.specimen_date,
                    c.geographic_region,
                    ti.lineage,
                    ti.predicted_drug_resistance,
                    cc.cluster_id::text AS cluster_id,
                    COUNT(*) OVER (PARTITION BY cc.cluster_id) AS cluster_size
                FROM case_clusters cc
                JOIN cases c ON cc.sample_id = c.pseudonymised_case_id
                LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
                ORDER BY cc.cluster_id, c.specimen_date
                """
            )
        ).mappings().all()

        grouped = {}
        for row in clustered_rows:
            grouped.setdefault(row["cluster_id"], []).append(row)

        if not grouped:
            # Fallback: build a temporal chain using recent cases for demo continuity.
            fallback_rows = db.execute(
                text(
                    """
                    SELECT
                        c.pseudonymised_case_id::text AS case_id,
                        c.specimen_date,
                        c.geographic_region,
                        ti.lineage,
                        ti.predicted_drug_resistance
                    FROM cases c
                    LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
                    ORDER BY c.specimen_date DESC NULLS LAST
                    LIMIT 20
                    """
                )
            ).mappings().all()

            if len(fallback_rows) < 2:
                print("[warn] Not enough data for transmission network")
                return

            grouped = {"temporal_chain": list(reversed(fallback_rows))}

        graph = nx.DiGraph()
        cluster_order = sorted(grouped.keys())

        # Resolve 8-char collisions so labels stay human-readable but unique.
        short_counts = {}
        for cluster_id in cluster_order:
            for row in grouped[cluster_id]:
                sid = _short_id(row["case_id"])
                short_counts[sid] = short_counts.get(sid, 0) + 1

        for cluster_id in cluster_order:
            rows = grouped[cluster_id]
            rows_sorted = sorted(rows, key=lambda r: _to_iso_date(r.get("specimen_date")))
            for row in rows_sorted:
                full_case_id = str(row["case_id"])
                short_case_id = _short_id(full_case_id)
                display_case_id = short_case_id
                if short_counts.get(short_case_id, 0) > 1:
                    display_case_id = f"{short_case_id}-{full_case_id[8:12]}"
                graph.add_node(
                    full_case_id,
                    short_case_id=short_case_id,
                    display_case_id=display_case_id,
                    full_case_id=full_case_id,
                    cluster_id=cluster_id,
                    specimen_date=_to_iso_date(row.get("specimen_date")),
                    region=row.get("geographic_region") or "Unknown",
                    lineage=row.get("lineage") or "Unknown",
                    resistant_drug_count=_resistant_drug_count(row.get("predicted_drug_resistance")),
                )

            for idx in range(len(rows_sorted) - 1):
                src_row = rows_sorted[idx]
                dst_row = rows_sorted[idx + 1]
                src_id = str(src_row["case_id"])
                dst_id = str(dst_row["case_id"])
                source = graph.nodes[src_id]
                target = graph.nodes[dst_id]
                prob = _estimate_transmission_probability(source, target)
                graph.add_edge(src_id, dst_id, probability=prob, confidence="high" if prob >= 0.8 else "medium" if prob >= 0.65 else "low")

                # Add short-range alternative path when cases are tightly linked in time.
                if idx + 2 < len(rows_sorted):
                    alt_dst_row = rows_sorted[idx + 2]
                    alt_dst_id = str(alt_dst_row["case_id"])
                    alt_target = {
                        "region": alt_dst_row.get("geographic_region") or "Unknown",
                        "lineage": alt_dst_row.get("lineage") or "Unknown",
                        "specimen_date": alt_dst_row.get("specimen_date"),
                        "resistant_drug_count": _resistant_drug_count(alt_dst_row.get("predicted_drug_resistance")),
                    }
                    alt_prob = _estimate_transmission_probability(source, alt_target) - 0.1
                    alt_prob = round(max(0.05, min(alt_prob, 0.95)), 2)
                    if alt_prob >= 0.55 and not graph.has_edge(src_id, alt_dst_id):
                        graph.add_edge(src_id, alt_dst_id, probability=alt_prob, confidence="medium" if alt_prob >= 0.7 else "low")

        if graph.number_of_nodes() == 0:
            print("[warn] Transmission network graph is empty")
            return

        out_weight = dict(graph.out_degree(weight="probability"))
        in_weight = dict(graph.in_degree(weight="probability"))
        centrality = nx.betweenness_centrality(graph.to_undirected()) if graph.number_of_nodes() > 2 else {n: 0.0 for n in graph.nodes()}

        for node in graph.nodes:
            risk_score = round(out_weight.get(node, 0.0) * 0.7 + in_weight.get(node, 0.0) * 0.3 + centrality.get(node, 0.0), 3)
            graph.nodes[node]["risk_score"] = risk_score
            if risk_score >= 1.8:
                graph.nodes[node]["risk_band"] = "high"
            elif risk_score >= 1.1:
                graph.nodes[node]["risk_band"] = "medium"
            else:
                graph.nodes[node]["risk_band"] = "low"

        high_conf_edges = sum(1 for _, _, d in graph.edges(data=True) if d.get("probability", 0) >= 0.8)

        # Render network figure.
        fig, ax = plt.subplots(figsize=(13.6, 9.6))

        # Cluster-centered layout: place each cluster in its own neighborhood,
        # then run local spring layout per cluster for readability.
        positions = {}
        n_clusters = max(1, len(cluster_order))
        ring_radius = max(2.8, min(5.2, 1.25 * n_clusters))

        for idx, cluster_id in enumerate(cluster_order):
            cluster_nodes = [n for n in graph.nodes() if graph.nodes[n].get("cluster_id") == cluster_id]
            if not cluster_nodes:
                continue

            angle = (2 * np.pi * idx) / n_clusters
            center = np.array([ring_radius * np.cos(angle), ring_radius * np.sin(angle)])

            subgraph = graph.subgraph(cluster_nodes).to_undirected()
            if subgraph.number_of_nodes() == 1:
                positions[cluster_nodes[0]] = center
            else:
                local_pos = nx.spring_layout(subgraph, seed=42 + idx, k=1.6, iterations=120)
                local_pos_arr = np.array(list(local_pos.values()))
                max_abs = max(1.0, float(np.max(np.abs(local_pos_arr))))
                scale = 0.95
                for node, xy in local_pos.items():
                    positions[node] = center + (np.array(xy) / max_abs) * scale

        palette = [
            "#2563eb", "#d97706", "#dc2626", "#059669", "#7c3aed",
            "#0891b2", "#be123c", "#65a30d", "#9333ea", "#ea580c",
        ]
        cluster_color = {cluster: palette[i % len(palette)] for i, cluster in enumerate(cluster_order)}

        node_colors = [cluster_color.get(graph.nodes[n].get("cluster_id"), "#93c5fd") for n in graph.nodes()]
        node_sizes = [760 + int(graph.nodes[n].get("risk_score", 0.0) * 220) for n in graph.nodes()]

        edge_colors = []
        edge_widths = []
        for src, dst in graph.edges():
            prob = graph.edges[src, dst].get("probability", 0.0)
            edge_widths.append(0.9 + prob * 2.4)
            if prob >= 0.8:
                edge_colors.append("#b91c1c")
            elif prob >= 0.65:
                edge_colors.append("#f59e0b")
            else:
                edge_colors.append("#64748b")

        nx.draw_networkx_nodes(
            graph,
            positions,
            node_color=node_colors,
            node_size=node_sizes,
            ax=ax,
            alpha=0.92,
            edgecolors="#0f172a",
            linewidths=0.9,
        )
        nx.draw_networkx_edges(
            graph,
            positions,
            ax=ax,
            arrows=True,
            arrowsize=10,
            arrowstyle="-|>",
            edge_color=edge_colors,
            width=edge_widths,
            alpha=0.72,
            connectionstyle="arc3,rad=0.07",
        )

        # Label only priority nodes to avoid unreadable overlap in dense clusters.
        sorted_by_risk = sorted(
            graph.nodes(), key=lambda n: graph.nodes[n].get("risk_score", 0.0), reverse=True
        )
        top_global = set(sorted_by_risk[:max(12, min(24, graph.number_of_nodes() // 8))])
        top_per_cluster = set()
        for cluster_id in cluster_order:
            cluster_nodes = [n for n in graph.nodes() if graph.nodes[n].get("cluster_id") == cluster_id]
            if not cluster_nodes:
                continue
            cluster_sorted = sorted(
                cluster_nodes, key=lambda n: graph.nodes[n].get("risk_score", 0.0), reverse=True
            )
            top_per_cluster.update(cluster_sorted[:2])

        label_nodes = top_global | top_per_cluster
        labels = {n: graph.nodes[n].get("display_case_id", str(n)[:8]) for n in label_nodes}
        nx.draw_networkx_labels(
            graph,
            positions,
            labels=labels,
            ax=ax,
            font_size=7.8,
            font_weight="bold",
            bbox=dict(facecolor="white", edgecolor="none", alpha=0.58, boxstyle="round,pad=0.12"),
        )

        edge_labels = {
            (s, t): f"{graph.edges[s, t].get('probability', 0):.2f}"
            for s, t in graph.edges()
            if graph.edges[s, t].get("probability", 0) >= 0.8
        }
        # Draw edge labels only for smaller/sparser networks; dense plots become unreadable.
        if edge_labels and graph.number_of_edges() <= 45:
            nx.draw_networkx_edge_labels(graph, positions, edge_labels=edge_labels, ax=ax, font_size=6.5, font_color="#334155")

        ax.set_title(
            "Posterior Transmission Network\n"
            f"{graph.number_of_nodes()} cases | {graph.number_of_edges()} links | {high_conf_edges} high-confidence links | labelled priority cases",
            fontsize=15,
            fontweight="bold",
            pad=12,
        )
        ax.axis("off")

        legend_items = [
            Line2D([0], [0], color="#b91c1c", lw=3, label="Posterior >= 0.80"),
            Line2D([0], [0], color="#f59e0b", lw=3, label="Posterior 0.65-0.79"),
            Line2D([0], [0], color="#64748b", lw=3, label="Posterior < 0.65"),
            Line2D([0], [0], marker="o", color="w", markerfacecolor="#94a3b8",
                   markeredgecolor="#0f172a", markersize=11, label="Larger node = higher inferred spread risk"),
        ]
        cluster_handles = [
            Patch(facecolor=cluster_color[c], edgecolor="#0f172a", label=f"Cluster {_display_cluster(c)}")
            for c in cluster_order[:6]
        ]
        if len(cluster_order) > 6:
            cluster_handles.append(Patch(facecolor="#e2e8f0", edgecolor="#0f172a", label=f"+{len(cluster_order) - 6} more clusters"))
        ax.legend(
            handles=legend_items + cluster_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.02),
            ncol=2,
            frameon=True,
            framealpha=0.95,
            facecolor="white",
            edgecolor="#cbd5e1",
            fontsize=8,
        )

        fig.subplots_adjust(left=0.02, right=0.98, top=0.88, bottom=0.19)
        fig.savefig("exports/outbreaker_tree.png", dpi=140, bbox_inches="tight", pad_inches=0.12)
        plt.close(fig)

        key_nodes = sorted(graph.nodes(), key=lambda n: graph.nodes[n].get("risk_score", 0), reverse=True)[:10]
        insights = {
            "generated_at": str(np.datetime64("now")),
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "cluster_count": len(cluster_order),
            "layout": "cluster_centered",
            "high_confidence_edges": high_conf_edges,
            "all_nodes": [
                {
                    "case_id": graph.nodes[node].get("display_case_id", node[:8]),
                    "full_case_id": graph.nodes[node].get("full_case_id", node),
                    "cluster_id": graph.nodes[node].get("cluster_id"),
                    "region": graph.nodes[node].get("region"),
                    "risk_score": graph.nodes[node].get("risk_score"),
                    "risk_band": graph.nodes[node].get("risk_band"),
                    "outgoing_links": int(graph.out_degree(node)),
                    "incoming_links": int(graph.in_degree(node)),
                }
                for node in sorted(graph.nodes())
            ],
            "key_nodes": [
                {
                    "case_id": graph.nodes[node].get("display_case_id", node[:8]),
                    "full_case_id": graph.nodes[node].get("full_case_id", node),
                    "cluster_id": graph.nodes[node].get("cluster_id"),
                    "region": graph.nodes[node].get("region"),
                    "risk_score": graph.nodes[node].get("risk_score"),
                    "risk_band": graph.nodes[node].get("risk_band"),
                    "outgoing_links": int(graph.out_degree(node)),
                    "incoming_links": int(graph.in_degree(node)),
                }
                for node in key_nodes
            ],
            "clusters": [
                {
                    "cluster_id": cluster,
                    "nodes": int(sum(1 for n in graph.nodes() if graph.nodes[n].get("cluster_id") == cluster)),
                }
                for cluster in cluster_order
            ],
        }

        with open("exports/transmission_network.json", "w", encoding="utf-8") as f:
            json.dump(insights, f, indent=2)

        print("[ok] Enhanced transmission network generated")

    except Exception as e:
        print(f"[warn] Could not generate enhanced transmission network: {e}")
    finally:
        db.close()

def generate_phylogenetic_tree():
    """Generate phylogenetic tree from DNA sequences."""
    os.makedirs("exports", exist_ok=True)

    try:
        # Read DNA sequences from FASTA
        sequences = {}
        if os.path.exists("exports/dna.fasta"):
            with open("exports/dna.fasta", "r") as f:
                current_id = None
                for line in f:
                    line = line.strip()
                    if line.startswith(">"):
                        current_id = line[1:].split()[0]
                        sequences[current_id] = ""
                    else:
                        sequences[current_id] += line

        if len(sequences) < 2:
            print("[warn] Not enough sequences for phylogenetic tree")
            return

        # Create distance matrix from sequences
        seq_ids = sorted(list(sequences.keys()))
        seq_list = [sequences[sid] for sid in seq_ids]

        # Calculate Hamming distances between sequences
        distances = []
        for i in range(len(seq_list)):
            for j in range(i+1, len(seq_list)):
                seq1, seq2 = seq_list[i], seq_list[j]
                if len(seq1) == len(seq2):
                    dist = sum(c1 != c2 for c1, c2 in zip(seq1, seq2))
                else:
                    dist = max(len(seq1), len(seq2))
                distances.append(dist)

        # Perform hierarchical clustering
        if len(distances) > 0:
            Z = linkage(distances, method='ward')

            # Create dendrogram
            fig_width = 15
            fig_height = min(12, max(7.5, len(seq_ids) * 0.34))
            fig, ax = plt.subplots(figsize=(fig_width, fig_height), constrained_layout=True)
            dendrogram(
                Z,
                labels=[_short_id(sid) for sid in seq_ids],
                ax=ax,
                leaf_font_size=8,
                color_threshold=25,
                above_threshold_color="#64748b",
            )

            ax.set_title(
                f'Phylogenetic Context - {len(seq_ids)} Sequenced Isolates\nHierarchical clustering by pairwise SNP distance',
                fontsize=15,
                fontweight='bold',
                pad=14,
            )
            ax.set_xlabel('Case isolates (short IDs, ordered by genetic distance)', fontsize=11)
            ax.set_ylabel('Genetic Distance (SNP count)', fontsize=11)
            ax.grid(True, alpha=0.28, axis='y', color="#cbd5e1")
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            for label in ax.get_xticklabels():
                label.set_rotation(55)
                label.set_ha("right")

            fig.savefig('exports/outbreaker_phylo.png', dpi=140, bbox_inches='tight', pad_inches=0.12)
            plt.close()

            print("[ok] Phylogenetic tree generated")

    except Exception as e:
        print(f"[warn] Could not generate phylogenetic tree: {e}")


def generate_resistance_heatmap():
    """Generate resistance profile heatmap."""
    os.makedirs("exports", exist_ok=True)

    try:
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap
        import numpy as np

        db = SessionLocal()
        try:
            # Query all cases with resistance predictions
            result = db.execute(text("""
                SELECT
                    c.pseudonymised_case_id,
                    c.geographic_region,
                    ti.predicted_drug_resistance
                FROM cases c
                LEFT JOIN tb_interpretation ti ON c.pseudonymised_case_id = ti.sample_id
                ORDER BY c.geographic_region, c.specimen_date
            """))

            cases_data = []
            for row in result:
                try:
                    full_case_id = str(row[0])
                    case_id = full_case_id[:8]
                    region = row[1] or "Unknown"
                    raw_resistance = row[2]

                    if isinstance(raw_resistance, dict):
                        resistance = raw_resistance
                    elif isinstance(raw_resistance, str) and raw_resistance.strip():
                        resistance = json.loads(raw_resistance)
                    else:
                        resistance = {}

                    if isinstance(resistance, dict):
                        cases_data.append({
                            'case_id': case_id,
                            'region': region,
                            'resistance': resistance,
                            'resistant_count': _resistant_drug_count(resistance),
                        })
                except Exception:
                    continue

            if len(cases_data) < 5:
                print("[warn] Not enough cases for resistance heatmap")
                return

            # Extract resistance info using only canonical drug-class keys.
            all_drugs = set()
            for case in cases_data:
                normalised = {}
                for key, value in case['resistance'].items():
                    canonical_key = _normalise_resistance_drug_key(key)
                    if canonical_key:
                        normalised[canonical_key] = value
                case['resistance'] = normalised
                all_drugs.update(normalised.keys())

            all_drugs = [drug for drug in _DRUG_DISPLAY_ORDER if drug in all_drugs][:8]
            cases_data.sort(key=lambda c: (-c["resistant_count"], c["region"], c["case_id"]))

            # Build matrix: rows=cases, cols=drugs
            resistance_matrix = []
            case_ids = []

            for case in cases_data:
                case_ids.append(case['case_id'])
                row = []
                for drug in all_drugs:
                    raw_pred = case['resistance'].get(drug)
                    pred = str(raw_pred or 'unknown').strip().lower()
                    if pred in {'r', 'resistant'}:
                        row.append(3)  # Red: Resistant
                    elif pred in {'i', 'intermediate'}:
                        row.append(2)  # Amber: Intermediate
                    elif raw_pred is None or pred in {'', 'unknown', 'none', 'null'}:
                        row.append(0)  # Grey: No call
                    else:
                        row.append(1)  # Green: Susceptible
                resistance_matrix.append(row)

            if len(resistance_matrix) < 2:
                print("[warn] Insufficient resistance data")
                return

            # Create heatmap
            fig_height = min(14, max(7.5, len(case_ids) * 0.34))
            fig, ax = plt.subplots(figsize=(12, fig_height), constrained_layout=True)
            resistance_matrix = np.array(resistance_matrix)

            # Custom colormap: No call -> Susceptible -> Intermediate -> Resistant
            cmap = ListedColormap(['#e5e7eb', '#5fbf7a', '#f2b84b', '#d94f45'])

            im = ax.imshow(resistance_matrix, cmap=cmap, aspect='auto', vmin=0, vmax=3)

            # Set labels
            ax.set_xticks(np.arange(len(all_drugs)))
            ax.set_yticks(np.arange(len(case_ids)))
            ax.set_xticklabels([_display_resistance_drug(d) for d in all_drugs], rotation=35, ha='right')
            ax.set_yticklabels(case_ids, fontsize=8)
            ax.set_xticks(np.arange(-.5, len(all_drugs), 1), minor=True)
            ax.set_yticks(np.arange(-.5, len(case_ids), 1), minor=True)
            ax.grid(which='minor', color='white', linestyle='-', linewidth=1.4)
            ax.tick_params(which='minor', bottom=False, left=False)

            ax.set_xlabel('Drug Class', fontsize=11, fontweight='bold')
            ax.set_ylabel('Case Isolates', fontsize=11, fontweight='bold')
            ax.set_title(
                f'Drug Resistance Profile Heatmap\n{len(case_ids)} cases sorted by resistance burden',
                fontsize=14,
                fontweight='bold',
                pad=12,
            )

            # Add colorbar
            cbar = plt.colorbar(im, ax=ax, ticks=[0, 1, 2, 3], fraction=0.035, pad=0.035)
            cbar.ax.set_yticklabels(['No call', 'Susceptible', 'Intermediate', 'Resistant'])
            cbar.ax.tick_params(labelsize=8)

            fig.savefig('exports/outbreaker_resistance.png', dpi=140, bbox_inches='tight', pad_inches=0.12)
            plt.close()

            print("[ok] Resistance profile heatmap generated")

        finally:
            db.close()

    except Exception as e:
        print(f"[warn] Could not generate resistance heatmap: {e}")


if __name__ == "__main__":
    print("Generating enhanced outbreak visualizations...")
    if os.getenv("TB_SKIP_PRIORITY_NETWORK", "0") != "1":
        generate_transmission_network()
    else:
        print("Skipping transmission network generation due to TB_SKIP_PRIORITY_NETWORK=1")
    generate_phylogenetic_tree()
    generate_resistance_heatmap()

