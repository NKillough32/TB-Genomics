#!/usr/bin/env python3
"""Render a transmission tree image from exports/transmission_network.json."""

from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx

from scripts.runtime_paths import EXPORTS


def main() -> int:
    exports_dir = EXPORTS
    exports_dir.mkdir(parents=True, exist_ok=True)

    network_path = exports_dir / "transmission_network.json"
    out_path = exports_dir / "outbreaker_tree.png"

    if not network_path.exists():
        fig, ax = plt.subplots(figsize=(12, 8))
        ax.text(0.5, 0.5, "Transmission network JSON not found", ha="center", va="center", fontsize=14)
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(out_path, dpi=130)
        plt.close(fig)
        print("render_transmission_tree: no network JSON; placeholder generated")
        return 0

    with network_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("all_nodes") or data.get("key_nodes") or []
    edges = data.get("edges") or []

    graph = nx.DiGraph()

    for node in nodes:
        case_id = str(node.get("full_case_id") or node.get("case_id") or "")
        if not case_id:
            continue
        graph.add_node(
            case_id,
            label=str(node.get("case_id") or case_id[:8]),
            risk=float(node.get("risk_score") or 0.0),
            band=str(node.get("risk_band") or "low"),
        )

    for edge in edges:
        src = str(edge.get("source") or "")
        dst = str(edge.get("target") or "")
        if not src or not dst:
            continue
        if src not in graph:
            graph.add_node(src, label=src[:8], risk=0.0, band="low")
        if dst not in graph:
            graph.add_node(dst, label=dst[:8], risk=0.0, band="low")
        graph.add_edge(src, dst, probability=float(edge.get("probability") or 0.0))

    fig, ax = plt.subplots(figsize=(14, 10))

    if graph.number_of_nodes() == 0:
        ax.text(0.5, 0.5, "No transmission nodes available", ha="center", va="center", fontsize=14)
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(out_path, dpi=130)
        plt.close(fig)
        print("render_transmission_tree: no nodes; placeholder generated")
        return 0

    # Prefer graphviz-like layout when possible; fall back to spring layout.
    try:
        positions = nx.nx_agraph.graphviz_layout(graph, prog="dot")
    except Exception:
        positions = nx.spring_layout(graph, seed=42, k=0.8, iterations=120)

    color_map = {"high": "#b91c1c", "medium": "#f59e0b", "low": "#2563eb"}
    node_colors = [color_map.get(graph.nodes[n].get("band", "low"), "#64748b") for n in graph.nodes()]
    node_sizes = [700 + int(max(0.0, graph.nodes[n].get("risk", 0.0)) * 180) for n in graph.nodes()]

    edge_colors = []
    edge_widths = []
    for src, dst in graph.edges():
        prob = float(graph.edges[src, dst].get("probability", 0.0))
        edge_widths.append(1.0 + prob * 2.8)
        if prob >= 0.8:
            edge_colors.append("#991b1b")
        elif prob >= 0.6:
            edge_colors.append("#d97706")
        else:
            edge_colors.append("#475569")

    nx.draw_networkx_nodes(
        graph,
        positions,
        node_color=node_colors,
        node_size=node_sizes,
        ax=ax,
        edgecolors="#0f172a",
        linewidths=0.9,
        alpha=0.92,
    )
    nx.draw_networkx_edges(
        graph,
        positions,
        ax=ax,
        arrows=True,
        arrowsize=14,
        arrowstyle="-|>",
        edge_color=edge_colors,
        width=edge_widths,
        alpha=0.82,
        connectionstyle="arc3,rad=0.06",
    )

    labels = {n: graph.nodes[n].get("label", str(n)[:8]) for n in graph.nodes()}
    nx.draw_networkx_labels(graph, positions, labels=labels, ax=ax, font_size=8, font_weight="bold")

    edge_labels = {
        (s, t): f"{graph.edges[s, t].get('probability', 0.0):.2f}"
        for s, t in graph.edges()
        if float(graph.edges[s, t].get("probability", 0.0)) >= 0.7
    }
    if edge_labels:
        nx.draw_networkx_edge_labels(graph, positions, edge_labels=edge_labels, ax=ax, font_size=7, font_color="#1f2937")

    ax.set_title("Transmission Tree (Derived From Posterior Network)", fontsize=14, fontweight="bold", pad=14)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)

    print(f"render_transmission_tree: wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

