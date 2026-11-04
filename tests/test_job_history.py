import io
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from botocore.exceptions import ClientError
from celery import Task
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError

from fimbox_api import auth, history, main, tasks, workspaces
from fimbox_api.models import StoredHandFimResult

NOW = datetime.now(timezone.utc)
LOCATION = "s3://resource/resource-id/data/contents/workspaces/study-a"
SCENARIOS = [{"ScenarioID": "s1", "Reaches": [
    {"HUC": "03020201", "ReachID": "8780625", "Streamflow": 1},
]}]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.session = auth.UserSession(
            user_id="alice", access_token="private-provider-token", scope="read write",
            expires_at=time.time() + 60,
        )
        self.objects = {}
        self.redis_values = {}
        self.owners = {}
        self.read_users = {"alice", "bob"}
        self.write_users = {"alice", "bob"}
        self.clients = {}
        self.redis = Mock()
        self.redis.get.side_effect = lambda key: self.redis_values.get(key)
        self.redis.set.side_effect = lambda key, value, **kw: self.redis_values.__setitem__(key, value)
        for module in (auth, history, workspaces):
            self.patch(module, "get_redis_client", return_value=self.redis)
        for module in (history, workspaces, main):
            self.patch(module, "output_s3_client", side_effect=lambda session: self.storage(session.user_id))
        self.patch(history, "s3_client", side_effect=lambda job_id: self.storage("alice"))
        self.patch(history, "mark_job_time", return_value=NOW)
        self.patch(main, "read_job_times", return_value={})
        self.patch(main, "create_job_credentials")
        self.patch(main, "delete_job_credentials")
        self.patch(main, "track_user_job", side_effect=lambda user, job: self.owners.__setitem__(job, user))
        self.patch(main, "untrack_user_job", side_effect=lambda user, job: self.owners.pop(job, None))
        self.patch(main, "user_has_job", side_effect=lambda user, job: self.owners.get(job) == user)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: self.session
        self.addCleanup(main.app.dependency_overrides.clear)
        self.api = TestClient(main.app)
        self.headers = {"X-FIMBox-Workspace": LOCATION}
        response = self.api.post("/workspaces", json={"location": LOCATION, "name": "Study A"})
        self.assertEqual(response.status_code, 201, response.text)
        self.workspace = workspaces.Workspace.model_validate(response.json())
        self.job_id = str(uuid4())

    def patch(self, module, name, **kwargs):
        patcher = patch.object(module, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def storage(self, user):
        if user in self.clients:
            return self.clients[user]
        client = Mock()

        def check(write=False):
            if user not in (self.write_users if write else self.read_users):
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject" if write else "GetObject")

        def put(**kwargs):
            check(write=True)
            key = (kwargs["Bucket"], kwargs["Key"])
            if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.objects[key] = kwargs["Body"]

        def get(**kwargs):
            check()
            value = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
            if value is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            if "Range" in kwargs:
                start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
                if start >= len(value):
                    raise ClientError({"Error": {"Code": "InvalidRange"}}, "GetObject")
                value = value[start:end + 1]
            return {"Body": io.BytesIO(value)}

        def listing(**kwargs):
            check()
            values = [{"Key": key} for bucket, key in self.objects
                      if bucket == kwargs["Bucket"] and key.startswith(kwargs["Prefix"])]
            return {"Contents": values[:kwargs.get("MaxKeys", len(values))]}

        client.put_object.side_effect = put
        client.get_object.side_effect = get
        client.list_objects_v2.side_effect = listing
        client.get_paginator.return_value.paginate.side_effect = lambda **kw: [listing(**kw)]
        self.clients[user] = client
        return client

    def prepare(self, job_id=None):
        return history.prepare(job_id or self.job_id, self.session, self.workspace, "hand-fim", {"scenarios": SCENARIOS})

    def finish(self, status="success"):
        archive = self.prepare()
        record = history.ArchivedJob.model_validate(archive["history_archive"]["record"])
        record.status = status
        record.submitted_at = NOW - timedelta(days=2)
        if status == "success":
            prefix = f"{self.workspace.prefix}/jobs/{self.job_id}/outputs"
            record.result = StoredHandFimResult(
                output_bucket="resource", output_prefix=prefix, outputs=[{"ScenarioID": "s1"}],
                files=[{"path": "s1.cog", "key": prefix + "/s1.cog", "size_bytes": 100}],
            )
        else:
            record.error = "computation failed"
        history.persist(self.storage("alice"), self.workspace, record, b"complete\n")
        return record

    def as_bob(self):
        self.session = self.session.model_copy(update={"user_id": "bob", "access_token": auth.SecretStr("bob-token")})

    def test_create_requires_empty_subprefix_and_preserves_resource_contents(self):
        self.objects[("resource", "resource-id/data/contents/existing.txt")] = b"unrelated"
        for location, status in (
            (LOCATION, 409),
            ("s3://resource/resource-id/data/contents", 422),
            ("s3://resource/resource-id/data/contents/new-study", 201),
        ):
            response = self.api.post("/workspaces", json={"location": location, "name": "Other"})
            self.assertEqual(response.status_code, status, response.text)
            if status == 422:
                self.assertEqual(response.json()["detail"][0]["loc"], ["body", "location"])
        self.assertEqual(self.objects[("resource", "resource-id/data/contents/existing.txt")], b"unrelated")
        occupied = "resource-id/data/contents/occupied"
        self.objects[("resource", occupied + "/file.txt")] = b"x"
        self.assertEqual(self.api.post("/workspaces", json={
            "location": "s3://resource/" + occupied, "name": "Other",
        }).status_code, 409)

    def test_conditional_creation_protects_concurrent_creator(self):
        client = self.storage("alice")
        client.list_objects_v2.return_value = {}
        client.list_objects_v2.side_effect = lambda **kw: {}
        before = dict(self.objects)
        response = self.api.post("/workspaces", json={"location": LOCATION, "name": "Replacement"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.objects, before)

    def test_open_is_read_only_and_missing_does_not_create(self):
        self.as_bob()
        self.write_users.remove("bob")
        before = dict(self.objects)
        response = self.api.post("/workspaces/open", json={"location": LOCATION})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["workspace_id"], str(self.workspace.workspace_id))
        self.assertEqual(self.api.post("/workspaces/open", json={"location": LOCATION + "-missing"}).status_code, 404)
        self.assertEqual(self.objects, before)

    def test_authentication_and_workspace_are_mandatory(self):
        self.assertEqual(self.api.get("/jobs").status_code, 400)
        self.assertEqual(self.api.post("/jobs/hand-fim", json={"scenarios": SCENARIOS}).status_code, 400)
        self.assertEqual(self.api.get("/jobs", headers={"X-FIMBox-History-Location": LOCATION}).status_code, 400)
        main.app.dependency_overrides.clear()
        with patch.object(main.settings, "hydroshare_client_id", ""), \
                patch.object(main.settings, "hydroshare_client_secret", auth.SecretStr("")):
            self.assertEqual(self.api.get("/jobs", headers=self.headers).status_code, 503)
        self.assertEqual(self.api.get("/jobs", headers=self.headers).status_code, 401)

    def test_invalid_descriptors_and_locations_are_explicit(self):
        response = self.api.post("/workspaces", json={"location": LOCATION, "name": "Other", "typo": True})
        self.assertEqual(response.status_code, 422)
        for value in ("https://example.com", "s3://resource/", "s3://resource/a/../b", LOCATION + "?token=x"):
            response = self.api.get("/jobs", headers={"X-FIMBox-Workspace": value})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"][0]["loc"], ["header", "X-FIMBox-Workspace"])
        key = ("resource", self.workspace.prefix + "/workspace.json")
        data = json.loads(self.objects[key])
        data["version"] = 9
        self.objects[key] = json.dumps(data).encode()
        self.assertEqual(self.api.get("/jobs", headers=self.headers).status_code, 409)
        self.objects[key] = b"not-json"
        self.assertEqual(self.api.get("/jobs", headers=self.headers).status_code, 502)
        self.objects[key] = b"x" * (workspaces.MAX_DESCRIPTOR_BYTES + 1)
        response = self.api.get("/jobs", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertIn("16 KiB", response.json()["detail"])

    def test_submit_assigns_outputs_and_persists_before_enqueue(self):
        def enqueue(**kwargs):
            record = history.read(self.session, self.workspace, kwargs["task_id"])
            self.assertEqual(record.status, "queued")
            self.assertEqual(record.user_id, "alice")
        with patch.object(tasks.hand_fim, "apply_async", side_effect=enqueue) as operation:
            response = self.api.post("/jobs/hand-fim", headers=self.headers, json={"scenarios": SCENARIOS})
        self.assertEqual(response.status_code, 202, response.text)
        call = operation.call_args.kwargs
        prefix = f"{self.workspace.prefix}/jobs/{response.json()['job_id']}/outputs"
        self.assertEqual(call["args"][2:4], ["resource", prefix])
        self.assertNotIn("private-provider-token", json.dumps(call))
        self.assertTrue(workspaces.job_is_bound(self.workspace, response.json()["job_id"]))
        tasks.hand_fim.__header__(*call["args"], **call["kwargs"])

    def test_destinations_and_comparison_inputs_cannot_escape_workspace(self):
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.api.post("/jobs/hand-fim", headers=self.headers, json={
                "scenarios": SCENARIOS, "output_bucket": "other", "output_prefix": "elsewhere",
            })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body"])
        enqueue.assert_not_called()
        with patch.object(tasks.hand_fim_compare, "apply_async") as enqueue:
            response = self.api.post("/jobs/compare", headers=self.headers, json={
                "base_bucket": "resource", "base_prefix": self.workspace.prefix + "-other/run",
                "candidate_bucket": "resource", "candidate_prefix": self.workspace.prefix + "/run",
            })
        self.assertEqual(response.status_code, 403)
        enqueue.assert_not_called()

    def test_comparison_and_parity_jobs_retain_workspace_records(self):
        requests = [
            ("/jobs/compare", tasks.hand_fim_compare, {
                "base_bucket": "resource", "base_prefix": self.workspace.prefix + "/jobs/base/outputs",
                "candidate_bucket": "resource", "candidate_prefix": self.workspace.prefix + "/jobs/candidate/outputs",
            }),
            ("/jobs/parity", tasks.hand_fim_parity, {
                "legacy_bucket": "resource", "legacy_prefix": self.workspace.prefix + "/jobs/base/outputs",
            }),
        ]
        with patch.object(main.settings, "stream_input_bucket", "hydrofabric"):
            for path, task, body in requests:
                with self.subTest(path=path), patch.object(task, "apply_async") as enqueue:
                    response = self.api.post(path, headers=self.headers, json=body)
                    self.assertEqual(response.status_code, 202, response.text)
                    job_id = response.json()["job_id"]
                    record = history.read(self.session, self.workspace, job_id)
                    self.assertEqual(record.workspace_id, self.workspace.workspace_id)
                    self.assertEqual(record.user_id, "alice")
                    self.assertEqual(record.request["output_prefix"], f"{self.workspace.prefix}/jobs/{job_id}/outputs")
                    self.assertIn("history_archive", enqueue.call_args.kwargs["kwargs"])
                    task.__header__(*enqueue.call_args.kwargs["args"], **enqueue.call_args.kwargs["kwargs"])

    def test_collaborators_can_read_complete_and_failed_history_after_expiry(self):
        record = self.finish()
        self.as_bob()
        self.redis_values.clear()
        with patch.object(main.celery_app, "AsyncResult") as celery:
            for path in (f"/jobs/{self.job_id}", f"/jobs/{self.job_id}/archive", f"/jobs/{self.job_id}/logs", "/jobs"):
                response = self.api.get(path, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
            celery.assert_not_called()
        jobs = response.json()["jobs"]
        self.assertEqual([(j["job_id"], j["user_id"]) for j in jobs], [(self.job_id, "alice")])
        self.assertLess(datetime.fromisoformat(jobs[0]["submitted_at"]), NOW - timedelta(hours=24))
        record.status = "failure"
        record.result = None
        record.error = "failed"
        history.persist(self.storage("alice"), self.workspace, record)
        self.assertEqual(self.api.get(f"/jobs/{self.job_id}", headers=self.headers).json()["error"], "failed")

    def test_read_only_storage_denies_submission_without_enqueue(self):
        self.as_bob()
        self.write_users.remove("bob")
        with patch.object(history, "s3_client", return_value=self.storage("bob")), \
                patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.api.post("/jobs/hand-fim", headers=self.headers, json={"scenarios": SCENARIOS})
        self.assertEqual(response.status_code, 403, response.text)
        enqueue.assert_not_called()

    def test_no_storage_access_denies_workspace_and_jobs(self):
        self.finish()
        self.as_bob()
        self.read_users.remove("bob")
        for path in ("/jobs", f"/jobs/{self.job_id}", f"/jobs/{self.job_id}/logs"):
            self.assertEqual(self.api.get(path, headers=self.headers).status_code, 403)

    def test_live_reads_require_trusted_workspace_binding_not_submitter(self):
        self.prepare()
        workspaces.bind_job(self.workspace, self.job_id)
        self.as_bob()
        with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="STARTED")), \
                patch.object(main, "read_job_logs", return_value=("live\n", 5)):
            self.assertEqual(self.api.get(f"/jobs/{self.job_id}", headers=self.headers).json()["status"], "started")
            self.assertEqual(self.api.get(f"/jobs/{self.job_id}/logs", headers=self.headers).json()["logs"], "live\n")

    def test_lost_redis_keeps_last_known_snapshot_explicit(self):
        archive = self.prepare()
        record = history.ArchivedJob.model_validate(archive["history_archive"]["record"])
        record.status = "started"
        history.persist(self.storage("alice"), self.workspace, record)
        for bound in (False, True):
            if bound:
                workspaces.bind_job(self.workspace, self.job_id)
            with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="PENDING")):
                body = self.api.get(f"/jobs/{self.job_id}", headers=self.headers).json()
            self.assertEqual(body["status"], "started")
            self.assertFalse(body["execution_state_known"])

    def test_cancel_is_submitter_only_even_if_archive_is_modified(self):
        archive = self.prepare()
        workspaces.bind_job(self.workspace, self.job_id)
        self.owners[self.job_id] = "alice"
        record = history.ArchivedJob.model_validate(archive["history_archive"]["record"])
        record.user_id = "bob"
        history.persist(self.storage("alice"), self.workspace, record)
        self.as_bob()
        with patch.object(main.celery_app.control, "revoke") as revoke:
            self.assertEqual(self.api.delete(f"/jobs/{self.job_id}", headers=self.headers).status_code, 403)
            revoke.assert_not_called()
        self.session = self.session.model_copy(update={"user_id": "alice"})
        with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="PENDING")), \
                patch.object(main.celery_app.control, "revoke") as revoke:
            self.assertEqual(self.api.delete(f"/jobs/{self.job_id}", headers=self.headers).status_code, 204)
            revoke.assert_called_once_with(self.job_id, terminate=False)
        self.assertEqual(history.read(self.session, self.workspace, self.job_id).status, "revoked")

    def test_other_workspace_cannot_expose_job_or_cancel_it(self):
        self.finish()
        other = LOCATION + "-other"
        self.api.post("/workspaces", json={"location": other, "name": "Other"})
        headers = {"X-FIMBox-Workspace": other}
        self.assertEqual(self.api.get("/jobs", headers=headers).json(), {"jobs": []})
        self.assertEqual(self.api.get(f"/jobs/{self.job_id}", headers=headers).status_code, 404)
        self.owners[self.job_id] = "alice"
        self.assertEqual(self.api.delete(f"/jobs/{self.job_id}", headers=headers).status_code, 403)

    def test_forged_record_cannot_grant_access_to_another_workspaces_live_state(self):
        archive = self.prepare()
        workspaces.bind_job(self.workspace, self.job_id)
        other_location = LOCATION + "-other"
        response = self.api.post("/workspaces", json={"location": other_location, "name": "Other"})
        other = workspaces.Workspace.model_validate(response.json())
        forged = history.ArchivedJob.model_validate(archive["history_archive"]["record"])
        forged.workspace_id = other.workspace_id
        history.persist(self.storage("alice"), other, forged)
        headers = {"X-FIMBox-Workspace": other_location}
        with patch.object(main.celery_app, "AsyncResult") as celery, \
                patch.object(main, "read_job_logs") as live_logs:
            response = self.api.get(f"/jobs/{self.job_id}", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["execution_state_known"])
            response = self.api.get(f"/jobs/{self.job_id}/logs", headers=headers)
            self.assertEqual(response.json()["logs"], "")
            celery.assert_not_called()
            live_logs.assert_not_called()

    def test_worker_binding_survives_client_workspace_switch(self):
        archive = self.prepare()
        other = self.api.post("/workspaces", json={"location": LOCATION + "-other", "name": "Other"}).json()
        self.redis_values[history.JOB_LOG_PREFIX + self.job_id] = b"finished\n"
        prefix = f"{self.workspace.prefix}/jobs/{self.job_id}/outputs"
        result = {"outputs": [{"ScenarioID": "s1"}], "output_bucket": "resource", "output_prefix": prefix}
        with patch.object(Task, "__call__", return_value=result):
            history.HistoryTask()(**archive)
        record = history.read(self.session, self.workspace, self.job_id)
        self.assertEqual(record.status, "success")
        self.assertEqual(record.workspace_id, self.workspace.workspace_id)
        self.assertIsNone(history.read(self.session, workspaces.Workspace.model_validate(other), self.job_id))

    def test_worker_failure_and_archive_failure_are_not_silenced(self):
        archive = self.prepare()
        with patch.object(Task, "__call__", side_effect=RuntimeError("science failed")):
            with self.assertRaisesRegex(RuntimeError, "science failed"):
                history.HistoryTask()(**archive)
        self.assertEqual(history.read(self.session, self.workspace, self.job_id).status, "failure")
        self.write_users.remove("alice")
        with self.assertRaises(ClientError):
            history.enqueue_failed(archive)

    def test_enqueue_failure_is_retained_and_credentials_cleaned(self):
        with patch.object(tasks.hand_fim, "apply_async", side_effect=OperationalError("down")):
            response = self.api.post("/jobs/hand-fim", headers=self.headers, json={"scenarios": SCENARIOS})
        self.assertEqual(response.status_code, 503)
        records = history.list_records(self.session, self.workspace)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "failure")
        main.delete_job_credentials.assert_called_once()

    def test_missing_outputs_do_not_remove_history_or_borrow_submitter_credentials(self):
        record = self.finish()
        self.as_bob()
        with patch.object(main, "output_raster_source") as source, \
                patch.object(main, "raster_info", side_effect=main.RasterReadError("Output file unavailable")):
            response = self.api.get(f"/jobs/{self.job_id}/rasters/0/info", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(source.call_args.args[0].user_id, "bob")
        self.assertEqual(source.call_args.args[1:], ("resource", record.result.files[0].key))
        self.assertEqual(self.api.get(f"/jobs/{self.job_id}", headers=self.headers).json()["status"], "success")

    def test_legacy_clear_and_export_are_not_available(self):
        before = dict(self.objects)
        for path in ("/jobs/history/export", "/jobs/history/clear"):
            self.assertIn(self.api.post(path, headers=self.headers).status_code, (404, 405))
        self.assertEqual(self.objects, before)

    def test_workspace_record_identity_is_validated(self):
        self.prepare()
        key = ("resource", self.workspace.prefix + f"/jobs/{self.job_id}/record.json")
        data = json.loads(self.objects[key])
        data["workspace_id"] = str(uuid4())
        self.objects[key] = json.dumps(data).encode()
        self.assertEqual(self.api.get(f"/jobs/{self.job_id}", headers=self.headers).status_code, 403)

    def test_history_listing_paginates_and_ignores_outputs(self):
        self.finish()
        keys = [{"Key": key} for bucket, key in self.objects if "/jobs/" in key]
        client = self.storage("alice")
        client.get_paginator.return_value.paginate.side_effect = None
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": []}, {"Contents": keys + [{"Key": self.workspace.prefix + f"/jobs/{self.job_id}/outputs/data.json"}]},
        ]
        self.assertEqual(len(history.list_records(self.session, self.workspace)), 1)


if __name__ == "__main__":
    unittest.main()
