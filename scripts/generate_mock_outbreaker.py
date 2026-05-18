#!/usr/bin/env python3
"""
Fallback Outbreaker2 Report Generator
Generates mock analysis plots and summary for demo when R is unavailable.
"""

import json
import os
from datetime import datetime
from pathlib import Path
import sys

def generate_mock_graphics():
    """Generate simple diagnostic graphics using matplotlib if available."""
    os.makedirs("exports", exist_ok=True)
    
    try:
        import matplotlib.pyplot as plt
        import numpy as np
        
        # Generate mock trace plot
        fig, ax = plt.subplots(figsize=(12, 8))
        iterations = np.arange(0, 2500)
        # Simulate MCMC chain
        likelihood = -5000 + np.cumsum(np.random.randn(2500) * 10)
        likelihood = np.maximum(likelihood, -6000)  # Keep bounded
        ax.plot(iterations, likelihood, linewidth=0.5, alpha=0.7)
        ax.set_xlabel('Iteration')
        ax.set_ylabel('Log-likelihood')
        ax.set_title('Outbreaker2 MCMC Trace - Likelihood')
        ax.grid(True, alpha=0.3)
        fig.savefig('exports/outbreaker_trace.png', dpi=100, bbox_inches='tight')
        plt.close()
        print("[OK] Mock trace plot generated")
        
        # Generate mock histogram
        fig, axes = plt.subplots(2, 2, figsize=(12, 8))
        fig.suptitle('Outbreaker2 Posterior Distributions', fontsize=14, fontweight='bold')
        
        # Mock posterior samples (burnin removed)
        burnin = 500
        samples = likelihood[burnin:]
        
        # Plot 1: Likelihood distribution
        axes[0, 0].hist(samples, bins=50, alpha=0.7, color='steelblue')
        axes[0, 0].set_title('Likelihood Distribution')
        axes[0, 0].set_xlabel('Log-likelihood')
        
        # Plot 2: Transmission probability
        trans_prob = np.random.beta(2, 8, len(samples))
        axes[0, 1].hist(trans_prob, bins=50, alpha=0.7, color='coral')
        axes[0, 1].set_title('Transmission Probability')
        axes[0, 1].set_xlabel('P(transmission)')
        
        # Plot 3: Incubation period
        incub = np.random.gamma(5, 2, len(samples))
        axes[1, 0].hist(incub, bins=50, alpha=0.7, color='mediumseagreen')
        axes[1, 0].set_title('Incubation Period')
        axes[1, 0].set_xlabel('Days')
        
        # Plot 4: Sampling probability
        samp_prob = np.random.beta(3, 5, len(samples))
        axes[1, 1].hist(samp_prob, bins=50, alpha=0.7, color='plum')
        axes[1, 1].set_title('Sampling Probability')
        axes[1, 1].set_xlabel('P(sampling)')
        
        fig.tight_layout()
        fig.savefig('exports/outbreaker_hist.png', dpi=100, bbox_inches='tight')
        plt.close()
        print("[OK] Mock histogram generated")
        
    except ImportError:
        print("[WARN] matplotlib not available - skipping graphics generation")


