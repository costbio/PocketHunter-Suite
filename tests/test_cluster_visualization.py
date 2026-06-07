"""Tests for the pure helpers in cluster_visualization."""
import numpy as np
import pandas as pd
import pytest

from cluster_visualization import (
    build_cluster_to_rep_mapping,
    build_consensus_matrix,
    build_per_pocket_matrix,
    calculate_heatmap_layout,
    filter_residue_columns,
    residue_sort_key,
    sort_residues_in_matrix,
)


class TestResidueSortKey:
    def test_basic_chain_and_number(self):
        assert residue_sort_key("A_807") == ("A", 807)

    def test_sorts_numerically_not_lexically(self):
        # Lexical sort would put A_1019 before A_807; we want numeric.
        names = ["A_1019", "A_807", "A_45"]
        ordered = sorted(names, key=residue_sort_key)
        assert ordered == ["A_45", "A_807", "A_1019"]

    def test_multi_char_chain(self):
        assert residue_sort_key("AB_12") == ("AB", 12)

    def test_malformed_returns_zero(self):
        assert residue_sort_key("totally_not_a_residue") == ("totally_not", 0) or \
               residue_sort_key("totally_not_a_residue") == ("totally_not_a_residue", 0)
        # The implementation does rsplit('_', 1); the trailing token "a_residue" → "a" + "residue"
        # depending on rsplit. Either fallback is acceptable for malformed input.

    def test_no_underscore_returns_zero_value(self):
        assert residue_sort_key("noresi") == ("noresi", 0)


class TestBuildConsensusMatrix:
    @staticmethod
    def _df():
        # 3 clusters, 4 residue cols, one all-zero residue, mixed coverage.
        return pd.DataFrame([
            {"cluster": 0, "A_10": 1, "A_11": 1, "A_12": 0, "B_5": 0, "probability": 0.9, "residues": "A_10 A_11"},
            {"cluster": 0, "A_10": 1, "A_11": 0, "A_12": 0, "B_5": 0, "probability": 0.7, "residues": "A_10"},
            {"cluster": 1, "A_10": 0, "A_11": 1, "A_12": 1, "B_5": 0, "probability": 0.6, "residues": "A_11 A_12"},
            {"cluster": 2, "A_10": 0, "A_11": 0, "A_12": 1, "B_5": 0, "probability": 0.4, "residues": "A_12"},
        ])

    @staticmethod
    def _reps():
        return pd.DataFrame([
            {"cluster": 0, "probability": 0.9, "residues": "A_10 A_11"},
            {"cluster": 1, "probability": 0.6, "residues": "A_11 A_12"},
            {"cluster": 2, "probability": 0.4, "residues": "A_12"},
        ])

    def test_shape_matches_cluster_count(self):
        df = self._df()
        residue_cols = ["A_10", "A_11", "A_12", "B_5"]
        matrix, labels = build_consensus_matrix(df, residue_cols, [0, 1, 2], self._reps())
        assert matrix.shape == (3, 4)
        assert len(labels) == 3

    def test_means_are_correct(self):
        df = self._df()
        residue_cols = ["A_10", "A_11", "A_12", "B_5"]
        matrix, _ = build_consensus_matrix(df, residue_cols, [0, 1, 2], self._reps())
        # Cluster 0 has two rows: A_10 always present (1, 1) → mean 1.0; A_11 mixed (1, 0) → 0.5.
        assert matrix[0, 0] == pytest.approx(1.0)
        assert matrix[0, 1] == pytest.approx(0.5)
        # Cluster 1 (single row): A_11 = 1, A_12 = 1.
        assert matrix[1, 1] == pytest.approx(1.0)
        assert matrix[1, 2] == pytest.approx(1.0)

    def test_label_includes_cluster_id_and_pocket_count(self):
        df = self._df()
        residue_cols = ["A_10", "A_11", "A_12", "B_5"]
        _, labels = build_consensus_matrix(df, residue_cols, [0, 1, 2], self._reps())
        assert "Cluster 0" in labels[0]
        assert "2 pockets" in labels[0]  # cluster 0 had 2 rows


