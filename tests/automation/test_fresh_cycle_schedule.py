from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kbo_card_news.automation.cli import _fresh_cycle_schedule_decision


KST = timezone(timedelta(hours=9))


class FreshCycleScheduleTest(unittest.TestCase):
    def test_skips_between_midnight_and_seven(self) -> None:
        decision = _fresh_cycle_schedule_decision(now=datetime(2026, 5, 11, 3, 30, tzinfo=KST))

        self.assertEqual(decision["mode"], "skip")
        self.assertEqual(decision["reason"], "quiet_hours_00_to_07")

    def test_runs_morning_catchup_once_in_seven_hour(self) -> None:
        marker_path = Path(tempfile.mkdtemp()) / "morning.done"
        with patch("kbo_card_news.automation.cli._fresh_morning_catchup_marker_path", return_value=marker_path):
            decision = _fresh_cycle_schedule_decision(now=datetime(2026, 5, 11, 7, 25, tzinfo=KST))

        self.assertEqual(decision["mode"], "morning_catchup")
        self.assertEqual(decision["analysis_now"], "2026-05-11T07:00:00+09:00")
        self.assertEqual(decision["collection_window_start"], "2026-05-11T00:00:00+09:00")
        self.assertEqual(decision["collection_window_end"], "2026-05-11T07:00:00+09:00")

    def test_after_morning_marker_seven_hour_uses_normal_cycle(self) -> None:
        marker_path = Path(tempfile.mkdtemp()) / "morning.done"
        marker_path.write_text("done", encoding="utf-8")
        with patch("kbo_card_news.automation.cli._fresh_morning_catchup_marker_path", return_value=marker_path):
            decision = _fresh_cycle_schedule_decision(now=datetime(2026, 5, 11, 7, 30, tzinfo=KST))

        self.assertEqual(decision["mode"], "normal")

    def test_after_seven_uses_normal_cycle(self) -> None:
        decision = _fresh_cycle_schedule_decision(now=datetime(2026, 5, 11, 8, 0, tzinfo=KST))

        self.assertEqual(decision["mode"], "normal")


if __name__ == "__main__":
    unittest.main()
