import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fimbox_api import archive_reader, historical_streamflow, retrospective


class HistoricalStreamflowTests(unittest.TestCase):
    def test_lookup_spans_the_cutoff_and_downloads_once_per_archive_hour(self):
        start = retrospective.LAST_TIME
        end = start + timedelta(hours=2)
        archived = []

        def read_archive(hour, reach_ids, deadline, stats):
            archived.append((hour, reach_ids))
            return {"7": 2.5, "8": None}

        retrospective_rows = [{
            "time": start,
            "flows_cms": {"7": 1.5, "8": 0.0},
        }]
        with patch.object(retrospective, "streamflows", return_value=retrospective_rows) as read_retro, \
             patch.object(archive_reader, "read_hour", side_effect=read_archive):
            result = historical_streamflow.lookup(
                ["7", "8"], start, end,
            )

        self.assertEqual(
            [sample["time"] for sample in result["samples"]],
            [start, start + timedelta(hours=1), end],
        )
        self.assertEqual(
            [sample["source"] for sample in result["samples"]],
            ["nwm_v3_retrospective", "nwm_archived_analysis", "nwm_archived_analysis"],
        )
        self.assertEqual(result["samples"][0]["flows_cms"], {"7": 1.5, "8": 0.0})
        self.assertEqual(len(archived), 2)
        read_retro.assert_called_once_with(["7", "8"], start, start, deadline=unittest.mock.ANY)

    def test_missing_archive_hour_is_an_unavailable_sample(self):
        hour = datetime(2023, 2, 1, 1, tzinfo=UTC)
        with patch.object(archive_reader, "read_hour", return_value=None):
            result = historical_streamflow.lookup(["7", "8"], hour, hour)
        self.assertEqual(
            result["samples"],
            [{
                "time": hour,
                "source": "nwm_archived_analysis",
                "hour_issue": "archive_hour_unavailable",
                "flows_cms": {"7": None, "8": None},
            }],
        )


if __name__ == "__main__":
    unittest.main()
