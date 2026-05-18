#!/usr/bin/env python3
"""
PoC Validation Script for TB Genomic Surveillance Platform
Runs a complete end-to-end test and generates a report.
"""

import json
import requests
import time
import sys
from datetime import datetime
from pathlib import Path

API_BASE = "http://localhost:8000"
TEST_CASE_COUNT = 100
DEMO_TIMEOUT = 300  # 5 minutes max for all jobs

class PoC_Validator:
    def __init__(self):
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "tests": [],
            "summary": {"passed": 0, "failed": 0, "total": 0},
        }
        self.job_ids = []

    def test(self, name: str, func, expected_status: int = 200):
        """Run a single test."""
        try:
            start = time.time()
            response = func()
            elapsed = time.time() - start
            
            if response.status_code == expected_status:
                self.results["tests"].append({
                    "name": name,
                    "status": "PASS",
                    "elapsed_ms": int(elapsed * 1000),
                })
                self.results["summary"]["passed"] += 1
                print(f"[OK] {name} ({elapsed:.2f}s)")
                return response
            else:
                self.results["tests"].append({
                    "name": name,
                    "status": "FAIL",
                    "expected": expected_status,
                    "got": response.status_code,
                })
                self.results["summary"]["failed"] += 1
                print(f"X {name} (expected {expected_status}, got {response.status_code})")
                return None
        except Exception as e:
            self.results["tests"].append({
                "name": name,
                "status": "ERROR",
                "error": str(e),
            })
            self.results["summary"]["failed"] += 1
            print(f"X {name} (error: {e})")
            return None
        finally:
            self.results["summary"]["total"] += 1

    def run_validation(self):
        """Execute the full PoC validation."""
        print("=" * 60)
        print("TB Genomic Surveillance - PoC Validation")
        print("=" * 60)

        # Step 1: Check backend health
        print("\n[1/6] Backend Health Check")
        resp = self.test(
            "Backend is running",
            lambda: requests.get(f"{API_BASE}/"),
        )
        if not resp:
            print("[X] Backend unreachable. Start backend with: python -m uvicorn backend.app:app --host 0.0.0.0")
            return False

        # Step 2: Seed synthetic data
        print("\n[2/6] Synthetic Data Generation")
        resp = self.test(
            f"Generate {TEST_CASE_COUNT} synthetic cases",
            lambda: requests.post(
                f"{API_BASE}/ingest/seed-synthetic",
                params={"case_count": TEST_CASE_COUNT, "reset": True, "seed": 42},
            ),
        )
        if resp:
            data = resp.json()
            print(f"   -> {data['cases_inserted']} cases inserted")
            print(f"   -> {data['clusters_inserted']} clusters created")

        # Step 3: Check case listing
        print("\n[3/6] Data Retrieval")
        resp = self.test(
            "List cases via API",
            lambda: requests.get(f"{API_BASE}/cases/"),
        )
        if resp:
            cases = resp.json()
            print(f"   -> Retrieved {len(cases)} cases")

        # Step 4: Check case summary
        print("\n[4/6] Analysis Summary")
        resp = self.test(
            "Get case summary",
            lambda: requests.get(f"{API_BASE}/cases/summary"),
        )
        if resp:
            summary = resp.json()
            print(f"   -> Total cases: {summary['total_cases']}")
            print(f"   -> Clustered: {summary['clustered_cases']}")
            print(f"   -> Unclustered: {summary['unclustered_cases']}")
            print(f"   -> Open clusters: {summary['open_clusters']}")

        # Step 5: Run clustering job
        print("\n[5/6] Workflow Execution")
        resp = self.test(
            "Start clustering job",
            lambda: requests.post(f"{API_BASE}/jobs/run/run_clustering"),
        )
        if resp:
            job_data = resp.json()
            if "job_id" in job_data:
                job_id = job_data["job_id"]
                self.job_ids.append(job_id)
                print(f"   -> Job ID: {job_id}")
                
                # Poll job status
                start = time.time()
                while time.time() - start < DEMO_TIMEOUT:
                    status_resp = requests.get(f"{API_BASE}/jobs/status/{job_id}")
                    if status_resp.status_code == 200:
                        job_status = status_resp.json()
                        if job_status["status"] == "completed":
                            print(f"   -> Clustering completed")
                            break
                        elif job_status["status"] == "failed":
                            print(f"   -> Clustering failed")
                            break
                    time.sleep(1)

        # Step 6: Governance audit trail
        print("\n[6/6] Governance & Compliance")
        resp = self.test(
            "Retrieve audit trail",
            lambda: requests.get(f"{API_BASE}/cases/audit-trail?limit=10"),
        )
        if resp:
            audit = resp.json()
            print(f"   -> {audit['total_entries']} audit entries found")
            for entry in audit["entries"][:3]:
                print(f"      - {entry['timestamp']}: {entry['action']} (user: {entry['user']})")

        return True

    def generate_report(self):
        """Generate and save validation report."""
        success_rate = (
            (self.results["summary"]["passed"] / self.results["summary"]["total"] * 100)
            if self.results["summary"]["total"] > 0
            else 0
        )
        
        print("\n" + "=" * 60)
        print("VALIDATION SUMMARY")
        print("=" * 60)
        print(f"Passed:  {self.results['summary']['passed']}/{self.results['summary']['total']}")
        print(f"Failed:  {self.results['summary']['failed']}/{self.results['summary']['total']}")
        print(f"Success: {success_rate:.1f}%")
        print("=" * 60)

        # Save report to file
        report_path = Path("exports/poc_validation_report.json")
        with open(report_path, "w") as f:
            json.dump(self.results, f, indent=2)
        print(f"\n[OK] Report saved to {report_path}")

        return self.results["summary"]["failed"] == 0


def main():
    validator = PoC_Validator()
    
    try:
        success = validator.run_validation()
        if success:
            validator.generate_report()
            sys.exit(0)
        else:
            validator.generate_report()
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n[X] Validation interrupted by user")
        sys.exit(1)


if __name__ == "__main__":
    main()