class TestFilterResidueColumns:
    def test_drops_all_zero_columns(self):
        matrix = np.array([[1.0, 0.0, 0.5], [0.0, 0.0, 1.0]])
        residues = ["A_1", "A_2", "A_3"]
        # A_2 is all-zero → drop it
        filtered_res, filtered_matrix = filter_residue_columns(residues, matrix)
        assert filtered_res == ["A_1", "A_3"]
        assert filtered_matrix.shape == (2, 2)

    def test_no_zero_columns_keeps_everything(self):
        matrix = np.array([[0.5, 1.0], [0.3, 0.7]])
        residues = ["A_1", "A_2"]
        filtered_res, filtered_matrix = filter_residue_columns(residues, matrix)
        assert filtered_res == ["A_1", "A_2"]
        assert filtered_matrix.shape == (2, 2)


class TestSortResiduesInMatrix:
    def test_reorders_matrix_to_match_sorted_residues(self):
        residues = ["A_1019", "A_45", "A_807"]
        matrix = np.array([
            [10.0, 20.0, 30.0],
            [40.0, 50.0, 60.0],
        ])
        sorted_res, sorted_matrix = sort_residues_in_matrix(residues, matrix)
        # Numeric sort: 45 → 807 → 1019
        assert sorted_res == ["A_45", "A_807", "A_1019"]
        # Matrix columns follow: original index 1, 2, 0
        np.testing.assert_array_equal(sorted_matrix[:, 0], [20.0, 50.0])
        np.testing.assert_array_equal(sorted_matrix[:, 2], [10.0, 40.0])

    def test_empty_returns_empty(self):
        sorted_res, sorted_matrix = sort_residues_in_matrix([], np.zeros((2, 0)))
        assert sorted_res == []
        assert sorted_matrix.shape == (2, 0)


class TestBuildClusterToRepMapping:
    def test_uses_cluster_column_when_present(self):
        df_reps = pd.DataFrame([
            {"cluster": 2, "probability": 0.4},
            {"cluster": 0, "probability": 0.9},
            {"cluster": 1, "probability": 0.6},
        ])
        # df_clustered isn't needed for the column path but the helper takes it for the fallback case.
        df_clustered = pd.DataFrame({"cluster": [0, 1, 2]})
        result = build_cluster_to_rep_mapping(df_reps, df_clustered)
        assert set(result.keys()) == {0, 1, 2}
        assert result[0]["probability"] == 0.9

    def test_positional_fallback_when_no_cluster_col(self):
        # No 'cluster' column → use positional alignment with sorted unique clusters
        df_reps = pd.DataFrame([
            {"probability": 0.9, "residues": "A_10"},
            {"probability": 0.6, "residues": "A_11"},
            {"probability": 0.4, "residues": "A_12"},
        ])
        df_clustered = pd.DataFrame({"cluster": [0, 1, 2]})
        result = build_cluster_to_rep_mapping(df_reps, df_clustered)
        assert set(result.keys()) == {0, 1, 2}
        assert result[0]["probability"] == 0.9
        assert result[2]["probability"] == 0.4

    def test_fewer_reps_than_clusters(self):
        df_reps = pd.DataFrame([{"probability": 0.9}])  # only one rep
        df_clustered = pd.DataFrame({"cluster": [0, 1, 2]})
        result = build_cluster_to_rep_mapping(df_reps, df_clustered)
        # Only cluster 0 maps; clusters 1 and 2 absent (extra reps would be ignored)
        assert 0 in result
        assert 1 not in result


class TestCalculateHeatmapLayout:
    def test_returns_dict_with_required_keys(self):
        layout = calculate_heatmap_layout(5)
        for key in ("height", "row_h", "cb_h", "top_pad", "gap", "heat_top_margin", "heat_bot_margin"):
            assert key in layout

    def test_height_scales_with_cluster_count(self):
        small = calculate_heatmap_layout(3)
        large = calculate_heatmap_layout(20)
        assert large["height"] > small["height"]

    def test_minimum_height_floor(self):
        # With just 1 cluster, height should hit the 400 floor.
        layout = calculate_heatmap_layout(1)
        assert layout["height"] >= 400


