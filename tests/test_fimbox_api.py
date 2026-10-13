import io
import json
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from botocore.exceptions import ClientError

from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from pydantic import SecretStr, ValidationError

from fimbox_api import auth, job_logs, main, storage, tasks
from fimbox_api.models import HandFimParameters, OutputDestination


PAYLOAD = [
    {
        "ScenarioID": "single-reach-scenario",
        "Reaches": [{"HUC": "03020201", "ReachID": "8780625", "Streamflow": 227}],
    }
]
DESTINATION = {"output_bucket": "fim-output", "output_prefix": "data/output"}
SUBMISSION = {"scenarios": PAYLOAD, **DESTINATION}
HAND_FIM_ROUTE = "/jobs/hand-fim"

def credentials_fixture():
    return storage.HydroShareS3Credentials(
        access_key="user:test-access", secret_key="test-secret",
        expiry=datetime.now(timezone.utc) + timedelta(days=1),
    )


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        for name, value in (
            ("hydroshare_client_id", "test-client"),
            ("hydroshare_client_secret", SecretStr("test-secret")),
        ):
            patcher = patch.object(main.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        main.app.dependency_overrides[auth.get_authenticated_session] = lambda: auth.UserSession(
            user_id="alice", access_token="provider-token", scope="read", expires_at=time.time() + 60,
        )
        self.addCleanup(main.app.dependency_overrides.clear)
        for name in (
            "track_user_job", "untrack_user_job", "user_has_job",
            "create_job_credentials", "delete_job_credentials",
        ):
            patcher = patch.object(main, name, return_value=True)
            mock = patcher.start()
            setattr(self, name, mock)
            self.addCleanup(patcher.stop)
        patcher = patch.object(storage, "__job_credentials", side_effect=lambda job_id: credentials_fixture())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_submit_scenarios_and_options_in_payload(self):
        operation = main.app.openapi()["paths"]["/jobs/hand-fim"]["post"]
        self.assertNotIn("parameters", operation)
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.client.post(HAND_FIM_ROUTE, json=SUBMISSION)
            self.assertEqual(response.status_code, 202)
            result = response.json()
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["status_url"], f"/jobs/{result['job_id']}")
            enqueue.assert_called_once_with(
                args=[result["job_id"], PAYLOAD, "fim-output", "data/output", None],
                task_id=result["job_id"],
                retry=False,
            )
            self.track_user_job.assert_called_with("alice", result["job_id"])
            self.create_job_credentials.assert_called()
            self.assertNotIn("provider-token", str(enqueue.call_args))

    def test_cloud_policy_requires_s3_for_submission(self):
        with patch.object(main.settings, "require_hydroshare_output", True), patch.object(
            tasks.hand_fim, "apply_async",
        ) as enqueue:
            response = self.client.post("/jobs/hand-fim", json={"scenarios": PAYLOAD})
            self.assertEqual(response.status_code, 422)
            self.assertIn("output_bucket", response.json()["detail"])
            enqueue.assert_not_called()
            self.track_user_job.assert_not_called()
            self.create_job_credentials.assert_not_called()
            self.assertEqual(self.client.post(HAND_FIM_ROUTE, json=SUBMISSION).status_code, 202)
            self.create_job_credentials.assert_called_once()
            enqueue.assert_called_once()

    def test_anonymous_local_job_workflow_and_s3_rejection(self):
        main.app.dependency_overrides.clear()
        with patch.object(main.settings, "hydroshare_client_id", ""), patch.object(
            main.settings, "hydroshare_client_secret", SecretStr(""),
        ), patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.client.post("/jobs/hand-fim", json={"scenarios": PAYLOAD})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            self.track_user_job.assert_called_with(auth.LOCAL_USER_ID, job_id)
            self.assertEqual(enqueue.call_args.kwargs["args"], [job_id, PAYLOAD, None, "", None])
            self.assertEqual(self.client.post(HAND_FIM_ROUTE, json=SUBMISSION).status_code, 409)
            self.assertEqual(enqueue.call_count, 1)
            self.create_job_credentials.assert_not_called()
            with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="PENDING")), patch.object(
                main, "list_user_jobs", return_value=[job_id],
            ) as jobs, patch.object(main.celery_app.control, "revoke") as revoke:
                self.assertEqual(self.client.get("/jobs").json(), {
                    "jobs": [{"job_id": job_id, "status": "queued"}],
                })
                jobs.assert_called_with(auth.LOCAL_USER_ID)
                self.assertEqual(self.client.get(f"/jobs/{job_id}").json()["status"], "queued")
                self.assertEqual(self.client.delete(f"/jobs/{job_id}").status_code, 204)
                revoke.assert_called_once_with(job_id, terminate=False)
                self.user_has_job.assert_called_with(auth.LOCAL_USER_ID, job_id)
            self.user_has_job.return_value = False
            self.assertEqual(self.client.get(f"/jobs/{job_id}").status_code, 403)
        self.assertEqual(self.client.get("/jobs").status_code, 401)

    def test_invalid_payloads_never_enqueue(self):
        bad_reach = {"HUC": "03020201", "ReachID": "8780625", "Streamflow": -1}
        payloads = [
            {"scenarios": scenarios}
            for scenarios in (
                [],
                [{"ScenarioID": "bad", "Reaches": []}],
                [{**PAYLOAD[0], "ScenarioID": "../../unsafe"}],
                [{"ScenarioID": "bad", "Reaches": [bad_reach]}],
                [{"ScenarioID": "bad", "Reaches": [{**bad_reach, "Streamflow": 1, "HUC": "123"}]}],
                [{**PAYLOAD[0], "unexpected": True}],
                PAYLOAD * 2,
            )
        ] + [{}, {"duration": 1}]
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            for payload in payloads:
                with self.subTest(payload=payload):
                    self.assertEqual(
                        self.client.post(HAND_FIM_ROUTE, json=payload).status_code, 422
                    )
            enqueue.assert_not_called()

    def test_nonfinite_flow_is_rejected(self):
        for flow in (float("inf"), float("nan")):
            with self.subTest(flow=flow), self.assertRaises(ValidationError):
                HandFimParameters.model_validate(
                    [{**PAYLOAD[0], "Reaches": [{**PAYLOAD[0]["Reaches"][0], "Streamflow": flow}]}]
                )

    def test_broker_failure_is_503(self):
        with patch.object(tasks.hand_fim, "apply_async", side_effect=OperationalError("offline")):
            response = self.client.post(HAND_FIM_ROUTE, json=SUBMISSION)
        self.assertEqual(response.status_code, 503)
        self.untrack_user_job.assert_called_once()
        self.delete_job_credentials.assert_called_once()

    def test_credential_failure_never_enqueues(self):
        self.create_job_credentials.side_effect = storage.StorageCredentialsError("Credential exchange failed")
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.client.post(HAND_FIM_ROUTE, json=SUBMISSION)
        self.assertEqual(response.status_code, 503)
        enqueue.assert_not_called()
        self.track_user_job.assert_not_called()

    def test_other_users_cannot_access_jobs(self):
        self.user_has_job.return_value = False
        job_id = str(uuid4())
        with patch.object(storage, "s3_client") as client, patch.object(main.celery_app, "AsyncResult") as result:
            for method, route in (
                ("GET", f"/jobs/{job_id}"),
                ("GET", f"/jobs/{job_id}/logs"),
                ("DELETE", f"/jobs/{job_id}"),
            ):
                self.assertEqual(self.client.request(method, route).status_code, 403)
            client.assert_not_called()
            result.assert_not_called()

    def test_poll_job_logs_returns_status_and_next_offset(self):
        job_id = str(uuid4())
        with patch.object(main, "read_job_logs", return_value=("worker output\n", 14)) as read_logs, patch.object(
            main.celery_app, "AsyncResult", return_value=Mock(state="STARTED"),
        ):
            response = self.client.get(f"/jobs/{job_id}/logs?offset=5&max_bytes=20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "job_id": job_id,
            "status": "started",
            "logs": "worker output\n",
            "next_offset": 14,
        })
        read_logs.assert_called_once_with(job_id, 5, 20)

    def test_job_logs_reject_invalid_offsets_and_limits(self):
        job_id = str(uuid4())
        for query in ("offset=-1", "max_bytes=3", "max_bytes=1048577"):
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(f"/jobs/{job_id}/logs?{query}").status_code,
                    422,
                )

    def test_job_logs_read_utf8_chunks_without_splitting_characters(self):
        client = Mock()
        client.strlen.return_value = len("hé\n".encode("utf-8"))
        client.getrange.side_effect = [
            "hé\n".encode("utf-8")[:2],
            "hé\n".encode("utf-8")[1:],
        ]
        with patch.object(job_logs, "get_redis_client", return_value=client) as get_client:
            first = job_logs.read_job_logs(str(uuid4()), 0, 2)
            second = job_logs.read_job_logs(str(uuid4()), first[1], 4)
        self.assertEqual(first, ("h", 1))
        self.assertEqual(second, ("é\n", 4))
        self.assertEqual(client.getrange.call_count, 2)
        get_client.assert_called_with(decode_responses=False)

    def test_other_users_cannot_read_job_logs(self):
        self.user_has_job.return_value = False
        job_id = str(uuid4())
        with patch.object(main, "read_job_logs") as read_logs, patch.object(
            main.celery_app, "AsyncResult",
        ) as result:
            self.assertEqual(self.client.get(f"/jobs/{job_id}/logs").status_code, 403)
        read_logs.assert_not_called()
        result.assert_not_called()

    def test_list_jobs_for_authenticated_user(self):
        states = {
            "PENDING": "queued", "RECEIVED": "queued", "STARTED": "started",
            "RETRY": "started", "SUCCESS": "success", "FAILURE": "failure",
            "REVOKED": "revoked", "CUSTOM": "custom",
        }
        with patch.object(main, "list_user_jobs", return_value=list(states)) as jobs, patch.object(
            main.celery_app, "AsyncResult", side_effect=lambda job_id: Mock(state=job_id),
        ) as results:
            self.assertEqual(self.client.get("/jobs").json(), {
                "jobs": [{"job_id": state, "status": status} for state, status in states.items()],
            })
            jobs.assert_called_once_with("alice")
            self.assertEqual(results.call_count, len(states))

    def test_empty_job_list_does_not_fetch_task_statuses(self):
        with patch.object(main, "list_user_jobs", return_value=[]), patch.object(
            main.celery_app, "AsyncResult",
        ) as results:
            self.assertEqual(self.client.get("/jobs").json(), {"jobs": []})
            results.assert_not_called()

    def test_status_returns_output_metadata(self):
        job_id = str(uuid4())
        result = {
            **DESTINATION, "local_output_dir": None,
            "files": [{"path": "result.cog", "key": "data/output/result.cog", "size_bytes": 10}],
            "outputs": [{"ScenarioID": "single-reach-scenario"}],
        }
        with patch.object(
            main.celery_app, "AsyncResult", return_value=Mock(state="SUCCESS", result=result)
        ):
            response = self.client.get(f"/jobs/{job_id}")
            self.assertEqual(response.json()["result"], result)

    def test_output_download_and_envelope_routes_are_removed(self):
        job_id = str(uuid4())
        for route in (
            f"/jobs/{job_id}/outputs/0.tif",
            f"/jobs/{job_id}/files/0",
            f"/jobs/{job_id}/files/0/content",
        ):
            self.assertEqual(self.client.get(route).status_code, 404)
        self.assertEqual(self.client.post("/jobs", json={"payload": PAYLOAD}).status_code, 405)

    def test_invalid_destination_is_rejected(self):
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            for body in (
                {"scenarios": PAYLOAD, "output_prefix": "orphan"},
                {"scenarios": PAYLOAD, "output_bucket": ""},
            ):
                self.assertEqual(self.client.post(HAND_FIM_ROUTE, json=body).status_code, 422)
            enqueue.assert_not_called()

    def test_local_submission_skips_s3_credentials(self):
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.client.post("/jobs/hand-fim", json={"scenarios": PAYLOAD})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(enqueue.call_args.kwargs["args"][2:], [None, "", None])
        self.create_job_credentials.assert_not_called()

    def test_max_procs_is_validated_and_queued(self):
        with patch.object(tasks.hand_fim, "apply_async") as enqueue:
            response = self.client.post(
                HAND_FIM_ROUTE, json={"scenarios": PAYLOAD, "max_procs": 3}
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(enqueue.call_args.kwargs["args"][-1], 3)
            enqueue.reset_mock()
            for value in (0, -1, "invalid", 1.5):
                self.assertEqual(
                    self.client.post(
                        HAND_FIM_ROUTE, json={"scenarios": PAYLOAD, "max_procs": value}
                    ).status_code,
                    422,
                )
            enqueue.assert_not_called()

    def test_failure_status_and_cancellation(self):
        job_id = str(uuid4())
        with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="FAILURE", result=RuntimeError("failed"))):
            self.assertEqual(self.client.get(f"/jobs/{job_id}").json()["error"], "failed")
            self.assertEqual(self.client.delete(f"/jobs/{job_id}").status_code, 409)
        with patch.object(main.celery_app, "AsyncResult", return_value=Mock(state="PENDING")), patch.object(main.celery_app.control, "revoke") as revoke:
            self.assertEqual(self.client.delete(f"/jobs/{job_id}").status_code, 204)
            revoke.assert_called_once_with(job_id, terminate=False)
        self.assertEqual(self.client.get("/jobs/not-a-uuid").status_code, 422)

    def test_openapi_uses_scenario_array(self):
        schema = self.client.get("/openapi.json").json()
        self.assertFalse(any(path.startswith("/auth/") for path in schema["paths"]))
        self.assertEqual(set(schema["paths"]["/jobs"]), {"get"})
        self.assertNotIn("JobRequest", schema["components"]["schemas"])
        self.assertNotIn("/jobs/{job_id}/outputs/{index}.tif", schema["paths"])
        self.assertNotIn("/jobs/{job_id}/files/{index}", schema["paths"])
        self.assertNotIn("/jobs/{job_id}/files/{index}/content", schema["paths"])
        self.assertEqual(
            set(schema["components"]["schemas"]["HandFimOutput"]["properties"]),
            {"ScenarioID"},
        )
        self.assertNotIn("download_url", schema["components"]["schemas"]["OutputFile"]["properties"])
        parameters = schema["components"]["schemas"]["HandFimParameters"]
        self.assertEqual(parameters["type"], "array")
        self.assertEqual(parameters["minItems"], 1)
        reach = schema["components"]["schemas"]["Reach"]["properties"]
        self.assertEqual(reach["HUC"]["examples"], ["03020201"])
        self.assertEqual(reach["ReachID"]["examples"], ["8780625"])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.job_id = str(uuid4())
        patcher = patch.object(tasks, "s3_client")
        self.storage_client = patcher.start().return_value
        self.addCleanup(patcher.stop)
        for name, value in (
            ("hand_fim_input_dir", self.root / "input"),
            ("hand_fim_output_dir", self.root / "output"),
            ("hand_fim_entry", self.root / "entry.py"),
        ):
            patcher = patch.object(tasks.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("clear_job_logs", "append_job_log"):
            patcher = patch.object(tasks, name)
            mock = patcher.start()
            setattr(self, name, mock)
            self.addCleanup(patcher.stop)

    def __write_output(self, command, **kwargs):
        source = Path(command[3])
        scenario = json.loads(source.read_text())[0]
        self.assertEqual(scenario["ScenarioID"], PAYLOAD[0]["ScenarioID"])
        self.assertEqual(scenario["Reaches"], PAYLOAD[0]["Reaches"])
        self.assertTrue(kwargs["check"])
        output = (
            self.root / "output" / "scenarios" / self.job_id
            / f"{scenario['ScenarioID']}.cog"
        )
        output.write_bytes(b"test-cog")

    def __spawn(self, command, **_kwargs):
        self.__write_output(command, check=True)
        return Mock(stdout=io.StringIO("worker output\n"), wait=Mock(return_value=0))

    def test_executes_scenario_and_preserves_original_identifier(self):
        with patch.object(tasks.subprocess, "Popen", side_effect=self.__spawn) as run:
            result = tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION)
        output_path = (
            self.root / "output" / "scenarios" / self.job_id
            / f"{PAYLOAD[0]['ScenarioID']}.cog"
        )
        self.assertEqual(result["outputs"][0]["ScenarioID"], PAYLOAD[0]["ScenarioID"])
        self.assertEqual(result["outputs"], [{"ScenarioID": PAYLOAD[0]["ScenarioID"]}])
        self.assertEqual(result["output_bucket"], "fim-output")
        self.assertEqual(result["output_prefix"], "data/output")
        self.storage_client.upload_file.assert_called_once_with(
            str(output_path),
            "fim-output", f"data/output/{PAYLOAD[0]['ScenarioID']}.cog",
            ExtraArgs={"ContentType": "image/tiff"},
        )
        self.assertEqual(run.call_args.args[0][-3:], [
            str(self.root / "input" / "jobs" / self.job_id / "scenario.json"), "1", self.job_id,
        ])
        self.append_job_log.assert_any_call(self.job_id, "worker output\n")

    def test_uploads_all_job_files_preserving_paths_and_excluding_shared_data(self):
        output_dir = self.root / "output" / "scenarios" / self.job_id
        output_dir.mkdir(parents=True)
        (output_dir / "stale.txt").write_text("old attempt")
        shared = self.root / "output" / "flood_03020201"
        shared.mkdir()
        (shared / "cache.csv").write_text("shared data")

        def compute(command, **kwargs):
            self.__write_output(command, check=True)
            (output_dir / "logs").mkdir()
            (output_dir / "logs" / "run.txt").write_text("log")
            (output_dir / f"{PAYLOAD[0]['ScenarioID']}.tif").write_bytes(b"raster")
            return Mock(stdout=io.StringIO("worker output\n"), wait=Mock(return_value=0))

        with patch.object(tasks.subprocess, "Popen", side_effect=compute):
            result = tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION)
        self.assertEqual(
            {file["path"] for file in result["files"]},
            {f"{PAYLOAD[0]['ScenarioID']}.cog", f"{PAYLOAD[0]['ScenarioID']}.tif", "logs/run.txt"},
        )
        self.assertEqual(self.storage_client.upload_file.call_count, 3)
        for file in result["files"]:
            self.assertEqual(file["key"], f"data/output/{file['path']}")
            self.assertNotIn("download_url", file)
        self.assertFalse((output_dir / "stale.txt").exists())
        self.assertTrue((shared / "cache.csv").exists())

    def test_subprocess_failure_propagates(self):
        with patch.object(
            tasks.subprocess, "Popen",
            return_value=Mock(stdout=io.StringIO("failed\n"), wait=Mock(return_value=1)),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION)

    def test_max_procs_reaches_container_and_uses_worker_default_when_omitted(self):
        with patch.object(tasks.settings, "hand_fim_max_procs", 2):
            for max_procs, expected in ((None, "2"), (4, "4")):
                with patch.object(tasks.subprocess, "Popen", side_effect=self.__spawn) as run:
                    tasks.hand_fim.run(self.job_id, PAYLOAD, max_procs=max_procs)
                self.assertEqual(run.call_args.args[0][-2], expected)
        with patch.object(tasks.subprocess, "Popen") as run:
            with self.assertRaises(ValidationError):
                tasks.hand_fim.run(self.job_id, PAYLOAD, max_procs=0)
            run.assert_not_called()

    def test_local_outputs_stay_on_mount_without_s3_client(self):
        with patch.object(tasks.subprocess, "Popen", side_effect=self.__spawn):
            result = tasks.hand_fim.run(self.job_id, PAYLOAD)
        self.assertIsNone(result["output_bucket"])
        self.assertIsNone(result["files"][0]["key"])
        output_dir = self.root / "output" / "scenarios" / self.job_id
        self.assertEqual(result["local_output_dir"], str(output_dir))
        self.assertTrue((output_dir / f"{PAYLOAD[0]['ScenarioID']}.cog").is_file())
        self.storage_client.upload_file.assert_not_called()

    def test_output_symlink_is_rejected_without_uploading_external_data(self):
        external = self.root / "secret.txt"
        external.write_text("not a job output")

        def compute(command, **kwargs):
            self.__write_output(command, check=True)
            output_dir = self.root / "output" / "scenarios" / self.job_id
            (output_dir / "linked.txt").symlink_to(external)
            return Mock(stdout=io.StringIO("worker output\n"), wait=Mock(return_value=0))

        with patch.object(tasks.subprocess, "Popen", side_effect=compute):
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION)
        self.storage_client.upload_file.assert_not_called()
        self.assertEqual(external.read_text(), "not a job output")

    def test_missing_output_and_stale_output_fail(self):
        output = (
            self.root / "output" / "scenarios" / self.job_id
            / f"{PAYLOAD[0]['ScenarioID']}.cog"
        )
        output.parent.mkdir(parents=True)
        output.write_bytes(b"stale")
        with patch.object(
            tasks.subprocess, "Popen",
            return_value=Mock(stdout=io.StringIO(), wait=Mock(return_value=0)),
        ):
            with self.assertRaisesRegex(RuntimeError, "produced no COG"):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION)
        self.assertFalse(output.exists())

    def test_worker_revalidates_before_execution(self):
        with patch.object(tasks.subprocess, "Popen") as run:
            with self.assertRaises(ValidationError):
                tasks.hand_fim.run(self.job_id, [], **DESTINATION)
            run.assert_not_called()

    def test_upload_failure_propagates(self):
        self.storage_client.upload_file.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "PutObject"
        )
        with patch.object(tasks.subprocess, "Popen", side_effect=self.__spawn):
            with self.assertRaises(ClientError):
                tasks.hand_fim.run(self.job_id, PAYLOAD, **DESTINATION)


class StorageTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(storage, "__job_credentials", side_effect=lambda job_id: credentials_fixture())
        self.job_credentials = patcher.start()
        self.addCleanup(patcher.stop)

    def test_output_file_keys_use_prefix_and_relative_path(self):
        self.assertEqual(
            storage.output_file_key("/data/output/", "logs/run.txt"),
            "data/output/logs/run.txt",
        )
        with self.assertRaisesRegex(storage.StorageCredentialsError, "1024"):
            storage.output_file_key("prefix", "x" * 1024)

    def test_s3_compatible_client_configuration(self):
        with patch.object(storage.boto3, "client") as client, patch.object(
            storage.settings, "s3_endpoint_url", "https://objects.example"
        ):
            storage.s3_client(uuid4())
        kwargs = client.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], "https://objects.example")
        self.assertEqual(kwargs["config"].signature_version, "s3v4")
        self.assertEqual(kwargs["config"].s3["addressing_style"], "path")
        self.assertEqual(kwargs["aws_access_key_id"], "user:test-access")
        self.assertEqual(kwargs["aws_secret_access_key"], "test-secret")
        self.assertEqual(kwargs["aws_session_token"], "")

    def test_multibyte_prefix_does_not_exceed_object_key_limit(self):
        with self.assertRaises(ValidationError):
            OutputDestination(output_bucket="bucket", output_prefix="\u00e9" * 451)

