#!/usr/bin/env python3
"""
Generate phylogenetic tree and resistance profile heatmap from TB cases
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
from scipy.cluster.hierarchy import dendrogram, linkage
from scipy.spatial.distance import pdist
from sqlalchemy import text
from backend.database import SessionLocal

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
                LIMIT 100
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
            
            for case in cases_data[:50]:  # Limit to 50 cases
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
            fig, ax = plt.subplots(figsize=(10, 14))
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
    print("Generating phylogenetic and resistance visualizations...")
    generate_phylogenetic_tree()
    generate_resistance_heatmap()
