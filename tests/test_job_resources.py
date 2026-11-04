import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fimbox_api import job_resources


class PeakMemorySamplerTests(unittest.TestCase):
    def test_records_the_largest_sample_during_task(self):
        with tempfile.TemporaryDirectory() as directory:
            usage_file = Path(directory) / "memory.current"
            usage_file.write_text("1024", encoding="ascii")
            with patch.object(job_resources, "CGROUP_MEMORY_FILES", (usage_file,)), patch.object(
                job_resources, "SAMPLE_INTERVAL_SECONDS", 0.005,
            ):
                with job_resources.PeakMemorySampler() as sampler:
                    usage_file.write_text("2048", encoding="ascii")
                    time.sleep(0.02)
            self.assertEqual(sampler.peak_bytes, 2048)

    def test_unavailable_cgroup_metrics_are_reported_as_unavailable(self):
        with patch.object(job_resources, "CGROUP_MEMORY_FILES", (Path("/missing/memory.current"),)):
            with job_resources.PeakMemorySampler() as sampler:
                pass
        self.assertIsNone(sampler.peak_bytes)


if __name__ == "__main__":
    unittest.main()
