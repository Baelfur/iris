"""Tests for core.pool_sizing.

Pure-function math + log-rendering. The variant-side ``get_connection_limit``
probes are exercised against real DBs in the integration suite.
"""

from core.engine.pool_sizing import (
    _verdict,
    compute_report,
    format_report_for_log,
)


class TestVerdict:
    def test_low_utilization_is_ok(self):
        assert _verdict(50) == "ok"
        assert _verdict(70) == "ok"

    def test_mid_utilization_is_caution(self):
        assert _verdict(71) == "caution"
        assert _verdict(90) == "caution"

    def test_high_utilization_is_unsafe(self):
        assert _verdict(91) == "unsafe"
        assert _verdict(100) == "unsafe"
        assert _verdict(150) == "unsafe"


class TestComputeReport:
    def test_known_db_max_produces_full_report(self):
        report = compute_report(
            db_max=151, db_max_source="MySQL @@max_connections",
            pool_min=2, pool_max=10, hpa_max=10,
        )
        assert report["db_max_connections"] == 151
        assert report["pool"] == {"min": 2, "max": 10}
        assert report["expected_replica_peak"] == 10
        assert report["total_at_peak"] == 100
        assert report["capacity_used_pct"] == 66
        assert report["recommendations"]
        # Current pool_max=10 should appear in recommendations.
        assert any(r["pool_max"] == 10 for r in report["recommendations"])

    def test_unknown_db_max_no_recommendations(self):
        report = compute_report(
            db_max=None, db_max_source="Trino has no pool concept",
            pool_min=2, pool_max=10, hpa_max=10,
        )
        assert report["db_max_connections"] is None
        assert report["capacity_used_pct"] is None
        assert report["recommendations"] == []
        assert report["total_at_peak"] == 100  # math still works

    def test_recommendation_verdict_progression(self):
        """Higher pool_max → higher used_pct → progressively worse verdict."""
        report = compute_report(
            db_max=100, db_max_source="test",
            pool_min=2, pool_max=8, hpa_max=10,
        )
        recs = {r["pool_max"]: r for r in report["recommendations"]}
        # 8 × 10 = 80 → ok-or-caution boundary
        # 10 × 10 = 100 → 100% → unsafe
        # 13 × 10 = 130 → unsafe
        assert recs[8]["used_pct"] == 80
        assert recs[10]["used_pct"] == 100
        assert recs[10]["verdict"] == "unsafe"
        assert recs[13]["used_pct"] == 130

    def test_zero_db_max_falls_back_to_unknown(self):
        """db_max=0 is treated as unknown — division-by-zero guard."""
        report = compute_report(
            db_max=0, db_max_source="weird",
            pool_min=2, pool_max=10, hpa_max=10,
        )
        # compute_report uses a falsy check, so 0 → no recommendations.
        assert report["recommendations"] == []


class TestFormatReportForLog:
    def test_known_limit_renders_recommendations(self):
        report = compute_report(
            db_max=151, db_max_source="MySQL @@max_connections",
            pool_min=2, pool_max=10, hpa_max=10,
        )
        text = format_report_for_log(report)
        assert "Pool sizing report" in text
        assert "151" in text
        assert "MySQL @@max_connections" in text
        assert "min=2, max=10" in text
        assert "100 connections" in text
        assert "66% of DB capacity" in text
        assert "← current" in text  # marks the configured pool_max line

    def test_unknown_limit_says_cannot_advise(self):
        report = compute_report(
            db_max=None, db_max_source="Trino has no pool concept",
            pool_min=2, pool_max=10, hpa_max=10,
        )
        text = format_report_for_log(report)
        assert "unknown" in text
        assert "Trino has no pool concept" in text
        assert "cannot advise" in text
        # Doesn't print spurious "X% of DB capacity" when no DB max.
        assert "DB capacity" not in text
