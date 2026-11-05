import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

HANDFIM = Path(__file__).resolve().parents[1] / "docker" / "handfim"
sys.path.insert(0, str(HANDFIM))
spec = importlib.util.spec_from_file_location(
    "prepare_streaming_collection_test", HANDFIM / "prepare_streaming_collection.py",
)
collection = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(collection)


class FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, *, Bucket=None, Prefix="", **_kwargs):
        if not Prefix.startswith("hand_fim_"):
            return [{"Contents": [
                {"Key": key} for (bucket, key) in sorted(self.objects)
                if bucket == Bucket and key.startswith(Prefix)
            ]}]
        return [
            {"CommonPrefixes": [
                {"Prefix": "hand_fim_4_5_2_11/00000001/"},
                {"Prefix": "hand_fim_4_5_2_11/not-a-huc/"},
            ]},
            {"CommonPrefixes": [
                {"Prefix": "hand_fim_4_5_2_11/00000002/"},
                {"Prefix": "hand_fim_4_5_2_11/00000003/"},
            ]},
        ]


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.deleted = []

    def get_paginator(self, _operation):
        return FakePaginator(self.objects)

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, *, Bucket, Key, Body, **_kwargs):
        self.objects[(Bucket, Key)] = Body

    def delete_objects(self, *, Bucket, Delete, **_kwargs):
        for item in Delete["Objects"]:
            self.objects.pop((Bucket, item["Key"]), None)
            self.deleted.append(item["Key"])

    def upload_file(self, filename, bucket, key, **_kwargs):
        self.uploads.append(key)
        self.objects[(bucket, key)] = Path(filename).read_bytes()


