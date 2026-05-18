import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations


def _load_sequence_assignments(path: str):
    assignments = {}
    if not os.path.exists(path):
        return assignments
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            case_id = (row.get("case_id") or "").strip()
            cluster_id = (row.get("cluster_id") or "").strip()
            if case_id and cluster_id:
                assignments[case_id] = cluster_id
    return assignments


def _load_network_assignments(path: str):
    assignments = {}
    if not os.path.exists(path):
        return assignments
    payload = json.loads(open(path, "r", encoding="utf-8").read())

    nodes = payload.get("all_nodes") or payload.get("key_nodes") or []
    for node in nodes:
        full_case_id = (node.get("full_case_id") or "").strip()
        cluster_id = (node.get("cluster_id") or "").strip()
        if full_case_id and cluster_id:
            assignments[full_case_id] = cluster_id

    # If explicit cluster IDs are absent, infer components from edge connectivity.
    if assignments:
        return assignments

    edges = payload.get("edges") or []
    if not edges:
        return assignments

    graph = defaultdict(set)
    node_ids = set()

    for node in nodes:
        node_id = (node.get("full_case_id") or node.get("case_id") or "").strip()
        if node_id:
            node_ids.add(node_id)

    for edge in edges:
        src = (edge.get("source") or "").strip()
        dst = (edge.get("target") or "").strip()
        if not src or not dst:
            continue
        node_ids.add(src)
        node_ids.add(dst)
        graph[src].add(dst)
        graph[dst].add(src)

    seen = set()
    component_index = 1
    for node_id in sorted(node_ids):
        if node_id in seen:
            continue
        stack = [node_id]
        component_members = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component_members.append(current)
            for neighbor in graph.get(current, set()):
                if neighbor not in seen:
                    stack.append(neighbor)

        if len(component_members) >= 2:
            component_id = f"outbreaker_component_{component_index:03d}"
            for member in component_members:
                assignments[member] = component_id
            component_index += 1

    return assignments


def _same_cluster_pairs(mapping: dict):
    by_cluster = defaultdict(list)
    for case_id, cluster_id in mapping.items():
        by_cluster[cluster_id].append(case_id)

    pairs = set()
    for members in by_cluster.values():
        members_sorted = sorted(members)
        for a, b in combinations(members_sorted, 2):
            pairs.add((a, b))
    return pairs


def main():
    os.makedirs("exports", exist_ok=True)

    seq_path = "exports/sequence_cluster_assignments.csv"
    network_path = "exports/transmission_network.json"
    out_path = "exports/cluster_method_comparison.json"

    seq_assignments = _load_sequence_assignments(seq_path)
    net_assignments = _load_network_assignments(network_path)

    common_cases = sorted(set(seq_assignments).intersection(net_assignments))
    seq_common = {k: seq_assignments[k] for k in common_cases}
    net_common = {k: net_assignments[k] for k in common_cases}

    seq_pairs = _same_cluster_pairs(seq_common)
    net_pairs = _same_cluster_pairs(net_common)

    overlap_pairs = seq_pairs.intersection(net_pairs)
    union_pairs = seq_pairs.union(net_pairs)

    precision = (len(overlap_pairs) / len(net_pairs)) if net_pairs else None
    recall = (len(overlap_pairs) / len(seq_pairs)) if seq_pairs else None
    jaccard = (len(overlap_pairs) / len(union_pairs)) if union_pairs else None

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "sequence_assignments": seq_path,
            "outbreaker_network": network_path,
        },
        "coverage": {
            "sequence_assigned_cases": len(seq_assignments),
            "outbreaker_assigned_cases": len(net_assignments),
            "overlap_cases": len(common_cases),
        },
        "agreement": {
            "sequence_same_cluster_pairs": len(seq_pairs),
            "outbreaker_same_cluster_pairs": len(net_pairs),
            "overlap_same_cluster_pairs": len(overlap_pairs),
            "pairwise_precision_outbreaker_vs_sequence": precision,
            "pairwise_recall_outbreaker_vs_sequence": recall,
            "pairwise_jaccard": jaccard,
        },
        "notes": [
            "Pairwise metrics compare whether case pairs are grouped together by both methods.",
            "If outbreaker JSON contains only key nodes, coverage will be partial.",
            "Current transmission network generation uses case_clusters as input; agreement may be inflated if those clusters were sequence-derived first.",
        ],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

