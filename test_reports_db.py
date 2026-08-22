import asyncio
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

import bot.reports.db as db  # noqa: E402


def run(coro):
    return asyncio.run(coro)


SAMPLE_ROW = {
    "id": 1,
    "reporter_id": "111",
    "reporter_username": "reporter",
    "target_id": "222",
    "target_username": "target",
    "categories": ["scam", "phishing"],
    "note": "test note",
    "status": "pending",
    "maintainer_channel_message_id": None,
    "rejection_reason": None,
    "created_at": "2026-08-22T00:00:00+00:00",
    "decided_at": None,
}


class TestReportsDb(unittest.TestCase):
    def test_insert_pending_report_passes_through_args(self):
        with patch("database.insert_pending_report", return_value=42) as mock_insert:
            report_id = run(
                db.insert_pending_report(
                    reporter_id="111",
                    reporter_username="reporter",
                    target_id="222",
                    target_username="target",
                    categories=["scam"],
                    note="hello",
                )
            )
        self.assertEqual(report_id, 42)
        mock_insert.assert_called_once_with("111", "reporter", "222", "target", ["scam"], "hello")

    def test_set_maintainer_message_id(self):
        with patch("database.set_report_maintainer_message_id") as mock_set:
            run(db.set_maintainer_message_id(1, "999"))
        mock_set.assert_called_once_with(1, "999")

    def test_get_report_maps_row_to_dataclass(self):
        with patch("database.get_report", return_value=SAMPLE_ROW):
            report = run(db.get_report(1))
        self.assertIsInstance(report, db.Report)
        self.assertEqual(report.reporter_id, "111")
        self.assertEqual(report.categories, ["scam", "phishing"])

    def test_get_report_returns_none_when_missing(self):
        with patch("database.get_report", return_value=None):
            report = run(db.get_report(999))
        self.assertIsNone(report)

    def test_mark_merged_returns_report(self):
        with patch("database.mark_report_merged", return_value={**SAMPLE_ROW, "status": "merged"}):
            report = run(db.mark_merged(1))
        self.assertEqual(report.status, "merged")

    def test_mark_rejected_returns_report_with_reason(self):
        with patch(
            "database.mark_report_rejected",
            return_value={**SAMPLE_ROW, "status": "rejected", "rejection_reason": "spam"},
        ) as mock_reject:
            report = run(db.mark_rejected(1, "spam"))
        self.assertEqual(report.status, "rejected")
        self.assertEqual(report.rejection_reason, "spam")
        mock_reject.assert_called_once_with(1, "spam")


if __name__ == "__main__":
    unittest.main()
