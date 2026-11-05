import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fimbox_api import dataset_preparations, tasks

HAND_FIM = Path(__file__).resolve().parents[1] / "docker" / "handfim"
sys.path.insert(0, str(HAND_FIM))


class DatasetPreparationTaskTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "job_id": "job-1",
            "source_version": "4.5.2.11",
            "source_location": "s3://ciroh-owp-hand-fim/hand_fim_4_5_2_11/",
            "destination_location": "s3://input/hand",
            "destination_bucket": "input",
            "destination_prefix": "hand",
            "dataset_name": "Input dataset",
            "submitted_by": "alice",
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "hucs": [],
            "all_hucs": True,
            "session": {
                "user_id": "alice",
                "access_token": "runtime-token",
                "refresh_token": "refresh-token",
                "scope": "read write",
                "expires_at": 2000000000,
            },
        }

    def test_coordinator_initializes_workspace_and_queues_huc_batches(self):
        hucs = [f"{number:08d}" for number in range(1, 12)]
        collection = SimpleNamespace(
            source_s3_client=Mock(return_value="source"),
            list_hucs=Mock(return_value=hucs),
            select_hucs=Mock(return_value=hucs),
            initialize_collection=Mock(),
        )
        queued_group = Mock()
        queued_group.apply_async = Mock()
        captured_batches = []

        def capture_group(signatures):
            captured_batches.extend(list(signatures))
            return queued_group

        with (
            patch.dict(sys.modules, {"prepare_streaming_collection": collection}),
            patch.object(dataset_preparations, "load_run_context", return_value=self.context),
            patch.object(dataset_preparations, "is_cancelled", return_value=False),
            patch.object(dataset_preparations, "mark_running") as mark_running,
            patch.object(tasks, "group", side_effect=capture_group) as make_group,
            patch("fimbox_api.storage.output_s3_client", return_value="output") as output_client,
        ):
            tasks.prepare_dataset.run("job-1")

        self.assertEqual(collection.initialize_collection.call_args.kwargs["available_hucs"], hucs)
        self.assertEqual(mark_running.call_args.args, ("job-1", 11))
        self.assertEqual(output_client.call_args.args[0].access_token.get_secret_value(), "runtime-token")
        self.assertEqual(len(captured_batches), 2)
        self.assertTrue(make_group.called)
        queued_group.apply_async.assert_called_once_with()
        signature_args = [signature.args for signature in captured_batches]
        self.assertEqual(signature_args, [
            ("job-1", hucs[:10]),
            ("job-1", hucs[10:]),
        ])

    def test_batch_marks_each_huc_failure_and_continues_the_batch(self):
        collection = SimpleNamespace(
            source_s3_client=Mock(return_value="source"),
            prepare_single_huc=Mock(side_effect=[RuntimeError("failed"), True]),
        )
        with (
            patch.dict(sys.modules, {"prepare_streaming_collection": collection}),
            patch.object(dataset_preparations, "load_run_context", return_value=self.context),
            patch.object(dataset_preparations, "is_cancelled", return_value=False),
            patch.object(dataset_preparations, "mark_huc_failed") as mark_failed,
            patch.object(dataset_preparations, "mark_huc_complete") as mark_complete,
            patch("fimbox_api.storage.output_s3_client", return_value="output"),
        ):
            with self.assertRaisesRegex(RuntimeError, "00000001"):
                tasks.prepare_dataset_batch.run("job-1", ["00000001", "00000002"])

        mark_failed.assert_called_once_with("job-1", "00000001", "HUC 00000001 failed (RuntimeError)")
        mark_complete.assert_called_once_with("job-1", "00000002")


if __name__ == "__main__":
    unittest.main()
