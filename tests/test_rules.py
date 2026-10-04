import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_contamination_score_depends_on_ratio_and_population(self):
        critical = assess({"concentration": 50, "limit": 10, "population": 10000})
        low = assess({"concentration": 1, "limit": 10, "population": 100})
        self.assertEqual(critical["level"], "critical")
        self.assertEqual(low["level"], "low")
        self.assertGreater(critical["score"], low["score"])

    def test_restore_rejects_failed_sample(self):
        item = {
            "status": "sampled",
            "payload": {
                "limit": 10,
                "sample_results": [{"concentration": 12}],
                "custody": {"zones": [{"zone_id": "Z-3", "state": "detected", "latest": None}]},
            },
        }
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "restore", {"all_zones_cleared": True}, "c", "coordinator")
        self.assertEqual(context.exception.code, "quality_not_met")

    def test_restore_rejects_legacy_event_without_custody(self):
        # 旧事件：有历史结果可查，但没有保管记录 -> 按未采样处理
        item = {
            "status": "sampled",
            "payload": {"limit": 10, "sample_results": [{"concentration": 1}]},
        }
        from src.rules import apply_action, sampling_status
        self.assertEqual(sampling_status(item["payload"]), "unsampled")
        with self.assertRaises(DomainError) as context:
            apply_action(item, "restore", {"all_zones_cleared": True}, "c", "coordinator")
        self.assertEqual(context.exception.code, "custody_missing")

    def test_recompute_zones_uses_latest_effective_receipt(self):
        from src.rules import recompute_custody_zones
        results = [
            {"bottle_id": 1, "bottle_no": "B-1", "zone_id": "Z-1", "receipt_no": "R-1",
             "concentration": 2, "analyzed_at": "2026-09-27T09:00:00+00:00", "created_at": ""},
            {"bottle_id": 2, "bottle_no": "B-2", "zone_id": "Z-1", "receipt_no": "R-2",
             "concentration": 11, "analyzed_at": "2026-09-27T10:00:00+00:00", "created_at": ""},
        ]
        zones = recompute_custody_zones(["Z-1", "Z-2"], results, 10)
        self.assertEqual(zones[0]["state"], "detected")
        self.assertEqual(zones[1]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
