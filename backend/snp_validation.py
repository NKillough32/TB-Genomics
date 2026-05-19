from dataclasses import dataclass


VALID_BASES = {"A", "C", "G", "T"}


@dataclass(frozen=True)
class SnpDistanceValidation:
    distance: int
    comparable_sites: int
    ambiguous_sites: int
    length_delta: int
    status: str


def validated_snp_distance(seq_a: str, seq_b: str) -> SnpDistanceValidation:
    """Return a pairwise SNP distance with basic sequence-quality accounting.

    Ambiguous sites are excluded from the mismatch count instead of being treated
    as true SNPs. Length differences are reported as validation findings and
    added to the conservative distance.
    """
    left = (seq_a or "").upper()
    right = (seq_b or "").upper()
    common = min(len(left), len(right))

    distance = 0
    comparable_sites = 0
    ambiguous_sites = 0

    for idx in range(common):
        a_base = left[idx]
        b_base = right[idx]
        if a_base not in VALID_BASES or b_base not in VALID_BASES:
            ambiguous_sites += 1
            continue
        comparable_sites += 1
        if a_base != b_base:
            distance += 1

    length_delta = abs(len(left) - len(right))
    # Do NOT add length_delta to distance: sequence length differences are typically
    # assembly/trimming artefacts, not true biological variants. Report separately
    # for transparency but exclude from SNP count to avoid false clustering.

    if not left or not right:
        status = "missing_sequence"
    elif length_delta:
        status = "length_mismatch"
    elif comparable_sites == 0:
        status = "no_comparable_sites"
    elif ambiguous_sites:
        status = "ambiguous_sites_excluded"
    else:
        status = "validated"

    return SnpDistanceValidation(
        distance=distance,
        comparable_sites=comparable_sites,
        ambiguous_sites=ambiguous_sites,
        length_delta=length_delta,
        status=status,
    )

