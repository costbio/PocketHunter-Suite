"""Tests for the cluster_labels helper module."""
from cluster_labels import describe_cluster_spatially, parse_residue_tokens


class TestParseResidueTokens:
    def test_single_chain_simple(self):
        assert parse_residue_tokens("A_807 A_810") == {"A": [807, 810]}

    def test_multi_chain(self):
        assert parse_residue_tokens("A_807 B_45 A_810") == {"A": [807, 810], "B": [45]}

    def test_empty_string(self):
        assert parse_residue_tokens("") == {}

    def test_none_value(self):
        assert parse_residue_tokens(None) == {}

    def test_handles_comma_separator(self):
        # CSV exports sometimes use commas
        assert parse_residue_tokens("A_807, A_810, B_45") == {"A": [807, 810], "B": [45]}

    def test_skips_malformed_tokens(self):
        assert parse_residue_tokens("A_807 garbage B_45") == {"A": [807], "B": [45]}

    def test_residues_sorted_within_chain(self):
        assert parse_residue_tokens("A_810 A_805 A_807") == {"A": [805, 807, 810]}

    def test_nan_value(self):
        import math
        assert parse_residue_tokens(float("nan")) == {}


class TestDescribeClusterSpatially:
    def test_single_residue(self):
        assert describe_cluster_spatially("A_807") == "A · 807"

    def test_contiguous_range(self):
        assert describe_cluster_spatially("A_5 A_6 A_7 A_8") == "A · 5–8"

    def test_non_contiguous_same_chain(self):
        assert describe_cluster_spatially("A_5 A_6 A_10") == "A · 5–6, 10"

    def test_multi_chain(self):
        assert describe_cluster_spatially("A_5 A_6 B_45") == "A · 5–6 | B · 45"

    def test_empty(self):
        assert describe_cluster_spatially("") == "—"

    def test_none(self):
        assert describe_cluster_spatially(None) == "—"

    def test_unordered_input(self):
        assert describe_cluster_spatially("B_45 A_5 A_6") == "A · 5–6 | B · 45"

    def test_truncates_with_many_chains(self):
        # If many chains, output should still be readable.
        residues = " ".join(f"{c}_1" for c in "ABCDEFGH")
        result = describe_cluster_spatially(residues)
        assert "A · 1" in result
        assert "B · 1" in result
        # Should fit in a single line without being absurdly long.
        assert len(result) < 200

    def test_long_contiguous_range(self):
        residues = " ".join(f"A_{n}" for n in range(100, 121))
        assert describe_cluster_spatially(residues) == "A · 100–120"
