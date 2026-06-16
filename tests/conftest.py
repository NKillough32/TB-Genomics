import pytest

INTEGRATION_TEST_FILES = {
    "test_fasta_analysis.py",
    "test_ingest_hardening.py",
    "test_lineage_dr_tool_extraction.py",
    "test_lineage_import_status.py",
    "test_run_transmission_synthesis_script.py",
    "test_static_assets.py",
    "test_tb_wgs_pipeline.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = pytest.mark.integration if item.path.name in INTEGRATION_TEST_FILES else pytest.mark.unit
        item.add_marker(marker)
