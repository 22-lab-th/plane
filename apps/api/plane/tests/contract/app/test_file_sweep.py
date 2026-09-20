# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The unverified-object sweep and the resurrection guard (AC-20, T-118).

Every object claim is made through an independent boto3 client rather than the
app's adapter, and every job is run as the real function the schedule runs (never
``.delay()``: the queue is not part of this environment). The two jobs are the
deletion mechanism for objects with no verified version - the sweep - and the guard
against a PUT that outlives it - the re-check.

The order of the assertions is the order of the contract: the row must stop being
listed, the object must be gone **by its exact key**, the reservation must be
released **once**, and the row must carry the terminal marker so nothing sweeps it
twice. A test that asserted the row first could let "row marked without object" pass.
"""

# Python imports
import io
import requests
from datetime import timedelta
from unittest import mock

# Django imports
from django.conf import settings
from django.utils import timezone

# Third party imports
import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.bgtasks.file_sweep_task import cleanup_unverified_objects, recheck_deleted_objects
from plane.db.models import (
    FileObject,
    FileVersion,
    Project,
    ProjectMember,
    ProjectStorageUsage,
    StorageQuota,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/complete-upload/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    """Sign browser-facing URLs against the reachable test endpoint (see T-102)."""
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def independent_store():
    """A second boto3 client: object claims must not go through the app's adapter."""
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION or "us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def object_exists(store, key):
    """True when the exact key is stored; a missing key is a real answer, not an error."""
    try:
        store.head_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


