import json
import time
import unittest
from unittest.mock import Mock, patch

from pydantic import SecretStr

from fimbox_api import auth, dataset_preparations as preparations


class DatasetPreparationTests(unittest.TestCase):
    def setUp(self):
        self.redis = Mock()
        pipeline = Mock()
        pipeline.__enter__ = Mock(return_value=pipeline)
        pipeline.__exit__ = Mock(return_value=False)
        stored = {}
        states = {}
        sets = {}

        def sadd(key, *values):
            target = sets.setdefault(key, set())
            before = len(target)
            target.update(values)
            return len(target) - before

        def srem(key, *values):
            target = sets.setdefault(key, set())
            before = len(target)
            target.difference_update(values)
            return before - len(target)
        pipeline.set.side_effect = lambda key, value, **_kwargs: stored.__setitem__(key, value)
        pipeline.hset.side_effect = lambda key, mapping: states.setdefault(key, {}).update(mapping)
        self.redis.pipeline.return_value = pipeline
        self.redis.get.side_effect = lambda key: stored.get(key)
        self.redis.hset.side_effect = lambda key, mapping: states.setdefault(key, {}).update(mapping)
        self.redis.hgetall.side_effect = lambda key: states.get(key, {"status": "running"})
        self.redis.hget.side_effect = lambda key, field: states.get(key, {}).get(field)
        self.redis.sadd.side_effect = sadd
        self.redis.srem.side_effect = srem
        self.redis.scard.side_effect = lambda key: len(sets.get(key, set()))
        self.redis.set.return_value = True
        self.session = auth.UserSession(
            user_id="alice",
            access_token=SecretStr("provider-token"),
            refresh_token=SecretStr("refresh-token"),
            scope="read write",
            expires_at=time.time() + 3600,
        )
        settings_patch = patch.object(
            preparations.settings, "stream_input_bucket", "input-resource",
        )
        prefix_patch = patch.object(
            preparations.settings, "stream_input_prefix", "resource-id/data/contents/parquet",
        )
        for item in (settings_patch, prefix_patch):
            item.start()
            self.addCleanup(item.stop)
        self.redis_patch = patch.object(preparations, "get_redis_client", return_value=self.redis)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)

    def test_submit_stores_run_context_and_queues_celery_task_without_returning_credentials(self):
        with patch.object(preparations.celery_app, "send_task") as enqueue:
            run = preparations.submit(
                self.session,
                preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
            )

        self.assertEqual(run.status, "queued")
        self.assertEqual(run.source_location, "s3://ciroh-owp-hand-fim/hand_fim_4_5_2_11/")
        self.assertEqual(run.destination_location, "s3://input-resource/resource-id/data/contents/parquet")
        self.assertEqual(run.celery_task_id, run.job_id)
        self.assertNotIn("provider-token", run.model_dump_json())
        self.assertNotIn("refresh-token", run.model_dump_json())
        enqueue.assert_called_once_with(
            "fimbox_api.tasks.prepare_dataset",
            args=[run.job_id],
            task_id=run.job_id,
            retry=False,
        )
        pipeline = self.redis.pipeline.return_value
        stored_context = json.loads(pipeline.set.call_args.args[1])
        self.assertEqual(stored_context["session"]["access_token"], "provider-token")
        self.assertEqual(stored_context["session"]["refresh_token"], "refresh-token")
        self.assertEqual(stored_context["submitted_by"], "alice")
        self.assertEqual(stored_context["hucs"], ["00000001"])
        self.assertEqual(stored_context["destination_bucket"], "input-resource")
        self.assertEqual(stored_context["destination_prefix"], "resource-id/data/contents/parquet")
        self.assertNotIn("workspace_id", stored_context)

    def test_submit_rejects_missing_write_scope(self):
        session = self.session.model_copy(update={"scope": "read"})
        with self.assertRaises(PermissionError):
            preparations.submit(
                session,
                preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
            )
        self.redis.set.assert_not_called()

    def test_submit_allows_preparation_into_dataset_with_existing_files(self):
        with patch.object(preparations.celery_app, "send_task"):
            preparations.submit(
                self.session,
                preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
            )
        self.redis.set.assert_called_once()

    def test_submit_reserves_input_dataset_against_concurrent_runs(self):
        self.redis.set.return_value = False
        with patch.object(preparations.celery_app, "send_task") as enqueue:
            with self.assertRaises(FileExistsError):
                preparations.submit(
                    self.session,
                    preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
                )
        enqueue.assert_not_called()
        self.redis.pipeline.assert_not_called()

    def test_submit_surfaces_celery_broker_errors_and_releases_input_dataset_reservation(self):
        with patch.object(preparations.celery_app, "send_task", side_effect=OSError("broker unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Celery could not queue"):
                preparations.submit(
                    self.session,
                    preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
                )
        self.redis.delete.assert_called_once()

    def test_cancel_releases_reservation_revokes_task_and_blocks_later_updates(self):
        with patch.object(preparations.celery_app, "send_task"):
            run = preparations.submit(
                self.session,
                preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
            )
        self.redis.get.side_effect = lambda key, _get=self.redis.get.side_effect: (
            run.job_id if key.startswith(preparations.RUN_PREFIX + "destination:") else _get(key)
        )
        with patch.object(preparations.celery_app, "control") as control:
            cancelled = preparations.cancel(run.job_id, "alice")

        self.assertEqual(cancelled.status, "cancelled")
        control.revoke.assert_called_once_with(run.job_id)
        self.redis.delete.assert_called_once()
        preparations.mark_running(run.job_id, 1)
        preparations.mark_failed(run.job_id, "late")
        self.assertTrue(preparations.is_cancelled(run.job_id))
        with self.assertRaises(FileExistsError):
            preparations.cancel(run.job_id, "alice")

    def test_cancel_is_private_to_submitter(self):
        with patch.object(preparations.celery_app, "send_task"):
            run = preparations.submit(
                self.session,
                preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"]),
            )
        with self.assertRaises(KeyError):
            preparations.cancel(run.job_id, "bob")

    def test_huc_completion_marks_run_complete_and_releases_input_dataset(self):
        with patch.object(preparations.celery_app, "send_task"):
            run = preparations.submit(
                self.session,
                preparations.PreparationRequest(
                    source_version="4.5.2.11", hucs=["00000001", "00000002"],
                ),
            )
        preparations.mark_running(run.job_id, 2)
        preparations.mark_huc_complete(run.job_id, "00000001")
        self.assertEqual(self.redis.hgetall(preparations.RUN_PREFIX + run.job_id + ":state")["status"], "running")
        self.redis.delete.assert_not_called()

        preparations.mark_huc_complete(run.job_id, "00000002")

        state = self.redis.hgetall(preparations.RUN_PREFIX + run.job_id + ":state")
        self.assertEqual(state["status"], "completed")
        self.redis.delete.assert_called_once()

    def test_failed_huc_fails_run_but_waits_for_other_hucs_before_releasing_workspace(self):
        with patch.object(preparations.celery_app, "send_task"):
            run = preparations.submit(
                self.session,
                preparations.PreparationRequest(
                    source_version="4.5.2.11", hucs=["00000001", "00000002"],
                ),
            )
        preparations.mark_running(run.job_id, 2)
        preparations.mark_huc_failed(run.job_id, "00000001", "HUC preparation failed")
        self.redis.delete.assert_not_called()

        preparations.mark_huc_complete(run.job_id, "00000002")

        state = self.redis.hgetall(preparations.RUN_PREFIX + run.job_id + ":state")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "HUC preparation failed")
        self.redis.delete.assert_called_once()

    def test_run_status_is_private_to_submitter(self):
        self.redis.get.side_effect = None
        self.redis.get.return_value = json.dumps({
            "submitted_by": "alice",
            "source_version": "4.5.2.11",
            "source_location": "s3://source",
            "dataset_name": "hand",
            "destination_location": "s3://input/hand",
            "destination_bucket": "input",
            "destination_prefix": "hand",
            "hucs": ["00000001", "00000002", "00000003"],
            "all_hucs": False,
            "submitted_at": "2026-01-01T00:00:00+00:00",
        })
        self.redis.hgetall.side_effect = None
        self.redis.hgetall.return_value = {"status": "running", "total_hucs": "3"}
        self.redis.scard.side_effect = lambda _key: 2

        run = preparations.get_run("job-1", "alice")
        self.assertEqual(run.completed_hucs, 2)
        self.assertEqual(run.requested_huc_count, 3)
        with self.assertRaises(KeyError):
            preparations.get_run("job-1", "bob")

    def test_request_requires_valid_huc_selection(self):
        with self.assertRaises(ValueError):
            preparations.PreparationRequest(source_version="4.5.2.11")
        with self.assertRaises(ValueError):
            preparations.PreparationRequest(source_version="4.5.2.11", hucs=["123"], all_hucs=False)
        with self.assertRaises(ValueError):
            preparations.PreparationRequest(source_version="4.5.2.11", hucs=["00000001"], all_hucs=True)
        request = preparations.PreparationRequest(source_version="4.5.2.11", all_hucs=True)
        self.assertEqual(request.hucs, [])


if __name__ == "__main__":
    unittest.main()