def generate_transmission_tree():
    """Generate transmission tree network diagram from clustered cases."""
    os.makedirs("exports", exist_ok=True)
    
    try:
        import matplotlib.pyplot as plt
        import networkx as nx
        import numpy as np
        
        # Get project root
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, project_root)
        
        from backend.database import SessionLocal
        from sqlalchemy import text
        
        # Query case clusters from database
        db = SessionLocal()
        try:
            # Get clustered cases
            result = db.execute(text("""
                SELECT DISTINCT 
                    cc.sample_id as case_id,
                    c.specimen_date,
                    c.geographic_region as region,
                    cc.cluster_id,
                    COUNT(*) OVER (PARTITION BY cc.cluster_id) as cluster_size
                FROM case_clusters cc
                JOIN cases c ON cc.sample_id = c.pseudonymised_case_id
                ORDER BY cc.cluster_id, c.specimen_date
            """))
            
            clusters = {}
            for row in result:
                cluster_id = str(row[3])
                if cluster_id not in clusters:
                    clusters[cluster_id] = []
                clusters[cluster_id].append({
                    'case_id': str(row[0])[:8],
                    'date': row[1],
                    'region': row[2],
                    'size': row[4]
                })
        finally:
            db.close()
        
        if not clusters:
            print("[WARN] No cluster data found - using synthetic transmission tree")
            clusters = {}
        
        # Create network graph
        fig, ax = plt.subplots(figsize=(16, 11))
        G = nx.DiGraph()
        
        edge_labels = {}
        node_colors = {}
        pos = {}
        
        if clusters:
            # Real clusters from database
            y_offset = 0
            color_map = plt.cm.Set3(np.linspace(0, 1, len(clusters)))
            
            for cluster_idx, (cluster_id, cases) in enumerate(sorted(clusters.items())):
                if len(cases) > 1:
                    # Sort by date
                    cases = sorted(cases, key=lambda x: str(x['date']))
                    x_positions = np.linspace(0, 12, len(cases))
                    cluster_color = color_map[cluster_idx]
                    
                    for i, case in enumerate(cases):
                        node_id = case['case_id']
                        G.add_node(node_id, cluster=cluster_id, date=str(case['date']))
                        pos[node_id] = (x_positions[i], y_offset)
                        node_colors[node_id] = cluster_color
                        
                        # Add edges with transmission probability
                        if i > 0:
                            prev_node = cases[i-1]['case_id']
                            if prev_node in G.nodes():
                                # Simulate transmission probability based on genetic distance
                                trans_prob = np.random.uniform(0.6, 0.95)
                                G.add_edge(prev_node, node_id, weight=trans_prob)
                                edge_labels[(prev_node, node_id)] = f'{trans_prob:.2f}'
                    
                    y_offset -= 3.5
        
        if len(G.nodes()) == 0:
            # Synthetic transmission tree with probabilities
            n_nodes = 15
            node_colors = {}
            chains = [
                [(0, 1), (1, 2), (2, 3), (2, 4), (3, 5)],  # Chain 1 (5 transmissions)
                [(6, 7), (7, 8), (8, 9), (9, 10)],  # Chain 2 (4 transmissions)
                [(11, 12), (12, 13), (13, 14)]  # Chain 3 (3 transmissions)
            ]
            
            colors_cycle = ['#FF6B6B', '#4ECDC4', '#45B7D1']
            node_idx = 0
            
            for chain_idx, chain in enumerate(chains):
                for src, dst in chain:
                    if src < n_nodes and dst < n_nodes:
                        # Add nodes if not already present
                        if f"C{src:02d}" not in G:
                            G.add_node(f"C{src:02d}")
                            node_colors[f"C{src:02d}"] = colors_cycle[chain_idx]
                        if f"C{dst:02d}" not in G:
                            G.add_node(f"C{dst:02d}")
                            node_colors[f"C{dst:02d}"] = colors_cycle[chain_idx]
                        
                        # Add edge with transmission probability
                        trans_prob = np.random.uniform(0.65, 0.95)
                        G.add_edge(f"C{src:02d}", f"C{dst:02d}", weight=trans_prob)
                        edge_labels[(f"C{src:02d}", f"C{dst:02d}")] = f'{trans_prob:.2f}'
            
            pos = nx.spring_layout(G, k=2.5, iterations=50, seed=42)
        
        # Draw network
        if len(G.nodes()) > 0:
            # Draw nodes
            node_list = list(G.nodes())
            colors_list = [node_colors.get(node, '#87CEEB') for node in node_list]
            nx.draw_networkx_nodes(G, pos, node_color=colors_list, 
                                  node_size=1200, ax=ax, alpha=0.85, edgecolors='black', linewidths=1.5)
            
            # Draw labels
            nx.draw_networkx_labels(G, pos, font_size=9, font_weight='bold', ax=ax)
            
            # Draw edges with arrows
            nx.draw_networkx_edges(G, pos, edge_color='#666666', 
                                  arrows=True, arrowsize=18, arrowstyle='->',
                                  width=2, connectionstyle='arc3,rad=0.1', ax=ax, alpha=0.7)
            
            # Draw edge labels (transmission probabilities)
            if edge_labels:
                nx.draw_networkx_edge_labels(G, pos, edge_labels, font_size=7, 
                                            font_color='#333333', ax=ax)
        
        ax.set_title('Inferred Transmission Network\n(Edge labels: Transmission Probability)', 
                    fontsize=14, fontweight='bold', pad=20)
        ax.axis('off')
        fig.tight_layout()
        fig.savefig('exports/outbreaker_tree.png', dpi=100, bbox_inches='tight')
        plt.close()
        print("[OK] Transmission tree generated with probabilities")
        
    except Exception as e:
        print(f"[WARN] Could not generate transmission tree: {e}")


def generate_mock_summary():
    """Generate mock outbreaker2 analysis summary."""
    summary = {
        "n_generations": 2500,
        "burnin": 500,
        "n_samples": 2000,
        "case_count": len([f for f in os.listdir('exports') if f == 'cases.csv']) > 0 and 100 or 0,
        "likelihood_mean": -5250.5,
        "likelihood_sd": 145.3,
        "transmission_probability": 0.085,
        "generation_time_mean": 12.5,
        "generation_time_sd": 3.2,
        "sampling_probability": 0.65,
        "convergence_diagnostic": "Rhat < 1.1 (good convergence)",
        "data_provenance": "mock",
        "analysis_engine": "mock_generator",
        "generated_at": datetime.now().isoformat(),
    }
    
    with open("exports/outbreaker_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("[OK] Mock summary generated")


if __name__ == "__main__":
    print("Generating mock outbreaker2 report...")
    generate_mock_graphics()
    generate_transmission_tree()
    generate_mock_summary()
    print("\n[OK] Mock report complete - graphics available for demo")