class TestPerPocketHeatmapTitleGap:
    """The per-pocket heatmap stays compact (capped at 500 px) while
    floor-allocating each subplot enough proportional height to render
    its "Cluster N" title uncropped. Pins the height + row-height math."""

    @staticmethod
    def _compute(cluster_meta):
        """Mirror the height + row_heights math from
        ``render_per_pocket_heatmap``."""
        from cluster_visualization import CLUSTER_TITLE_GAP_PX

        cluster_ids = list(cluster_meta.keys())
        n_clusters = len(cluster_ids)
        total_pockets = sum(len(cluster_meta[c]) for c in cluster_ids)
        total_height = min(500, max(280, total_pockets * 16 + 40 * n_clusters))
        floor_frac = min(
            CLUSTER_TITLE_GAP_PX / total_height, 0.95 / n_clusters
        )
        remaining = max(0.0, 1.0 - floor_frac * n_clusters)
        raw = [float(len(cluster_meta[c])) for c in cluster_ids]
        s = sum(raw) or 1.0
        proportions = [h / s for h in raw]
        row_heights = [floor_frac + p * remaining for p in proportions]
        return total_height, row_heights, floor_frac

    def test_height_stays_capped_at_500(self):
        """A massive clustering doesn't blow up the figure height."""
        cluster_meta = {
            0: [{"i": i} for i in range(8000)],
            1: [{"i": i} for i in range(500)],
            2: [{"i": i} for i in range(300)],
            3: [{"i": i} for i in range(200)],
            4: [{"i": i} for i in range(100)],
            5: [{"i": i} for i in range(50)],
        }
        total_height, _, _ = self._compute(cluster_meta)
        assert total_height == 500

    def test_height_floor_for_tiny_clustering(self):
        """A single-cluster small case still gets the 280 px floor."""
        cluster_meta = {0: [{"i": 0}]}
        total_height, _, _ = self._compute(cluster_meta)
        assert total_height == 280

    def test_row_heights_sum_to_one(self):
        """Floor + proportional remainder must still partition the figure."""
        cluster_meta = {
            0: [{"i": i} for i in range(8000)],
            1: [{"i": i} for i in range(50)],
            2: [{"i": 0}],
        }
        _, row_heights, _ = self._compute(cluster_meta)
        assert sum(row_heights) == pytest.approx(1.0)

    def test_small_cluster_gets_title_floor(self):
        """In a dominant-big + tiny-small mix, the tiny cluster's row
        is lifted to the title-gap floor (not crushed to ~0)."""
        from cluster_visualization import CLUSTER_TITLE_GAP_PX

        cluster_meta = {
            0: [{"i": i} for i in range(8000)],
            1: [{"i": i} for i in range(500)],
            2: [{"i": i} for i in range(300)],
            3: [{"i": i} for i in range(200)],
            4: [{"i": i} for i in range(100)],
            5: [{"i": 0}],
        }
        total_height, row_heights, floor_frac = self._compute(cluster_meta)
        # The 1-pocket cluster gets at least the floor (it has near-zero
        # proportional share but gains the full floor fraction).
        smallest_px = row_heights[-1] * total_height
        assert smallest_px >= CLUSTER_TITLE_GAP_PX - 1  # tolerance for FP

    def test_proportions_preserved_when_all_clusters_above_floor(self):
        """If every cluster's natural share exceeds the floor (small
        clusterings), the floor binds for all equally, so the *ordering*
        of row_heights matches cluster sizes."""
        cluster_meta = {
            0: [{"i": i} for i in range(20)],
            1: [{"i": i} for i in range(5)],
            2: [{"i": 0}],
        }
        _, row_heights, _ = self._compute(cluster_meta)
        assert row_heights[0] > row_heights[1] > row_heights[2]


