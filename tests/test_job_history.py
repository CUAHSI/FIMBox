import io
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from botocore.exceptions import ClientError
from celery import Task
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fimbox_api import auth, history, main, tasks

STARTED = datetime(2026, 1, 1, tzinfo=timezone.utc)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        for target, value in ((main, "read_job_times"), (history, "mark_job_time")):
            patcher = patch.object(target, value, return_value={} if value == "read_job_times" else STARTED)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = auth.UserSession(
            user_id="alice", access_token="never-archive-this-token",
            scope="read", expires_at=time.time() + 60,
        )
        self.location = history.parse_location("s3://resource/some-resource/data/contents/history")
        self.job_id = str(uuid4())
        self.objects = {}
        self.client = Mock()

        def put(**kwargs):
            if kwargs.get("IfNoneMatch") == "*" and (kwargs["Bucket"], kwargs["Key"]) in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

        def get(**kwargs):
            value = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
            if value is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            if "Range" in kwargs:
                start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
                if start >= len(value):
                    raise ClientError({"Error": {"Code": "InvalidRange"}}, "GetObject")
                value = value[start:end + 1]
            return {"Body": io.BytesIO(value)}

        self.client.put_object.side_effect = put
        self.client.get_object.side_effect = get
        for name in ("s3_client", "output_s3_client"):
            patcher = patch.object(history, name, return_value=self.client)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("hydroshare_client_id", "test"), ("hydroshare_client_secret", SecretStr("test"))):
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: self.session
        self.addCleanup(main.app.dependency_overrides.clear)
        self.api = TestClient(main.app)
        self.headers = {"X-FIMBox-History-Location": "s3://resource/some-resource/data/contents/history"}
        self.request = {"scenarios": [{"ScenarioID": "s1", "Reaches": [
            {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 1},
        ]}], "output_bucket": "resource", "output_prefix": "outputs"}
        self.result = {"outputs": [{"ScenarioID": "s1"}], "output_bucket": "resource", "output_prefix": "outputs"}

    def prepare(self):
        return history.prepare(self.job_id, self.session, self.location, "hand-fim", self.request)

    def test_disabled_history_has_no_storage_access(self):
        self.assertEqual(history.prepare(self.job_id, self.session, None, "hand-fim", self.request), {})
        self.client.put_object.assert_not_called()

    def test_export_completed_redis_jobs_and_logs_then_retry(self):
        jobs = [self.job_id, str(uuid4()), str(uuid4()), str(uuid4())]
        results = [
            Mock(state="SUCCESS", result=self.result), Mock(state="FAILURE", result=RuntimeError("failed")),
            Mock(state="REVOKED"), Mock(state="STARTED"),
        ]
        redis = Mock()
        redis.get.return_value = b"retained log\n"
        with patch.object(main, "list_user_jobs", return_value=jobs), \
                patch.object(main, "user_has_job", return_value=True), \
                patch.object(main.celery_app, "AsyncResult", side_effect=lambda job: results[jobs.index(job)]), \
                patch.object(history, "get_redis_client", return_value=redis):
            response = self.api.post("/jobs/history/export", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"exported": 3, "skipped_existing": 0, "skipped_active": 1})
            before = dict(self.objects)
            response = self.api.post("/jobs/history/export", headers=self.headers)
            self.assertEqual(response.json(), {"exported": 0, "skipped_existing": 3, "skipped_active": 1})
            self.assertEqual(self.objects, before)
        for job_id, status in zip(jobs, ["success", "failure", "revoked"]):
            record = history.read(self.session, self.location, job_id)
            self.assertEqual(record.status, status)
            self.assertEqual(record.source, "redis-export")
            self.assertIsNone(record.submitted_at)
            self.assertEqual(record.request, {})
            self.assertEqual(history.read_logs(self.session, self.location, job_id, 0, 100)[0], "retained log\n")
        self.assertEqual(history.read(self.session, self.location, self.job_id).result.output_prefix, "outputs")
        self.assertEqual(history.read(self.session, self.location, jobs[1]).error, "failed")
        self.assertIsNone(history.read(self.session, self.location, jobs[3]))
        with patch.object(main, "user_has_job", return_value=False):
            response = self.api.get(f"/jobs/{self.job_id}", headers=self.headers)
            self.assertEqual(response.json()["status"], "success")
        self.prepare()
        keys = [{"Key": key} for _, key in self.objects]
        self.client.get_paginator.return_value.paginate.return_value = [{"Contents": keys}]
        records = history.list_records(self.session, self.location)
        self.assertEqual(len(records), 3)
        self.assertTrue(any(record.source == "submission" for record in records))
        self.assertTrue(any(record.submitted_at is None for record in records))

    def test_export_preserves_richer_existing_record_and_log(self):
        self.prepare()
        before = dict(self.objects)
        with patch.object(main, "list_user_jobs", return_value=[self.job_id]), \
                patch.object(main, "user_has_job", return_value=True), \
                patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="SUCCESS", result=self.result)):
            response = self.api.post("/jobs/history/export", headers=self.headers)
        self.assertEqual(response.json()["skipped_existing"], 1)
        self.assertEqual(self.objects, before)
        redis = Mock()
        redis.get.return_value = b"first log"
        job = history.JobStatus(job_id=self.job_id, status="success", result=self.result)
        with patch.object(history, "get_redis_client", return_value=redis):
            self.assertFalse(history.export_snapshot(self.session, self.location, job))
            before = dict(self.objects)
            redis.get.return_value = b"replacement log"
            self.assertFalse(history.export_snapshot(self.session, self.location, job))
            self.assertEqual(self.objects, before)

    def test_export_requires_location_and_ownership(self):
        response = self.api.post("/jobs/history/export")
        self.assertEqual(response.status_code, 400)
        with patch.object(main, "list_user_jobs", return_value=[self.job_id]), \
                patch.object(main, "user_has_job", return_value=False):
            response = self.api.post("/jobs/history/export", headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.client.put_object.assert_not_called()

    def test_export_storage_failure_is_explicit_and_retryable(self):
        redis = Mock()
        redis.get.return_value = b"log"
        original_put = self.client.put_object.side_effect

        def deny_metadata(**kwargs):
            if kwargs["Key"].endswith(".json"):
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
            original_put(**kwargs)

        self.client.put_object.side_effect = deny_metadata
        with patch.object(main, "list_user_jobs", return_value=[self.job_id]), \
                patch.object(main, "user_has_job", return_value=True), \
                patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="SUCCESS", result=self.result)), \
                patch.object(history, "get_redis_client", return_value=redis):
            response = self.api.post("/jobs/history/export", headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(history.read(self.session, self.location, self.job_id))
            self.client.put_object.side_effect = original_put
            response = self.api.post("/jobs/history/export", headers=self.headers)
            self.assertEqual(response.json()["exported"], 1)

    def __clear(self, live, states, headers=None):
        results = {job_id: Mock(state=state) for job_id, state in zip(live, states)}
        cleanup = {name: Mock() for name in ("clear_job_logs", "clear_job_times", "untrack_user_job")}
        with patch.object(main, "list_user_jobs", return_value=live), \
                patch.object(main.celery_app, "AsyncResult", side_effect=lambda job: results[job]), \
                patch.multiple(main, **cleanup):
            response = self.api.post("/jobs/history/clear", headers=headers or {})
        return response, results, cleanup

    def test_clear_removes_finished_jobs_from_redis_and_s3_but_keeps_active(self):
        finished, active, archived_only = self.job_id, str(uuid4()), str(uuid4())
        for job_id, status in ((finished, "success"), (active, "started"), (archived_only, "failure")):
            self.job_id = job_id
            self.prepare()
            record = history.read(self.session, self.location, job_id)
            record.status = status
            history.persist(self.client, self.location, record, b"log")
        output = ("resource", "outputs/scenario.tif")
        self.objects[output] = b"raster"
        self.client.delete_object.side_effect = lambda **kwargs: self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        self.client.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [{"Contents": [
            {"Key": key} for bucket, key in self.objects if key.startswith(kwargs["Prefix"])
        ]}]
        response, results, cleanup = self.__clear([finished, active], ["SUCCESS", "STARTED"], self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"cleared": 2, "skipped_active": 1})
        for job_id in (finished, archived_only):
            self.assertIsNone(history.read(self.session, self.location, job_id))
            self.assertEqual(history.read_logs(self.session, self.location, job_id, 0, 10)[0], "")
        self.assertEqual(history.read(self.session, self.location, active).status, "started")
        self.assertIn(output, self.objects)
        results[finished].forget.assert_called_once()
        results[active].forget.assert_not_called()
        cleanup["clear_job_logs"].assert_called_once_with(finished)
        cleanup["clear_job_times"].assert_called_once_with(finished)
        cleanup["untrack_user_job"].assert_called_once_with("alice", finished)

    def test_clear_without_location_only_touches_redis(self):
        response, _, cleanup = self.__clear([self.job_id, str(uuid4())], ["REVOKED", "PENDING"])
        self.assertEqual(response.json(), {"cleared": 1, "skipped_active": 1})
        cleanup["untrack_user_job"].assert_called_once_with("alice", self.job_id)
        self.client.delete_object.assert_not_called()

    def test_clear_storage_failure_keeps_redis_entries_for_retry(self):
        self.prepare()
        record = history.read(self.session, self.location, self.job_id)
        record.status = "success"
        history.persist(self.client, self.location, record)
        self.client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": key} for _, key in self.objects]}]
        self.client.delete_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
        response, results, cleanup = self.__clear([self.job_id], ["SUCCESS"], self.headers)
        self.assertEqual(response.status_code, 503)
        cleanup["untrack_user_job"].assert_not_called()
        results[self.job_id].forget.assert_not_called()

    def test_success_archives_inputs_result_and_logs_without_tokens(self):
        archive = self.prepare()
        redis = Mock()
        redis.get.return_value = "a flood ☃\n".encode()
        with patch.object(history, "get_redis_client", return_value=redis), \
                patch.object(Task, "__call__", return_value=self.result):
            returned = history.HistoryTask()("payload", **archive)
        self.assertEqual(returned, self.result)
        record = history.read(self.session, self.location, self.job_id)
        self.assertEqual(record.status, "success")
        self.assertEqual(record.request, self.request)
        self.assertEqual(record.result.output_prefix, "outputs")
        self.assertEqual((record.started_at, record.finished_at), (STARTED, STARTED))
        content = b"".join(self.objects.values())
        self.assertNotIn(b"never-archive-this-token", content)
        logs, offset = history.read_logs(self.session, self.location, self.job_id, 0, 9)
        self.assertEqual(logs, "a flood ")
        rest, next_offset = history.read_logs(self.session, self.location, self.job_id, offset, 100)
        self.assertEqual(rest, "☃\n")
        self.assertEqual(next_offset, len("a flood ☃\n".encode()))

    def test_failure_is_archived_and_original_exception_is_raised(self):
        archive = self.prepare()
        redis = Mock()
        redis.get.return_value = b"computation failed\n"
        with patch.object(history, "get_redis_client", return_value=redis), \
                patch.object(Task, "__call__", side_effect=RuntimeError("bad source")):
            with self.assertRaisesRegex(RuntimeError, "bad source"):
                history.HistoryTask()(**archive)
        record = history.read(self.session, self.location, self.job_id)
        self.assertEqual((record.status, record.error), ("failure", "bad source"))

    def test_celery_trace_executes_archive_wrapper(self):
        from fimbox_api.celery_app import celery_app

        expected = self.result

        @celery_app.task(name="tests.history_trace", base=history.HistoryTask)
        def operation(job_id, *, history_archive=None):
            return expected

        self.addCleanup(lambda: celery_app.tasks.pop("tests.history_trace", None))
        redis = Mock()
        redis.get.return_value = b"real task trace\n"
        with patch.object(history, "get_redis_client", return_value=redis):
            result = operation.apply(args=[self.job_id], kwargs=self.prepare(), throw=True)
        self.assertTrue(result.successful())
        self.assertEqual(history.read(self.session, self.location, self.job_id).status, "success")

    def test_status_archive_and_logs_remain_after_redis_expiry(self):
        archive = self.prepare()
        redis = Mock()
        redis.get.return_value = b"complete\n"
        with patch.object(history, "get_redis_client", return_value=redis), \
                patch.object(Task, "__call__", return_value=self.result):
            history.HistoryTask()(**archive)
        with patch.object(main, "user_has_job", return_value=False), \
                patch.object(main.celery_app, "AsyncResult") as celery:
            response = self.api.get(f"/jobs/{self.job_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "success")
            celery.assert_not_called()
            response = self.api.get(f"/jobs/{self.job_id}/archive", headers=self.headers)
            self.assertEqual(response.json()["request"], self.request)
            response = self.api.get(f"/jobs/{self.job_id}/logs", headers=self.headers)
            self.assertEqual(response.json()["logs"], "complete\n")

    def test_archived_raster_and_manifest_info_do_not_require_redis_ownership(self):
        archive = self.prepare()
        result = {
            **self.result,
            "files": [{"path": "s1.cog", "key": "outputs/s1.cog", "size_bytes": 100}],
            "scenario_manifests": [{
                "ScenarioID": "s1", "key": "outputs/s1/manifest.json", "tile_count": 1,
                "bounds": [-79, 35, -78, 36], "resolution": 10, "tile_size": 2048,
            }],
        }
        redis = Mock()
        redis.get.return_value = None
        with patch.object(history, "get_redis_client", return_value=redis), \
                patch.object(Task, "__call__", return_value=result):
            history.HistoryTask()(**archive)
        info = {"bounds": [-79, 35, -78, 36], "minzoom": 8, "maxzoom": 14}
        with patch.object(main, "user_has_job", return_value=False), \
                patch.object(main, "raster_source") as source, \
                patch.object(main, "raster_info", return_value=info), \
                patch.object(main, "scenario_info", return_value=info):
            response = self.api.get(f"/jobs/{self.job_id}/rasters/0/info", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(source.call_args.args[2:], ("resource", "outputs/s1.cog"))
            response = self.api.get(f"/jobs/{self.job_id}/scenarios/0/info", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)

    def test_user_namespaces_are_separate_and_records_are_checked(self):
        self.prepare()
        bob = self.session.model_copy(update={"user_id": "bob"})
        self.assertIsNone(history.read(bob, self.location, self.job_id))
        key = next(iter(self.objects))
        data = json.loads(self.objects[key])
        data["user_id"] = "bob"
        self.objects[key] = json.dumps(data).encode()
        response = self.api.get(f"/jobs/{self.job_id}", headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_pagination_and_live_jobs_are_merged_without_duplicates(self):
        self.prepare()
        keys = [{"Key": key} for bucket, key in self.objects]
        self.client.get_paginator.return_value.paginate.return_value = [{"Contents": []}, {"Contents": keys}]
        with patch.object(main, "list_user_jobs", return_value=[self.job_id]), \
                patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="STARTED")):
            response = self.api.get("/jobs", headers=self.headers)
        self.assertEqual(response.json()["jobs"], [{"job_id": self.job_id, "status": "started"}])
        with patch.object(main, "list_user_jobs", return_value=[]):
            response = self.api.get("/jobs", headers=self.headers)
        self.assertEqual(response.json()["jobs"], [{"job_id": self.job_id, "status": "queued"}])

    def test_submission_passes_nonsecret_archive_kwargs_and_preserves_arguments(self):
        with patch.object(main, "create_job_credentials"), patch.object(main, "track_user_job"), \
                patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.api.post("/jobs/hand-fim", json=self.request, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        call = enqueue.call_args.kwargs
        self.assertIn("history_archive", call["kwargs"])
        self.assertEqual(call["args"][1], self.request["scenarios"])
        tasks.hand_fim.__header__(*call["args"], **call["kwargs"])

    def test_storage_failure_prevents_enqueue(self):
        self.client.put_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        with patch.object(main, "create_job_credentials"), patch.object(main, "delete_job_credentials"), \
                patch.object(main, "untrack_user_job"), patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.api.post("/jobs/hand-fim", json=self.request, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        enqueue.assert_not_called()

    def test_invalid_locations_and_missing_archives_are_explicit(self):
        for value in ("https://example.com/history", "s3://resource/", "s3://resource/a/../b", "s3://resource/a?token=x"):
            with self.subTest(value=value):
                response = self.api.get("/jobs", headers={"X-FIMBox-History-Location": value})
                self.assertEqual(response.status_code, 422)
        response = self.api.get(f"/jobs/{self.job_id}/archive", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_enqueue_failure_and_cancellation_are_retained(self):
        archive = self.prepare()
        history.enqueue_failed(archive)
        self.assertEqual(history.read(self.session, self.location, self.job_id).status, "failure")
        history.mark_cancelled(self.session, self.location, self.job_id)
        self.assertEqual(history.read(self.session, self.location, self.job_id).status, "revoked")

    def test_worker_archives_revocation_without_history_header(self):
        archive = self.prepare()
        history.archive_revoked_task(Mock(kwargs=archive))
        self.assertEqual(history.read(self.session, self.location, self.job_id).status, "revoked")

    def test_archive_write_failure_is_not_reported_as_task_success(self):
        archive = self.prepare()
        redis = Mock()
        redis.get.return_value = b"complete\n"
        self.client.put_object.side_effect = [
            None, ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
        ]
        with patch.object(history, "get_redis_client", return_value=redis), \
                patch.object(Task, "__call__", return_value=self.result):
            with self.assertRaises(ClientError):
                history.HistoryTask()(**archive)


if __name__ == "__main__":
    unittest.main()
