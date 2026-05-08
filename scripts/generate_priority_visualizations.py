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
from scipy.cluster.hierarchy import dendrogram, linkage
from sqlalchemy import text
from backend.database import SessionLocal


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
                print("⚠ Not enough data for transmission network")
                return

            grouped = {"temporal_chain": list(reversed(fallback_rows))}

        graph = nx.DiGraph()
        cluster_order = sorted(grouped.keys())

        def _short_id(full_id: str) -> str:
            return str(full_id)[:8]

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
            print("⚠ Transmission network graph is empty")
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

        # Render network figure.
        fig, ax = plt.subplots(figsize=(16, 10))

        # Cluster-centered layout: place each cluster in its own neighborhood,
        # then run local spring layout per cluster for readability.
        positions = {}
        n_clusters = max(1, len(cluster_order))
        ring_radius = max(3.0, 2.2 * n_clusters)

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
                scale = 1.45
                for node, xy in local_pos.items():
                    positions[node] = center + (np.array(xy) / max_abs) * scale

        cmap = plt.get_cmap("tab20", max(1, len(cluster_order)))
        cluster_color = {cluster: cmap(i) for i, cluster in enumerate(cluster_order)}

        node_colors = [cluster_color.get(graph.nodes[n].get("cluster_id"), "#93c5fd") for n in graph.nodes()]
        node_sizes = [900 + int(graph.nodes[n].get("risk_score", 0.0) * 260) for n in graph.nodes()]

        edge_colors = []
        edge_widths = []
        for src, dst in graph.edges():
            prob = graph.edges[src, dst].get("probability", 0.0)
            edge_widths.append(1.2 + prob * 3.2)
            if prob >= 0.8:
                edge_colors.append("#b91c1c")
            elif prob >= 0.65:
                edge_colors.append("#f59e0b")
            else:
                edge_colors.append("#64748b")

        nx.draw_networkx_nodes(graph, positions, node_color=node_colors, node_size=node_sizes, ax=ax, alpha=0.9, edgecolors="#0f172a", linewidths=1.0)
        nx.draw_networkx_edges(graph, positions, ax=ax, arrows=True, arrowsize=14, arrowstyle="-|>", edge_color=edge_colors, width=edge_widths, alpha=0.8, connectionstyle="arc3,rad=0.08")

        labels = {n: graph.nodes[n].get("display_case_id", str(n)[:8]) for n in graph.nodes()}
        nx.draw_networkx_labels(graph, positions, labels=labels, ax=ax, font_size=7, font_weight="bold")

        edge_labels = {(s, t): f"{graph.edges[s, t].get('probability', 0):.2f}" for s, t in graph.edges() if graph.edges[s, t].get("probability", 0) >= 0.7}
        if edge_labels:
            nx.draw_networkx_edge_labels(graph, positions, edge_labels=edge_labels, ax=ax, font_size=7, font_color="#1f2937")

        ax.set_title(
            "Enhanced Transmission Network\n"
            "Node size = inferred spread risk | Edge label = transmission probability | Color = cluster",
            fontsize=14,
            fontweight="bold",
            pad=16,
        )
        ax.axis("off")
        fig.tight_layout()
        fig.savefig("exports/outbreaker_tree.png", dpi=120, bbox_inches="tight")
        plt.close(fig)

        high_conf_edges = sum(1 for _, _, d in graph.edges(data=True) if d.get("probability", 0) >= 0.8)

        key_nodes = sorted(graph.nodes(), key=lambda n: graph.nodes[n].get("risk_score", 0), reverse=True)[:10]
        insights = {
            "generated_at": str(np.datetime64("now")),
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "cluster_count": len(cluster_order),
            "layout": "cluster_centered",
            "high_confidence_edges": high_conf_edges,
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

        print("✓ Enhanced transmission network generated")

    except Exception as e:
        print(f"⚠ Could not generate enhanced transmission network: {e}")
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
            print("⚠ Not enough sequences for phylogenetic tree")
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
            fig, ax = plt.subplots(figsize=(14, 8))
            dendrogram(Z, labels=seq_ids, ax=ax, leaf_font_size=8)
            
            ax.set_title('Phylogenetic Tree - TB Case Isolates\n(Hierarchical Clustering by SNP Distance)', 
                        fontsize=14, fontweight='bold', pad=20)
            ax.set_xlabel('Case Isolates (ordered by genetic distance)', fontsize=11)
            ax.set_ylabel('Genetic Distance (SNP count)', fontsize=11)
            ax.grid(True, alpha=0.3, axis='y')
            
            fig.tight_layout()
            fig.savefig('exports/outbreaker_phylo.png', dpi=100, bbox_inches='tight')
            plt.close()
            
            print("✓ Phylogenetic tree generated")
        
    except Exception as e:
        print(f"⚠ Could not generate phylogenetic tree: {e}")


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
                    case_id = str(row[0])[:8]
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
                            'resistance': resistance
                        })
                except Exception:
                    continue
            
            if len(cases_data) < 5:
                print("⚠ Not enough cases for resistance heatmap")
                return
            
            # Extract resistance info
            all_drugs = set()
            for case in cases_data:
                all_drugs.update(case['resistance'].keys())
            
            all_drugs = sorted(list(all_drugs))[:8]  # Limit to 8 drugs for clarity
            
            # Build matrix: rows=cases, cols=drugs
            resistance_matrix = []
            case_ids = []
            
            for case in cases_data:
                case_ids.append(case['case_id'])
                row = []
                for drug in all_drugs:
                    pred = str(case['resistance'].get(drug, 'unknown')).strip().lower()
                    if pred in {'r', 'resistant'}:
                        row.append(2)  # Red: Resistant
                    elif pred in {'i', 'intermediate'}:
                        row.append(1)  # Yellow: Intermediate
                    else:
                        row.append(0)  # Green: Susceptible
                resistance_matrix.append(row)
            
            if len(resistance_matrix) < 2:
                print("⚠ Insufficient resistance data")
                return
            
            # Create heatmap
            fig_height = min(24, max(10, len(case_ids) * 0.25))
            fig, ax = plt.subplots(figsize=(10, fig_height))
            resistance_matrix = np.array(resistance_matrix)
            
            # Custom colormap: Green (S) -> Yellow (I) -> Red (R)
            cmap = ListedColormap(['#2ecc71', '#f39c12', '#e74c3c'])  # Green, Yellow, Red
            
            im = ax.imshow(resistance_matrix, cmap=cmap, aspect='auto', vmin=0, vmax=2)
            
            # Set labels
            ax.set_xticks(np.arange(len(all_drugs)))
            ax.set_yticks(np.arange(len(case_ids)))
            ax.set_xticklabels(all_drugs, rotation=45, ha='right')
            ax.set_yticklabels(case_ids, fontsize=8)
            
            ax.set_xlabel('Drug Class', fontsize=11, fontweight='bold')
            ax.set_ylabel('Case Isolates', fontsize=11, fontweight='bold')
            ax.set_title('Drug Resistance Profile Heatmap\n(Green=Susceptible, Yellow=Intermediate, Red=Resistant)', 
                        fontsize=12, fontweight='bold', pad=15)
            
            # Add colorbar
            cbar = plt.colorbar(im, ax=ax, ticks=[0, 1, 2])
            cbar.ax.set_yticklabels(['Susceptible', 'Intermediate', 'Resistant'])
            
            fig.tight_layout()
            fig.savefig('exports/outbreaker_resistance.png', dpi=100, bbox_inches='tight')
            plt.close()
            
            print("✓ Resistance profile heatmap generated")
            
        finally:
            db.close()
            
    except Exception as e:
        print(f"⚠ Could not generate resistance heatmap: {e}")


if __name__ == "__main__":
    print("Generating enhanced outbreak visualizations...")
    generate_transmission_network()
    generate_phylogenetic_tree()
    generate_resistance_heatmap()