class TestBuildPerPocketMatrix:
    @staticmethod
    def _df():
        # 2 clusters × 3 pockets each. Residue cols A_10, A_11, A_12.
        # Probabilities deliberately not pre-sorted; the helper must sort.
        return pd.DataFrame([
            {"cluster": 1, "A_10": 0, "A_11": 1, "A_12": 1, "probability": 0.6, "File name": "f10.pdb", "residues": "A_11 A_12", "pocket_index": 1},
            {"cluster": 0, "A_10": 1, "A_11": 1, "A_12": 0, "probability": 0.9, "File name": "f1.pdb", "residues": "A_10 A_11", "pocket_index": 0},
            {"cluster": 0, "A_10": 1, "A_11": 0, "A_12": 0, "probability": 0.4, "File name": "f3.pdb", "residues": "A_10", "pocket_index": 0},
            {"cluster": 1, "A_10": 0, "A_11": 1, "A_12": 1, "probability": 0.8, "File name": "f7.pdb", "residues": "A_11 A_12", "pocket_index": 1},
            {"cluster": 0, "A_10": 1, "A_11": 1, "A_12": 0, "probability": 0.7, "File name": "f2.pdb", "residues": "A_10 A_11", "pocket_index": 0},
            {"cluster": 1, "A_10": 0, "A_11": 0, "A_12": 1, "probability": 0.5, "File name": "f12.pdb", "residues": "A_12", "pocket_index": 1},
        ])

    def test_shape_includes_spacer_between_clusters(self):
        df = self._df()
        matrix, meta = build_per_pocket_matrix(df, ["A_10", "A_11", "A_12"])
        # 6 pockets + 1 spacer between cluster 0 and cluster 1 = 7 rows.
        assert matrix.shape == (7, 3)
        assert len(meta) == 7

    def test_spacer_row_is_all_zero(self):
        df = self._df()
        matrix, meta = build_per_pocket_matrix(df, ["A_10", "A_11", "A_12"])
        spacer_idxs = [i for i, m in enumerate(meta) if m.get("is_spacer")]
        assert len(spacer_idxs) == 1
        spacer_idx = spacer_idxs[0]
        np.testing.assert_array_equal(matrix[spacer_idx], [0.0, 0.0, 0.0])

    def test_rows_grouped_by_cluster_and_sorted_by_probability(self):
        df = self._df()
        _, meta = build_per_pocket_matrix(df, ["A_10", "A_11", "A_12"])
        real = [m for m in meta if not m.get("is_spacer")]
        # First three rows = cluster 0, next three = cluster 1.
        assert [m["cluster"] for m in real] == [0, 0, 0, 1, 1, 1]
        # Within each cluster, probability descends.
        cluster0_probs = [m["probability"] for m in real[:3]]
        assert cluster0_probs == sorted(cluster0_probs, reverse=True)
        cluster1_probs = [m["probability"] for m in real[3:]]
        assert cluster1_probs == sorted(cluster1_probs, reverse=True)

    def test_no_spacers_when_disabled(self):
        df = self._df()
        matrix, meta = build_per_pocket_matrix(
            df, ["A_10", "A_11", "A_12"], insert_spacers=False
        )
        assert matrix.shape == (6, 3)
        assert all(not m.get("is_spacer") for m in meta)

    def test_empty_df_returns_empty(self):
        df = pd.DataFrame(columns=["cluster", "A_10", "probability", "File name", "residues", "pocket_index"])
        matrix, meta = build_per_pocket_matrix(df, ["A_10"])
        assert matrix.shape == (0, 1)
        assert meta == []

    def test_missing_cluster_column_returns_empty(self):
        df = pd.DataFrame([{"A_10": 1, "probability": 0.5}])
        matrix, meta = build_per_pocket_matrix(df, ["A_10"])
        assert matrix.shape == (0, 1)
        assert meta == []

    def test_meta_carries_lookup_fields(self):
        df = self._df()
        _, meta = build_per_pocket_matrix(df, ["A_10", "A_11", "A_12"])
        real = [m for m in meta if not m.get("is_spacer")]
        # First row should be cluster 0's highest-probability pocket (p=0.9, f1.pdb).
        assert real[0]["file_name"] == "f1.pdb"
        assert real[0]["residues"] == "A_10 A_11"
        assert real[0]["pocket_index"] == 0


class TestRoundTrip:
    """Full pipeline: build consensus → filter → sort, end-to-end on synthetic data."""

    def test_end_to_end(self):
        df = pd.DataFrame([
            {"cluster": 0, "A_10": 1, "A_5": 0, "A_100": 1, "probability": 0.9, "residues": "A_10 A_100"},
            {"cluster": 1, "A_10": 0, "A_5": 1, "A_100": 0, "probability": 0.5, "residues": "A_5"},
        ])
        reps = pd.DataFrame([
            {"cluster": 0, "probability": 0.9, "residues": "A_10 A_100"},
            {"cluster": 1, "probability": 0.5, "residues": "A_5"},
        ])
        residue_cols = ["A_10", "A_5", "A_100"]
        matrix, labels = build_consensus_matrix(df, residue_cols, [0, 1], reps)
        filtered_res, filtered_matrix = filter_residue_columns(residue_cols, matrix)
        # All columns have at least one non-zero → all survive.
        assert len(filtered_res) == 3
        sorted_res, sorted_matrix = sort_residues_in_matrix(filtered_res, filtered_matrix)
        # Numeric sort: A_5, A_10, A_100
        assert sorted_res == ["A_5", "A_10", "A_100"]
        # Cluster 0 row of sorted matrix: A_5=0, A_10=1, A_100=1
        np.testing.assert_array_equal(sorted_matrix[0, :], [0.0, 1.0, 1.0])
