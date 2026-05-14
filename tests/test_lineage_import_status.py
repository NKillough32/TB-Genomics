from scripts.run_lineage_dr_validation import _import_status


def test_import_status_does_not_report_success_when_all_rows_skipped():
    status, message = _import_status("tb-profiler", imported=0, skipped=10)

    assert status == "no_matching_cases"
    assert "none matched cases" in message


def test_import_status_reports_partial_imports_as_warnings():
    status, message = _import_status("mykrobe", imported=8, skipped=2)

    assert status == "imported_with_warnings"
    assert "skipped unlinked samples" in message
