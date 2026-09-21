# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The structured lifecycle records and the four counters (AC-31, R-NFR-5, T-120).

Every assertion here is made on what a *collector* would read, not on the code that
writes it: the records come from ``caplog`` (the same ``LogRecord`` attributes the
deployment's JSON formatter serialises) and the counters from
:func:`plane.utils.file_storage.observability.snapshot`. The tests go through the
real doors - the initiate/finalize/abort/purge endpoints, the sweep task - because a
record that only appears when a helper is called directly is not observability.

Both directions are asserted, which is what makes these tests falsifiable: a record
must be present when the event happened **and absent when it did not**, and a counter
must reach exactly 1 (not "at least 1") on one occurrence and stay at 0 on the
neighbouring case that must not be counted. Deleting any emission site in the product
code therefore fails a test here rather than passing quietly.

The counters are per process, so the autouse fixture resets them around every test;
without that, one test's increment would make the next one's zero assertion lie.
"""

# Python imports
import io
import logging
import requests
from datetime import timedelta
from unittest import mock

# Django imports
from django.utils import timezone

# Third party imports
import pytest
from rest_framework import status

# Module imports
from plane.bgtasks.file_sweep_task import cleanup_unverified_objects
from plane.db.models import (
    FileVersion,
    Project,
    ProjectMember,
    StorageQuota,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage
from plane.utils.file_storage import observability

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
MINIO_ENDPOINT = "http://test-minio:9000"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/complete-upload/"


def purge_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/purge/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def records(caplog, event=None, outcome=None):
    """The structured records captured so far, oldest first.

    Every record a test captures is checked against the contract keys on the way
    out, so a call site that omitted an identifier fails whichever test captures it
    instead of only the test that happens to assert that field. The keys are checked
    for presence, not for a value: ``None`` is a deliberate "not applicable" for a
    field the call site genuinely does not have (a purge of a file whose active
    pointer is already reconciled away).
    """
    found = []
    for entry in caplog.records:
        payload = getattr(entry, "file_record", None)
        if payload is None:
            continue
        missing = [key for key in observability.RECORD_KEYS if key not in payload]
        assert missing == [], f"{payload.get('event')} record is missing {missing}"
        if event is not None and payload["event"] != event:
            continue
        if outcome is not None and payload["outcome"] != outcome:
            continue
        found.append(payload)
    return found


def counter_stream(caplog, counter=None):
    """The counter records captured so far: how a collector adds these up."""
    found = []
    for entry in caplog.records:
        payload = getattr(entry, "file_counter", None)
        if payload is None:
            continue
        if counter is not None and payload["counter"] != counter:
            continue
        found.append(payload)
    return found


def counters():
    return observability.snapshot()


@pytest.fixture(autouse=True)
def observe(caplog):
    """Capture the domain logger at INFO, which is the level the records use.

    The test settings configure no logging, so the root logger's default level would
    drop an INFO record before any handler saw it.
    """
    caplog.set_level(logging.INFO, logger=observability.LOGGER_NAME)
    yield caplog


@pytest.fixture(autouse=True)
def clean_counters():
    observability.reset()
    yield
    observability.reset()


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", MINIO_ENDPOINT)
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", MINIO_ENDPOINT)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Observability Workspace", slug="obs-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Observability Project", identifier="OBSP", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


@pytest.fixture
def stored_objects():
    """Delete whatever objects a test stored; the database rolls back, MinIO does not."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


def _initiate(client, project, **overrides):
    payload = {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"}
    payload.update(overrides)
    return client.post(upload_url(project.workspace.slug, project.id), payload, format="json")


def _put_at_signed_url(initiated, *, data=PDF_BYTES, stored_objects=None):
    upload = initiated.data["upload"]
    response = requests.put(upload["url"], data=data, headers=upload["headers"], timeout=30)
    assert response.status_code == 200, response.content[:200]
    if stored_objects is not None:
        stored_objects.append(initiated.data["file"]["object_key"])
    return response


def _store_directly(key, *, content_type, data=PDF_BYTES, stored_objects=None):
    """Store an object without the signed URL, the way a hostile client would."""
    assert S3Storage().upload_file(io.BytesIO(data), key, content_type=content_type) is True
    if stored_objects is not None:
        stored_objects.append(key)
    return key


def _complete(client, project, initiated, *, size_bytes=len(PDF_BYTES), version_no=1):
    return client.post(
        complete_url(project.workspace.slug, project.id, initiated.data["file"]["id"]),
        {"version_no": version_no, "size_bytes": size_bytes},
        format="json",
    )


def _uploaded(client, project, stored_objects):
    """A file that exists, is verified and is visible: the starting point for purge."""
    initiated = _initiate(client, project)
    assert initiated.status_code == status.HTTP_200_OK, initiated.data
    _put_at_signed_url(initiated, stored_objects=stored_objects)
    complete = _complete(client, project, initiated)
    assert complete.status_code == status.HTTP_200_OK, complete.data
    return initiated


@pytest.mark.contract
@pytest.mark.django_db
class TestPresignRecord:
    """``file.presign``: one record per signing attempt, with the attempt's identities."""

    def test_a_signed_upload_emits_the_record_with_its_identities(self, session_client, project, observe):
        response = _initiate(session_client, project)

        assert response.status_code == status.HTTP_200_OK, response.data
        found = records(observe, observability.EVENT_PRESIGN)
        assert len(found) == 1, found

        record = found[0]
        assert record["outcome"] == observability.PRESIGN_SIGNED
        assert record["workspace_id"] == str(project.workspace_id)
        assert record["project_id"] == str(project.id)
        assert record["file_id"] == response.data["file"]["id"]
        assert record["version_no"] == 1
        # The evidence a reader needs to tie the URL to the object it signs.
        assert record["object_key"] == response.data["file"]["object_key"]
        assert record["size_bytes"] == len(PDF_BYTES)
        # …and never the signature itself (AD-15).
        assert "url" not in record and "X-Amz-Signature" not in str(record)

    def test_a_provider_that_cannot_sign_emits_the_same_event_with_its_own_outcome(
        self, session_client, project, observe
    ):
        with mock.patch.object(S3Storage, "generate_presigned_put", return_value=None):
            response = _initiate(session_client, project)

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.data["code"] == "storage_unavailable"

        found = records(observe, observability.EVENT_PRESIGN)
        assert len(found) == 1, found
        assert found[0]["outcome"] == observability.PRESIGN_STORAGE_UNAVAILABLE
        assert found[0]["file_id"] == str(FileVersion.objects.get().file_id)


@pytest.mark.contract
@pytest.mark.django_db
class TestFinalizeRecord:
    """``file.finalize``: the settled outcome, the refusal, and the replayed repeat."""

    def test_activation_is_recorded_with_the_verified_evidence(self, session_client, project, stored_objects, observe):
        initiated = _uploaded(session_client, project, stored_objects)

        found = records(observe, observability.EVENT_FINALIZE)
        assert len(found) == 1, found
        record = found[0]
        assert record["outcome"] == observability.FINALIZE_ACTIVATED
        assert record["workspace_id"] == str(project.workspace_id)
        assert record["project_id"] == str(project.id)
        assert record["file_id"] == initiated.data["file"]["id"]
        assert record["version_no"] == 1
        assert record["size_bytes"] == len(PDF_BYTES)
        assert record["mime_type"] == "application/pdf"
        assert record["object_key"] == initiated.data["file"]["object_key"]
        # A first upload activates, so nothing before it was displaced.
        assert record["magic_bytes_match"] is True

    def test_a_size_mismatch_is_recorded_and_moves_both_counters(
        self, session_client, project, stored_objects, observe
    ):
        initiated = _initiate(session_client, project)
        longer = PDF_BYTES + b"trailing bytes the declaration never mentioned"
        _put_at_signed_url(initiated, data=longer, stored_objects=stored_objects)

        refused = _complete(session_client, project, initiated)

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "size_mismatch"

        found = records(observe, observability.EVENT_FINALIZE)
        assert len(found) == 1, found
        assert found[0]["outcome"] == observability.FINALIZE_FAILED
        assert found[0]["code"] == "size_mismatch"
        assert found[0]["file_id"] == initiated.data["file"]["id"]
        assert found[0]["version_no"] == 1
        # The numbers that disagreed travel with the record.
        assert found[0]["observed_size"] == len(longer)
        assert found[0]["declared_size"] == len(PDF_BYTES)

        assert counters()[observability.UPLOAD_FAILURES] == 1
        assert counters()[observability.VERIFICATION_MISMATCHES] == 1
        # …and each increment is on the log stream, which is the transport: a
        # collector that never reads this process's memory still sees the number.
        streamed = counter_stream(observe, observability.VERIFICATION_MISMATCHES)
        assert len(streamed) == 1, streamed
        assert streamed[0]["value"] == 1
        assert streamed[0]["code"] == "size_mismatch"
        assert streamed[0]["file_id"] == initiated.data["file"]["id"]

    def test_a_content_type_mismatch_moves_the_same_two_counters(self, session_client, project, stored_objects):
        initiated = _initiate(session_client, project)
        _store_directly(
            initiated.data["file"]["object_key"],
            content_type="text/plain",
            stored_objects=stored_objects,
        )

        refused = _complete(session_client, project, initiated)

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "mime_mismatch"
        assert counters()[observability.UPLOAD_FAILURES] == 1
        assert counters()[observability.VERIFICATION_MISMATCHES] == 1

    def test_an_absent_object_is_a_failure_that_is_not_a_mismatch(self, session_client, project):
        """The neighbouring case: nothing was stored, so nothing contradicted a declaration."""
        initiated = _initiate(session_client, project)

        refused = _complete(session_client, project, initiated)

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "object_missing"
        assert counters()[observability.UPLOAD_FAILURES] == 1
        assert counters()[observability.VERIFICATION_MISMATCHES] == 0

    def test_a_settled_attempt_is_replayed_and_moves_no_counter(self, session_client, project, stored_objects, observe):
        initiated = _uploaded(session_client, project, stored_objects)

        repeated = _complete(session_client, project, initiated)

        assert repeated.status_code == status.HTTP_200_OK
        replayed = records(observe, observability.EVENT_FINALIZE, observability.FINALIZE_REPLAYED)
        assert len(replayed) == 1, replayed
        assert replayed[0]["file_id"] == initiated.data["file"]["id"]
        assert replayed[0]["version_no"] == 1
        # One record per outcome: the first request activated, the second replayed.
        assert len(records(observe, observability.EVENT_FINALIZE)) == 2
        assert counters() == {name: 0 for name in observability.COUNTER_NAMES}

    def test_a_repeated_refusal_replays_without_double_counting(self, session_client, project, observe):
        initiated = _initiate(session_client, project)
        first = _complete(session_client, project, initiated)
        second = _complete(session_client, project, initiated)

        assert first.status_code == second.status_code == status.HTTP_400_BAD_REQUEST
        assert second.data["code"] == "object_missing"
        assert counters()[observability.UPLOAD_FAILURES] == 1
        replayed = records(observe, observability.EVENT_FINALIZE, observability.FINALIZE_REPLAYED)
        assert len(replayed) == 1, replayed


@pytest.mark.contract
@pytest.mark.django_db
class TestDeleteRecord:
    """``file.delete``: the purge ends either with every object gone or with a survivor."""

    def _trashed(self, client, project, stored_objects):
        initiated = _uploaded(client, project, stored_objects)
        file_id = initiated.data["file"]["id"]
        trashed = client.delete(detail_url(project.workspace.slug, project.id, file_id))
        assert trashed.status_code == status.HTTP_204_NO_CONTENT
        return file_id, initiated.data["file"]["object_key"]

    def test_a_purged_file_is_recorded_after_its_objects_are_gone(
        self, session_client, project, stored_objects, observe
    ):
        file_id, object_key = self._trashed(session_client, project, stored_objects)

        purged = session_client.delete(purge_url(project.workspace.slug, project.id, file_id) + "?confirm=true")

        assert purged.status_code == status.HTTP_204_NO_CONTENT, purged.data
        found = records(observe, observability.EVENT_DELETE)
        assert len(found) == 1, found
        record = found[0]
        assert record["outcome"] == observability.DELETE_PURGED
        assert record["workspace_id"] == str(project.workspace_id)
        assert record["project_id"] == str(project.id)
        assert record["file_id"] == file_id
        assert record["version_no"] == 1
        assert record["trigger"] == "manual"
        assert record["versions"] == 1
        assert record["bytes"] == len(PDF_BYTES)
        assert S3Storage().get_object_metadata(object_key) is None

    def test_a_purge_that_left_an_object_is_recorded_with_the_other_outcome(
        self, session_client, project, stored_objects, observe
    ):
        file_id, object_key = self._trashed(session_client, project, stored_objects)

        with mock.patch.object(S3Storage, "delete_files", return_value=False):
            refused = session_client.delete(purge_url(project.workspace.slug, project.id, file_id) + "?confirm=true")

        assert refused.status_code == status.HTTP_502_BAD_GATEWAY
        assert refused.data["code"] == "storage_unavailable"
        found = records(observe, observability.EVENT_DELETE)
        assert len(found) == 1, found
        assert found[0]["outcome"] == observability.DELETE_PURGE_FAILED
        assert found[0]["file_id"] == file_id
        assert found[0]["version_no"] == 1
        assert found[0]["object_key"] == object_key

    def test_a_trashed_file_that_was_never_purged_emits_no_delete_record(
        self, session_client, project, stored_objects, observe
    ):
        """The negative control: the record is tied to the deletion, not to the endpoint."""
        self._trashed(session_client, project, stored_objects)

        assert records(observe, observability.EVENT_DELETE) == []


@pytest.mark.contract
@pytest.mark.django_db
class TestQuotaRejectionCounter:
    """``quota_rejections``: the ceiling refuses the request, before any signing."""

    def test_a_refused_reservation_moves_the_counter_and_writes_no_presign_record(
        self, session_client, project, observe
    ):
        # The ceiling is set on a row that exists: the listing/initiate path would
        # otherwise materialise the default one and the refusal would never happen.
        StorageQuota.objects.create(workspace=project.workspace, limit_bytes=1)

        refused = _initiate(session_client, project)

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "quota_exceeded"
        assert counters()[observability.QUOTA_REJECTIONS] == 1
        assert counters()[observability.UPLOAD_FAILURES] == 0

        streamed = counter_stream(observe, observability.QUOTA_REJECTIONS)
        assert len(streamed) == 1, streamed
        assert streamed[0]["level"] == "workspace"
        assert streamed[0]["limit_bytes"] == 1
        assert streamed[0]["project_id"] == str(project.id)

        # No URL was signed, so there is no presign outcome to report: the refusal
        # is the counter plus the audit row, not a presign record.
        assert records(observe, observability.EVENT_PRESIGN) == []

    def test_a_second_refusal_moves_the_counter_again(self, session_client, project):
        StorageQuota.objects.create(workspace=project.workspace, limit_bytes=1)

        assert _initiate(session_client, project).status_code == status.HTTP_400_BAD_REQUEST
        assert _initiate(session_client, project).status_code == status.HTTP_400_BAD_REQUEST

        assert counters()[observability.QUOTA_REJECTIONS] == 2


@pytest.mark.contract
@pytest.mark.django_db
class TestSweepDeletionCounter:
    """``sweep_deletions``: the deletion mechanism for objects with no verified version."""

    def _aged_abandoned_attempt(self, client, project, stored_objects):
        initiated = _initiate(client, project)
        _put_at_signed_url(initiated, stored_objects=stored_objects)
        when = timezone.now() - timedelta(hours=20)
        FileVersion.objects.filter(file_id=initiated.data["file"]["id"], version_no=1).update(
            reservation_expires_at=when, status_changed_at=when
        )
        return initiated

    def test_a_swept_object_moves_the_counter_once(self, session_client, project, stored_objects, observe):
        initiated = self._aged_abandoned_attempt(session_client, project, stored_objects)

        summary = cleanup_unverified_objects(batch_size=10)

        assert summary["swept"] == 1
        assert counters()[observability.SWEEP_DELETIONS] == 1
        # The sweep takes over an abandoned attempt; that is not an upload the server
        # refused, so the failure counter stays where it was.
        assert counters()[observability.UPLOAD_FAILURES] == 0

        streamed = counter_stream(observe, observability.SWEEP_DELETIONS)
        assert len(streamed) == 1, streamed
        assert streamed[0]["value"] == 1
        assert streamed[0]["file_id"] == initiated.data["file"]["id"]
        assert streamed[0]["version_no"] == 1
        assert streamed[0]["object_key"] == initiated.data["file"]["object_key"]

        # The row now carries the terminal marker, so a second pass deletes nothing
        # and the counter must not move again.
        assert cleanup_unverified_objects(batch_size=10)["swept"] == 0
        assert counters()[observability.SWEEP_DELETIONS] == 1

    def test_an_object_that_could_not_be_deleted_moves_no_counter(self, session_client, project, stored_objects):
        self._aged_abandoned_attempt(session_client, project, stored_objects)

        with mock.patch.object(S3Storage, "delete_files", return_value=False):
            summary = cleanup_unverified_objects(batch_size=10)

        assert summary == {"swept": 0, "failed": 1, "reservations_released": 0, "scanned": 1}
        assert counters()[observability.SWEEP_DELETIONS] == 0


@pytest.mark.contract
@pytest.mark.django_db
class TestTheObservabilitySurfaceItself:
    """The parts of the contract a caller can only get wrong, not merely forget."""

    def test_an_undeclared_counter_name_raises_instead_of_creating_a_series(self):
        with pytest.raises(KeyError):
            observability.increment("upload_failure")

    def test_the_four_counters_start_at_zero_under_their_documented_names(self):
        assert counters() == {
            "upload_failures": 0,
            "verification_mismatches": 0,
            "quota_rejections": 0,
            "sweep_deletions": 0,
        }