@pytest.fixture
def stored_objects():
    """Delete whatever objects a test stored; the database rolls back, MinIO does not."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Sweep Workspace", slug="sweep-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Sweep Project", identifier="SWEP", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def _aged(version, *, hours=20):
    """Back-date an attempt past the URL TTL plus the margin, as an abandoned one is.

    Both fields the predicate reads move together: a real attempt that was abandoned
    has an expired reservation **and** a ``status_changed_at`` older than the window,
    and the sweep refuses either half alone.
    """
    when = timezone.now() - timedelta(hours=hours)
    FileVersion.objects.filter(pk=version.pk).update(
        reservation_expires_at=when, status_changed_at=when
    )
    version.refresh_from_db()


def _initiate(session_client, project, *, name="sweep.pdf", size_bytes=len(PDF_BYTES), file_id=None):
    payload = {"file_name": name, "size_bytes": size_bytes, "mime_type": "application/pdf"}
    if file_id is not None:
        payload["file_id"] = str(file_id)

    response = session_client.post(
        upload_url(project.workspace.slug, project.id),
        payload,
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK, response.data
    return response


def _store_at_signed_url(initiated, *, data=PDF_BYTES, stored_objects=None):
    """PUT the bytes at the key the presign named, the way a browser does."""
    upload = initiated.data["upload"]
    response = requests.put(upload["url"], data=data, headers=upload["headers"], timeout=30)
    assert response.status_code == 200, response.content[:200]
    if stored_objects is not None:
        stored_objects.append(initiated.data["file"]["object_key"])
    return response


def _listing_ids(session_client, project):
    response = session_client.get(files_url(project.workspace.slug, project.id))
    assert response.status_code == status.HTTP_200_OK, response.data
    return [row["id"] for row in response.data["results"]]


def _reserved_bytes(project):
    return (
        ProjectStorageUsage.objects.get(project=project).reserved_bytes,
        StorageQuota.objects.get(workspace=project.workspace).reserved_bytes,
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestCleanupUnverifiedObjects:
    """AC-20: an object with no verified version is deleted, released and never listed."""

    def test_an_abandoned_attempt_is_swept_by_its_exact_key_and_its_reservation_released(
        self, session_client, project, stored_objects, independent_store
    ):
        initiated = _initiate(session_client, project)
        file_id = initiated.data["file"]["id"]
        object_key = initiated.data["file"]["object_key"]
        _store_at_signed_url(initiated, stored_objects=stored_objects)

        # A second object under the same prefix, stored by someone else's attempt: an
        # exact-key deletion must leave it alone, which a prefix listing could not
        # (AD-13: "the sweep deletes exact stored keys, never lists the bucket").
        sibling_key = f"{object_key}.neighbour"
        assert S3Storage().upload_file(
            io.BytesIO(PDF_BYTES), sibling_key, content_type="application/pdf"
        ) is True
        stored_objects.append(sibling_key)

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert _reserved_bytes(project) == (len(PDF_BYTES), len(PDF_BYTES))
        _aged(version)

        # Never listed, even before the sweep removes the object.
        assert file_id not in _listing_ids(session_client, project)

        summary = cleanup_unverified_objects(batch_size=10)

        assert summary == {"swept": 1, "failed": 0, "reservations_released": 1, "scanned": 1}
        assert object_exists(independent_store, object_key) is False
        assert object_exists(independent_store, sibling_key) is True

        version.refresh_from_db()
        assert version.status == FileVersion.Status.FAILED
        assert version.object_deleted_at is not None
        assert version.reservation_released_at is not None
        assert version.reserved_bytes == 0
        assert version.is_active is False

        # The guarded release moved each counter exactly once, to zero.
        assert _reserved_bytes(project) == (0, 0)

        # And the file stays invisible on the listing while its detail still answers:
        # the client that is retrying this upload reads the failure there (AC-04).
        assert file_id not in _listing_ids(session_client, project)
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK, detail.data
        assert detail.data["version"] is None
        assert [item["status"] for item in detail.data["versions"]] == [FileVersion.Status.FAILED]

        # The terminal marker is what makes a second run a no-op: no second deletion,
        # no second release, no counter moved again (single-fire, ARCH-001 §2.8 item 3).
        second = cleanup_unverified_objects(batch_size=10)

        assert second == {"swept": 0, "failed": 0, "reservations_released": 0, "scanned": 0}
        assert _reserved_bytes(project) == (0, 0)

    def test_a_retry_after_the_sweep_stores_a_new_version_and_the_file_is_listed_again(
        self, session_client, project, stored_objects, independent_store
    ):
        """The sweep does not strand the file: the client's remedy is a fresh presign."""
        initiated = _initiate(session_client, project)
        file_id = initiated.data["file"]["id"]
        first_key = initiated.data["file"]["object_key"]
        _store_at_signed_url(initiated, stored_objects=stored_objects)
        _aged(FileVersion.objects.get(file_id=file_id, version_no=1))

        assert cleanup_unverified_objects(batch_size=10)["swept"] == 1
        assert object_exists(independent_store, first_key) is False
        assert file_id not in _listing_ids(session_client, project)

        # The remedy after the window has passed is a new presign for the same file id
        # (ARCH-001 §2.8 item 4), and the swept row must not stand in its way.
        retry = _initiate(session_client, project, file_id=file_id)
        assert retry.data["version_no"] == 2
        retry_key = FileVersion.objects.get(file_id=file_id, version_no=2).object_key
        assert retry_key != first_key
        _store_at_signed_url(retry, stored_objects=None)
        stored_objects.append(retry_key)

        finished = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 2, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert finished.status_code == status.HTTP_200_OK, finished.data
        assert finished.data["version"]["status"] == FileVersion.Status.ACTIVE
        # The file is a listed file again, and its history keeps the swept attempt.
        assert file_id in _listing_ids(session_client, project)
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert [item["status"] for item in detail.data["versions"]] == [
            FileVersion.Status.ACTIVE,
            FileVersion.Status.FAILED,
        ]
        assert detail.data["file"]["size_bytes"] == len(PDF_BYTES)
        # The retry settled the reservation into usage, so the sweep's release did not
        # leave the counters behind.
        assert ProjectStorageUsage.objects.get(project=project).used_bytes == len(PDF_BYTES)

    def test_a_size_mismatch_leaves_the_object_for_the_sweep_which_releases_nothing_again(
        self, session_client, project, stored_objects, independent_store
    ):
        """The DEFECT-001 shape, end to end: refused at finalize, hidden, then swept."""
        initiated = _initiate(session_client, project)
        file_id = initiated.data["file"]["id"]
        object_key = initiated.data["file"]["object_key"]
        # One byte more than declared: the store is never trusted, and the mismatch is
        # what makes the object unverifiable.
        _store_at_signed_url(initiated, data=PDF_BYTES + b"x", stored_objects=stored_objects)

        refused = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "size_mismatch"

        # The failed finalize already released the reservation and recorded the
        # mismatch; the object is left for the sweep (ARCH-001 §2.4 "Failure").
        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert version.status == FileVersion.Status.FAILED
        assert version.object_deleted_at is None
        assert version.reservation_released_at is not None
        assert _reserved_bytes(project) == (0, 0)
        assert object_exists(independent_store, object_key) is True

        assert file_id not in _listing_ids(session_client, project)
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK, detail.data
        assert detail.data["versions"][0]["status"] == FileVersion.Status.FAILED

        _aged(version)
        summary = cleanup_unverified_objects(batch_size=10)

        # ``reservations_released`` is 0 on purpose: the failed finalize ended the
        # attempt first, so the sweep's guarded statement matched no row and no
        # counter moved twice.
        assert summary == {"swept": 1, "failed": 0, "reservations_released": 0, "scanned": 1}
        assert object_exists(independent_store, object_key) is False

        version.refresh_from_db()
        assert version.object_deleted_at is not None
        assert _reserved_bytes(project) == (0, 0)

        # The listing stays empty and the mismatch record stays readable.
        assert file_id not in _listing_ids(session_client, project)
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK, detail.data
        assert detail.data["versions"][0]["status"] == FileVersion.Status.FAILED
        assert version.storage_metadata["failure"]["code"] == "size_mismatch"

    def test_a_live_attempt_and_a_verified_version_are_out_of_scope(
        self, session_client, project, stored_objects, independent_store
    ):
        """The age guard: an attempt inside its window is not racing its own PUT."""
        live = _initiate(session_client, project, name="live.pdf")
        live_key = live.data["file"]["object_key"]
        _store_at_signed_url(live, stored_objects=stored_objects)

        verified = _initiate(session_client, project, name="verified.pdf")
        verified_key = verified.data["file"]["object_key"]
        _store_at_signed_url(verified, stored_objects=stored_objects)
        finished = session_client.post(
            complete_url(project.workspace.slug, project.id, verified.data["file"]["id"]),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )
        assert finished.status_code == status.HTTP_200_OK, finished.data
        # An ancient verified version is still not the sweep's business: it has an
        # object a row claims and can serve.
        FileVersion.objects.filter(
            file_id=verified.data["file"]["id"], version_no=1
        ).update(status_changed_at=timezone.now() - timedelta(days=30))

        summary = cleanup_unverified_objects(batch_size=10)

        assert summary == {"swept": 0, "failed": 0, "reservations_released": 0, "scanned": 0}
        assert object_exists(independent_store, live_key) is True
        assert object_exists(independent_store, verified_key) is True
        assert FileVersion.objects.get(file_id=live.data["file"]["id"]).status == FileVersion.Status.UPLOADING
        assert _reserved_bytes(project)[0] == len(PDF_BYTES)
        assert verified.data["file"]["id"] in _listing_ids(session_client, project)

    def test_a_failed_deletion_leaves_the_row_untouched_for_the_next_run(
        self, session_client, project, stored_objects, independent_store
    ):
        """A sweep that could not delete must not look like one that did."""
        initiated = _initiate(session_client, project)
        file_id = initiated.data["file"]["id"]
        object_key = initiated.data["file"]["object_key"]
        _store_at_signed_url(initiated, stored_objects=stored_objects)
        _aged(FileVersion.objects.get(file_id=file_id, version_no=1))

        with mock.patch.object(S3Storage, "delete_files", return_value=False):
            summary = cleanup_unverified_objects(batch_size=10)

        assert summary == {"swept": 0, "failed": 1, "reservations_released": 0, "scanned": 1}
        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert version.object_deleted_at is None
        assert version.status == FileVersion.Status.UPLOADING
        assert version.reservation_released_at is None
        assert object_exists(independent_store, object_key) is True
        assert _reserved_bytes(project)[0] == len(PDF_BYTES)

        # The next run retries the very same row and succeeds.
        assert cleanup_unverified_objects(batch_size=10)["swept"] == 1
        assert object_exists(independent_store, object_key) is False
        assert _reserved_bytes(project) == (0, 0)