class PrepareStreamingCollectionTests(unittest.TestCase):
    def test_prepares_assigned_hucs_and_publishes_marker_last(self):
        source = FakeS3()
        output = FakeS3()

        def fake_download(huc, huc_dir, **_kwargs):
            (huc_dir / "branches" / "0").mkdir(parents=True)

        def fake_prepare(huc, _huc_dir, output_dir, **_kwargs):
            huc_dir = output_dir / "hucs" / huc
            (huc_dir / "branches" / "0").mkdir(parents=True)
            (huc_dir / "branches" / "0" / "rem.tif").write_bytes(b"raster")
            (huc_dir / "reaches").mkdir()
            (huc_dir / "reaches" / "reach.parquet").write_bytes(b"reach")
            (huc_dir / "huc.parquet").write_bytes(b"complete")
            return {"branches": {"0": {}}}

        with (
            patch.object(collection.preparation, "download_huc", side_effect=fake_download),
            patch.object(collection.preparation, "prepare_huc", side_effect=fake_prepare),
        ):
            completed = collection.prepare_hucs(
                source_client=source, output_client=output, output_bucket="input-resource",
                output_prefix="hand-stream", task_index=0, task_count=2, log=lambda _msg: None,
            )

        self.assertEqual(completed, ["00000001", "00000003"])
        self.assertIn(("input-resource", "hand-stream/dataset.parquet"), output.objects)
        coverage = output.objects[("input-resource", "hand-stream/dataset-info.txt")].decode()
        self.assertIn("fim_version=4.5.2.11", coverage)
        self.assertIn("published_location=s3://input-resource/hand-stream/", coverage)
        self.assertIn("huc_count=3", coverage)
        for huc in completed:
            marker = f"hand-stream/hucs/{huc}/huc.parquet"
            self.assertIn(("input-resource", marker), output.objects)
            marker_upload = output.uploads.index(marker)
            self.assertFalse(any(
                key.startswith(f"hand-stream/hucs/{huc}/") for key in output.uploads[marker_upload + 1:]
            ))
            self.assertTrue(any(
                key.startswith(f"hand-stream/hucs/{huc}/") for key in output.uploads[:marker_upload]
            ))

    def test_persists_submission_provenance_and_reports_completed_hucs(self):
        source = FakeS3()
        output = FakeS3()
        completed_hucs = []
        context = {
            "job_id": "job-123",
            "dataset_name": "hand-45211",
            "submitted_by": "alice",
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "source_version": "4.5.2.11",
            "source_location": "s3://ciroh-owp-hand-fim/hand_fim_4_5_2_11/",
            "destination_location": "s3://input-resource/hand-stream",
            "all_hucs": False,
            "hucs": ["00000001", "00000002", "00000003"],
        }

        def fake_prepare(huc, _src, output_dir, **_kwargs):
            marker = output_dir / "hucs" / huc / collection.HUC_MARKER
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"complete")
            return {"branches": {"0": {}}}

        with (
            patch.object(collection.preparation, "download_huc"),
            patch.object(collection.preparation, "__branch_ids", return_value=["0"]),
            patch.object(collection.preparation, "prepare_huc", side_effect=fake_prepare),
        ):
            collection.prepare_hucs(
                source_client=source,
                output_client=output,
                output_bucket="input-resource",
                output_prefix="hand-stream",
                task_index=1,
                task_count=3,
                run_context=context,
                on_huc_complete=completed_hucs.append,
                log=lambda _msg: None,
            )

        coverage = output.objects[("input-resource", "hand-stream/dataset-info.txt")].decode()
        self.assertNotIn("preparation_job_id=job-123", coverage)
        provenance = output.objects[("input-resource", "hand-stream/preparations/job-123.json")].decode()
        self.assertIn('"submitted_by": "alice"', provenance)
        self.assertIn('"hucs": [', provenance)
        self.assertEqual(completed_hucs, ["00000002"])

    def test_select_hucs_filters_and_rejects_unavailable_hucs(self):
        available = ["00000001", "00000002", "00000003"]
        self.assertEqual(
            collection.select_hucs(available, ["00000003", "00000001"], False),
            ["00000001", "00000003"],
        )
        self.assertEqual(collection.select_hucs(available, [], True), available)
        with self.assertRaisesRegex(collection.preparation.PreparationError, "not available"):
            collection.select_hucs(available, ["00000004"], False)

    def test_rerun_replaces_existing_huc_prefix_only(self):
        source = FakeS3()
        output = FakeS3()
        stale = [
            "hand-stream/hucs/00000001/huc.parquet",
            "hand-stream/hucs/00000001/branches/0/rem.tif",
        ]
        other = ["hand-stream/hucs/00000010/huc.parquet", "hand-stream/hucs/00000001x/huc.parquet"]
        for key in stale + other:
            output.objects[("input-resource", key)] = b"old"

        def prepare_huc(huc, _huc_dir, output_dir, **_kwargs):
            marker = output_dir / "hucs" / huc / collection.HUC_MARKER
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"new")
            return {"branches": []}

        with (
            patch.object(collection.preparation, "download_huc"),
            patch.object(collection.preparation, "__branch_ids", create=True, return_value=[]),
            patch.object(collection.preparation, "prepare_huc", side_effect=prepare_huc),
        ):
            collection.prepare_single_huc(
                source_client=source, output_client=output, output_bucket="input-resource",
                output_prefix="hand-stream", huc="00000001", log=lambda _msg: None,
            )

        self.assertEqual(sorted(output.deleted), sorted(stale))
        self.assertNotIn(("input-resource", stale[1]), output.objects)
        self.assertEqual(output.objects[("input-resource", stale[0])], b"new")
        for key in other:
            self.assertEqual(output.objects[("input-resource", key)], b"old")

    def test_rejects_existing_dataset_with_different_source_version(self):
        source = FakeS3()
        output = FakeS3()
        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pylist([{
            "format": collection.preparation.DATASET_FORMAT,
            "version": collection.preparation.DATASET_VERSION,
            "block_size": 512,
            "fim_version": "4.5.2.10",
        }]), buffer)
        output.objects[("input-resource", "hand-stream/dataset.parquet")] = buffer.getvalue()

        with self.assertRaises(collection.preparation.PreparationError):
            collection.prepare_hucs(
                source_client=source, output_client=output, output_bucket="input-resource",
                output_prefix="hand-stream", task_index=0, task_count=3, log=lambda _msg: None,
            )

    def test_missing_source_branch_fails_without_publishing_huc_marker(self):
        source = FakeS3()
        output = FakeS3()

        def fake_download(huc, huc_dir, **_kwargs):
            (huc_dir / "branches" / "0").mkdir(parents=True)
            (huc_dir / "branches" / "1").mkdir()

        def fake_prepare(huc, _huc_dir, output_dir, **_kwargs):
            marker = output_dir / "hucs" / huc / "huc.parquet"
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"partial")
            return {"branches": {"0": {}}}

        with (
            patch.object(collection.preparation, "download_huc", side_effect=fake_download),
            patch.object(collection.preparation, "prepare_huc", side_effect=fake_prepare),
            self.assertRaises(collection.preparation.PreparationError),
        ):
            collection.prepare_hucs(
                source_client=source, output_client=output, output_bucket="input-resource",
                output_prefix="hand-stream", task_index=0, task_count=3, log=lambda _msg: None,
            )

        self.assertNotIn(("input-resource", "hand-stream/hucs/00000001/huc.parquet"), output.objects)

    def test_rejects_invalid_cloud_run_shard(self):
        with self.assertRaises(ValueError):
            collection.prepare_hucs(
                source_client=FakeS3(), output_client=FakeS3(), output_bucket="input-resource",
                output_prefix="hand-stream", task_index=3, task_count=3, log=lambda _msg: None,
            )


if __name__ == "__main__":
    unittest.main()