class HydroShareCredentialTests(unittest.TestCase):
    def setUp(self):
        self.job_id = uuid4()
        self.session = auth.UserSession(
            user_id="alice", access_token="hydroshare-token",
            scope="read write", expires_at=time.time() + 60,
        )
        self.redis = Mock()
        patcher = patch.object(storage, "get_redis_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def __provider(self, response):
        patcher = patch.object(storage.httpx, "Client")
        factory = patcher.start()
        self.addCleanup(patcher.stop)
        client = factory.return_value.__enter__.return_value
        client.post.return_value = response
        return client

    def test_exchange_stores_temporary_credentials_only_in_redis(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        client = self.__provider(httpx.Response(
            201, json={"access_key": "alice:key", "secret_key": "temporary-secret", "expiry": expiry.isoformat()},
            request=httpx.Request("POST", "https://provider.example/credentials"),
        ))
        storage.create_job_credentials(self.job_id, self.session)
        client.post.assert_called_once_with(
            storage.settings.hydroshare_s3_credentials_url,
            headers={"Authorization": "Bearer hydroshare-token"},
            json={"expiry": storage.settings.hydroshare_s3_credentials_expiry_days},
        )
        key, raw = self.redis.set.call_args.args
        self.assertEqual(key, storage.JOB_CREDENTIALS_PREFIX + str(self.job_id))
        self.assertEqual(json.loads(raw)["secret_key"], "temporary-secret")
        self.assertNotIn("hydroshare-token", raw)
        self.assertGreater(self.redis.set.call_args.kwargs["ex"], 0)
        self.redis.get.return_value = raw
        with patch.object(storage.boto3, "client") as factory:
            storage.s3_client(self.job_id)
        self.assertEqual(factory.call_args.kwargs["aws_access_key_id"], "alice:key")
        self.assertEqual(factory.call_args.kwargs["endpoint_url"], "https://s3.hydroshare.org")
        storage.delete_job_credentials(self.job_id)
        self.redis.delete.assert_called_once_with(key)

    def test_exchange_errors_never_leak_response_or_token(self):
        client = self.__provider(httpx.Response(
            401, text="sensitive-token", request=httpx.Request("POST", "https://provider.example"),
        ))
        with self.assertRaisesRegex(storage.StorageCredentialsError, "HTTP 401") as raised:
            storage.create_job_credentials(self.job_id, self.session)
        self.assertNotIn("sensitive-token", str(raised.exception))
        self.redis.set.assert_not_called()
        for body in (
            {"access_key": "", "secret_key": "", "expiry": datetime.now(timezone.utc).isoformat()},
            {"access_key": "key", "secret_key": "secret"},
        ):
            client.post.return_value = httpx.Response(
                201, json=body, request=httpx.Request("POST", "https://provider.example"),
            )
            with self.assertRaises(storage.StorageCredentialsError):
                storage.create_job_credentials(self.job_id, self.session)
        self.redis.set.assert_not_called()

    def test_missing_credentials_do_not_fall_back_to_environment(self):
        self.redis.get.return_value = None
        with patch.object(storage.boto3, "client") as client:
            with self.assertRaisesRegex(storage.StorageCredentialsError, "missing or expired"):
                storage.s3_client(self.job_id)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
