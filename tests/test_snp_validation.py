from backend.snp_validation import validated_snp_distance


def test_validated_snp_distance_excludes_ambiguous_sites():
    result = validated_snp_distance("ACNT", "AGGT")

    assert result.distance == 1
    assert result.comparable_sites == 3
    assert result.ambiguous_sites == 1
    assert result.status == "ambiguous_sites_excluded"


def test_validated_snp_distance_reports_length_delta():
    result = validated_snp_distance("ACGT", "ACGTA")

    assert result.distance == 1
    assert result.length_delta == 1
    assert result.status == "length_mismatch"