@pytest.mark.contract
@pytest.mark.django_db
class TestRecheckDeletedObjects:
    """AC-20: an object a late PUT recreated under a marked row is deleted again."""

    def test_a_recreated_object_is_removed_after_the_window_and_counted(
        self, session_client, project, stored_objects, independent_store
    ):
        initiated = _initiate(session_client, project)
        file_id = initiated.data["file"]["id"]
        object_key = initiated.data["file"]["object_key"]
        _store_at_signed_url(initiated, stored_objects=stored_objects)
        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        _aged(version)
        assert cleanup_unverified_objects(batch_size=10)["swept"] == 1

        # A PUT that started before the URL expired and finished after the sweep: the
        # object is stored again under a row whose marker is already set.
        assert S3Storage().upload_file(
            io.BytesIO(PDF_BYTES), object_key, content_type="application/pdf"
        ) is True
        assert object_exists(independent_store, object_key) is True

        # Inside the window the row is not re-checked at all.
        young = recheck_deleted_objects(batch_size=10)
        assert young == {"resurrection_detected": 0, "failed": 0, "checked": 0}
        assert object_exists(independent_store, object_key) is True

        FileVersion.objects.filter(pk=version.pk).update(
            object_deleted_at=timezone.now() - timedelta(hours=25)
        )

        summary = recheck_deleted_objects(batch_size=10)

        assert summary == {"resurrection_detected": 1, "failed": 0, "checked": 1}
        assert object_exists(independent_store, object_key) is False
        # The counters are unmoved: the sweep already released this attempt.
        assert _reserved_bytes(project) == (0, 0)
        assert file_id not in _listing_ids(session_client, project)

    def test_a_marked_row_whose_object_stayed_deleted_is_not_a_resurrection(
        self, session_client, project, stored_objects, independent_store
    ):
        initiated = _initiate(session_client, project)
        object_key = initiated.data["file"]["object_key"]
        _store_at_signed_url(initiated, stored_objects=stored_objects)
        version = FileVersion.objects.get(file_id=initiated.data["file"]["id"], version_no=1)
        _aged(version)
        assert cleanup_unverified_objects(batch_size=10)["swept"] == 1

        FileVersion.objects.filter(pk=version.pk).update(
            object_deleted_at=timezone.now() - timedelta(hours=25)
        )

        summary = recheck_deleted_objects(batch_size=10)

        # It was checked and there was nothing to remove: the counter must not claim
        # a resurrection that did not happen.
        assert summary == {"resurrection_detected": 0, "failed": 0, "checked": 1}
        assert object_exists(independent_store, object_key) is False
