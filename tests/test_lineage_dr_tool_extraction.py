import json


def test_tbprofiler_extraction_preserves_clinical_summary_and_mutations(tmp_path):
    from scripts.run_lineage_dr_validation import _extract_lineage_and_resistance

    result_path = tmp_path / "sample.results.json"
    result_path.write_text(
        json.dumps(
            {
                "main_lineage": "lineage4",
                "sub_lineage": "lineage4.9",
                "drtype": "MDR-TB",
                "db_version": {"name": "WHO_catalogue_v2"},
                "dr_variants": [{"drug": "rifampicin", "gene": "rpoB", "change": "S450L"}],
            }
        ),
        encoding="utf-8",
    )

    fields = _extract_lineage_and_resistance(result_path)

    assert fields["lineage"] == "L4"
    assert fields["sublineage"] == "L4.9"
    assert fields["resistance_mutations"] == [{"drug": "rifampicin", "gene": "rpoB", "change": "S450L"}]
    assert fields["predicted_drug_resistance"]["classification"] == "MDR-TB"
    assert fields["predicted_drug_resistance"]["catalogue"] == "WHO_catalogue_v2"
    assert fields["predicted_drug_resistance"]["resistant_drugs"] == ["rifampicin"]
    assert "MDR-TB" in fields["interpretation_summary"]


def test_tbprofiler_extraction_preserves_sublineage_when_only_lineage_field_has_detail(tmp_path):
    from scripts.run_lineage_dr_validation import _extract_lineage_and_resistance

    result_path = tmp_path / "sample_sublineage.results.json"
    result_path.write_text(
        json.dumps(
            {
                "main_lineage": "lineage4.1.2.1",
            }
        ),
        encoding="utf-8",
    )

    fields = _extract_lineage_and_resistance(result_path)

    assert fields["lineage"] == "L4"
    assert fields["sublineage"] == "L4.1.2.1"


def test_normalise_tb_lineage_maps_common_aliases():
    from scripts.run_lineage_dr_validation import normalise_tb_lineage

    assert normalise_tb_lineage("lineage4") == "L4"
    assert normalise_tb_lineage("Lineage 4") == "L4"
    assert normalise_tb_lineage("4") == "L4"
    assert normalise_tb_lineage("L4") == "L4"
    assert normalise_tb_lineage("L4.1") == "L4.1"
    assert normalise_tb_lineage(None) is None
    assert normalise_tb_lineage("  ") is None


def test_dr_normalisation_keeps_no_call_category():
    from scripts.run_lineage_dr_validation import _normalise_dr_to_rs

    calls = _normalise_dr_to_rs(
        {
            "rifampicin": {"predict": "R"},
            "isoniazid": {"predict": "N"},
            "ethambutol": {"predict": "S"},
        }
    )

    assert calls == {"rifampicin": "R", "isoniazid": "N", "ethambutol": "S"}
