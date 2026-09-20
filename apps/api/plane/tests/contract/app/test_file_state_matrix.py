# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One answer per file state, from every read path (ADV-001 §5 P-1).

The dominant defect across T-101…T-105 was two sources of truth for "can this row
be seen / served": the list showed a row that detail 404ed, ``can_download``
advertised a file every delivery endpoint refused, and a 409 branch was
unreachable because the lookup that guarded it had already returned. This module
walks each state a file can be in **today** through all four read paths in one
test and asserts they agree:

1. the listing (default surface and ``?trashed=true``),
2. the detail endpoint,
3. the ``permissions`` block of that same detail response,
4. ``download/`` and ``preview/``.

and it asserts the invariant that ties 3 to 4: ``permissions.can_download`` is
true **if and only if** the delivery endpoints sign.

How each state is built - the API wherever an API path exists:

======================  ==========================================================
state                   built by
======================  ==========================================================
``pending``/``uploading``  ``initiate-upload`` only (the row and its reservation
                        exist, no bytes are stored)
``active``              ``initiate-upload`` -> signed ``PUT`` -> ``complete-upload``
``superseded``          the same pipeline twice: the revision settles superseded
                        while v1 stays active, so the file still serves v1
``failed``              ``complete-upload`` with a declared size that contradicts
                        the stored object
``superseded`` only,   **directly in the database** (``hand_built_...``): nothing
no active version       deactivates a version yet, T-108 owns that
``purged``/            **directly in the database** (``hand_built_...``): only a
``purge_failed``        whole file can be purged today, so a single version in a
revisions               terminal state is marked by hand
``purge_failed``        the purge path with its object deletion made to fail (only
                        the storage failure is injected)
``purged``              the purge path: the row is gone, so every path 404s
``trashed``             **directly in the database** - see the test id
                        ``hand_built_trashed_row``. No API path deletes a file
                        yet (T-107 owns ``DELETE files/{file_id}/``), so the row
                        is written with the two columns that endpoint will set
                        (``status='trashed'`` and ``deleted_at``)
======================  ==========================================================

--- Extension point (ADV-001 §5 P-1) -------------------------------------------
``purged`` and ``quarantined`` are deliberately **not** in the table.

* ``purged`` (T-107): nothing purges a file today, so a row could only be
  hand-written - and a hand-written row proves that the code answers what the row
  says, which is the hollow coverage T-105 F-6 was flagged for. Add it in the
  commit that lands the purge path, asserting the whole contract ("row gone for
  good => every read path 404s, no delivery endpoint signs").
* ``quarantined`` (P3 scanner): the scanner does not exist yet. Add it with
  ``delivery_refusal``'s ``file_quarantined`` refusal when it does.

Neither may be added before an API path can produce the state.
"""

# Python imports
from dataclasses import dataclass
from unittest import mock

# Django imports
from django.conf import settings
from django.utils import timezone

# Third party imports
import pytest
import requests
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileObject,
    FileVersion,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

#: ``ROLE.MEMBER`` - a caller who may read and write but not reach the trash by id.
MEMBER = 15
#: ``ROLE.ADMIN``
ADMIN = 20


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def stored_objects():
    """Delete the objects this test stored; the database rolls back, MinIO does not."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    """The default caller is a MEMBER: an ADMIN can address the trash without a flag."""
    workspace = Workspace.objects.create(name="Matrix Workspace", slug="matrix-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=ADMIN, is_active=True)
    project = Project.objects.create(name="Matrix Project", identifier="MTX", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=MEMBER, is_active=True
    )
    return project


@pytest.fixture
def admin_client(project):
    """A second caller with the project ADMIN role, for the trash-addressability rule."""
    admin = User.objects.create(email="matrix-admin@example.com", username="matrix-admin", first_name="Matrix")
    admin.set_password("test-password")
    admin.save()
    WorkspaceMember.objects.create(workspace=project.workspace, member=admin, role=ADMIN, is_active=True)
    ProjectMember.objects.create(
        project=project, member=admin, workspace=project.workspace, role=ADMIN, is_active=True
    )
    client = APIClient()
    client.force_authenticate(user=admin)
    return client


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/complete-upload/"


def download_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/download/"


def preview_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/preview/"


def copy_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/copy/"


def purge_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/purge/"


@dataclass(frozen=True)
class VersionRequest:
    """One explicit ``?version=`` probe: the number, and what it must answer."""

    version_no: int
    servable: bool
    refusal_code: str | None = None
    version_status: str | None = None


@dataclass(frozen=True)
class FileState:
    """What the four read paths must answer for one state, and how it was built."""

    id: str
    #: the default listing shows the row / the ``?trashed=true`` listing shows it
    visible_in_list: bool
    visible_in_trash: bool
    #: whether the delivery endpoints sign; also what ``can_download`` must say
    servable: bool
    #: the version the delivery endpoints serve when they sign
    served_version_no: int | None = None
    #: the refusal when they do not
    refusal_status: int | None = None
    refusal_code: str | None = None
    #: ``version_status`` of the *file-level* refusal (``None`` keys are absent)
    file_level_version_status: str | None = None
    #: the answer for an explicit ``?version=`` request, when the state has one
    requested_version: int | None = None
    requested_servable: bool = False
    requested_refusal_code: str | None = None
    requested_version_status: str | None = None
    #: further ``?version=`` probes for a file that holds versions in more than one
    #: terminal state (the requested-version dimension, extended past ``failed``).
    extra_version_requests: tuple[VersionRequest, ...] = ()
    #: an ADMIN may address a row the default surface hides (detail without a flag)
    admin_can_address_hidden_row: bool = False
    #: False once the row is purged: every path answers 404, not a refusal.
    row_exists: bool = True
    #: whether ``POST {file_id}/copy/`` is allowed, and how it refuses when not.
    #: It matches ``servable`` in every state today, and that is the point: a copy
    #: of a file nobody can download is a new row nobody can download.
    copy_allowed: bool = False
    copy_refusal_status: int = 409
    #: ``None`` means the generic "does not exist" body (the row is off the default
    #: surface, so the write doors treat it as absent rather than as a refusal).
    copy_refusal_code: str | None = None


PENDING = FileState(
    id="pending_uploading",
    visible_in_list=True,
    visible_in_trash=False,
    servable=False,
    refusal_status=409,
    refusal_code="object_unavailable",
    # No version is active yet, so the file-level answer names no version status;
    # asking for the in-flight version names what that version is.
    file_level_version_status=None,
    requested_version=1,
    requested_servable=False,
    requested_refusal_code="object_unavailable",
    requested_version_status=FileVersion.Status.UPLOADING,
    copy_allowed=False,
    copy_refusal_status=409,
    copy_refusal_code="object_unavailable",
)

ACTIVE = FileState(
    id="active",
    visible_in_list=True,
    visible_in_trash=False,
    servable=True,
    served_version_no=1,
    requested_version=1,
    requested_servable=True,
    copy_allowed=True,
)

SUPERSEDED = FileState(
    id="active_with_a_superseded_revision",
    visible_in_list=True,
    visible_in_trash=False,
    servable=True,
    # v2 is stored but not active (AD-18): the file serves v1, and only v2 asked
    # for by number is served from v2's key.
    served_version_no=1,
    requested_version=2,
    requested_servable=True,
    copy_allowed=True,
)

FAILED = FileState(
    id="failed_verification",
    visible_in_list=True,
    visible_in_trash=False,
    servable=False,
    refusal_status=409,
    refusal_code="object_unavailable",
    file_level_version_status=None,
    requested_version=1,
    requested_servable=False,
    requested_refusal_code="object_unavailable",
    requested_version_status=FileVersion.Status.FAILED,
    copy_allowed=False,
    copy_refusal_status=409,
    copy_refusal_code="object_unavailable",
)

SUPERSEDED_ONLY = FileState(
    id="hand_built_superseded_only_no_active_version",
    visible_in_list=True,
    visible_in_trash=False,
    servable=False,
    refusal_status=409,
    refusal_code="object_unavailable",
    file_level_version_status=None,
    # v2 is still stored, so asking for it by number is served; the file itself has
    # no active pointer and must not guess one.
    requested_version=2,
    requested_servable=True,
    copy_allowed=False,
    copy_refusal_status=409,
    copy_refusal_code="object_unavailable",
)

TRASHED = FileState(
    id="hand_built_trashed_row",
    visible_in_list=False,
    visible_in_trash=True,
    servable=False,
    refusal_status=409,
    refusal_code="file_trashed",
    requested_version=1,
    requested_servable=False,
    requested_refusal_code="file_trashed",
    admin_can_address_hidden_row=True,
    copy_allowed=False,
    copy_refusal_status=409,
    copy_refusal_code="file_trashed",
)


PURGED = FileState(
    id="purged_row_gone",
    visible_in_list=False,
    visible_in_trash=False,
    servable=False,
    row_exists=False,
    copy_allowed=False,
    copy_refusal_status=404,
    copy_refusal_code=None,
)

PURGE_FAILED = FileState(
    id="purge_failed_after_injected_object_deletion_failure",
    visible_in_list=False,
    # A failed purge stays visible in the trash, so the deletion cannot drop out
    # of sight while it still holds objects and quota (R3-02).
    visible_in_trash=True,
    servable=False,
    refusal_status=409,
    refusal_code="object_unavailable",
    # v1 is still the active version, so the file-level refusal names its status.
    file_level_version_status=FileVersion.Status.PURGE_FAILED,
    requested_version=1,
    requested_servable=False,
    requested_refusal_code="object_unavailable",
    requested_version_status=FileVersion.Status.PURGE_FAILED,
    copy_allowed=False,
    # The row is soft-deleted, so the write doors treat it as absent (F-2) rather
    # than as a copyable file: its remaining life is restore or purge.
    copy_refusal_status=404,
    copy_refusal_code=None,
)


# Every state builder has the same shape - ``(session_client, admin_client, project,
# stored_objects)`` - so the walk can call them uniformly; only the two purge states
# need the admin client, and the others name it ``_admin_client`` to say so.
TERMINAL_VERSIONS = FileState(
    id="hand_built_live_file_with_purged_and_purge_failed_revisions",
    visible_in_list=True,
    visible_in_trash=False,
    # v1 is active and stored, so the file itself is servable; the two terminal
    # revisions are not, and asking for one by number must say which and why.
    servable=True,
    served_version_no=1,
    requested_version=2,
    requested_servable=False,
    requested_refusal_code="object_unavailable",
    requested_version_status=FileVersion.Status.PURGED,
    copy_allowed=True,
    extra_version_requests=(
        VersionRequest(3, False, "object_unavailable", FileVersion.Status.PURGE_FAILED),
    ),
)


def _initiate(session_client, project, *, file_name, file_id=None):
    response = session_client.post(
        upload_url(project.workspace.slug, project.id),
        {"file_name": file_name, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"}
        | ({"file_id": str(file_id)} if file_id is not None else {}),
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK, response.data
    return response


def _put_bytes(initiated, stored_objects):
    """Store the bytes at the signed key; the only step that needs plain requests."""
    upload = initiated.data["upload"]
    put = requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30)
    assert put.status_code == 200, put.content[:200]
    stored_objects.append(initiated.data["file"]["object_key"])


def build_pending(session_client, _admin_client, project, stored_objects):
    """An initiated upload: the row and its reservation exist, no bytes are stored."""
    initiated = _initiate(session_client, project, file_name="state-pending.pdf")
    return PENDING, initiated.data["file"]["id"]


def build_active(session_client, _admin_client, project, stored_objects):
    """A verified first version, through the real pipeline."""
    initiated = _initiate(session_client, project, file_name="state-active.pdf")
    file_id = initiated.data["file"]["id"]
    _put_bytes(initiated, stored_objects)

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data
    assert completed.data["version"]["status"] == FileVersion.Status.ACTIVE

    return ACTIVE, file_id


def build_superseded(session_client, _admin_client, project, stored_objects):
    """A second verified version that stays superseded while v1 remains active."""
    _, file_id = build_active(session_client, admin_client, project, stored_objects)

    revision = _initiate(session_client, project, file_name="state-active.pdf", file_id=file_id)
    assert revision.data["version_no"] == 2
    _put_bytes(revision, stored_objects)

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 2, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data
    assert completed.data["version"]["status"] == FileVersion.Status.SUPERSEDED
    assert completed.data["activation_required"] is True

    return SUPERSEDED, file_id


def build_failed(session_client, _admin_client, project, stored_objects):
    """A version whose declared size contradicts the stored object."""
    initiated = _initiate(session_client, project, file_name="state-failed.pdf")
    file_id = initiated.data["file"]["id"]
    _put_bytes(initiated, stored_objects)

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(PDF_BYTES) + 10},
        format="json",
    )
    assert completed.status_code == status.HTTP_400_BAD_REQUEST, completed.data
    assert completed.data["code"] == "size_mismatch"

    return FAILED, file_id


def build_superseded_only(session_client, _admin_client, project, stored_objects):
    """Built directly in the database: nothing deactivates a version yet (T-108).

    The state F-1 was raised for: every version is stored, none is active, and the
    old copy path used to point the copy's active version at the newest one anyway.
    """
    _, file_id = build_superseded(session_client, _admin_client, project, stored_objects)

    FileVersion.objects.filter(file_id=file_id, version_no=1).update(
        status=FileVersion.Status.SUPERSEDED, is_active=False
    )
    assert not FileVersion.objects.filter(file_id=file_id, is_active=True).exists()

    return SUPERSEDED_ONLY, file_id


def build_purged(session_client, admin_client, project, stored_objects):
    """Trash then purge through the API; the row is gone for good (no hand-writing)."""
    _, file_id = build_active(session_client, admin_client, project, stored_objects)
    assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
    purged = admin_client.delete(
        purge_url(project.workspace.slug, project.id, file_id) + "?confirm=true"
    )
    assert purged.status_code == 204, purged.data

    return PURGED, file_id


def build_purge_failed(session_client, admin_client, project, stored_objects):
    """Trash, then purge with the adapter's object deletion failing.

    The state itself comes from the API path; only the storage failure is injected,
    because that is the one thing a test cannot arrange for real.
    """
    _, file_id = build_active(session_client, admin_client, project, stored_objects)
    assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204

    with mock.patch.object(S3Storage, "delete_files", return_value=False):
        failed = admin_client.delete(
            purge_url(project.workspace.slug, project.id, file_id) + "?confirm=true"
        )
    assert failed.status_code == 502, failed.data

    return PURGE_FAILED, file_id


def build_terminal_versions(session_client, _admin_client, project, stored_objects):
    """A live file holding a purged revision and a purge_failed revision.

    Built through the API as far as it can be (v1 active, v2 and v3 stored) and then
    marked in the database, because nothing purges a single *version* yet: the purge
    endpoint removes whole files, and deleting an unverified version's object is
    T-118's sweep. The marks are written directly, which is why this state's id says
    ``hand_built``.
    """
    _, file_id = build_superseded(session_client, _admin_client, project, stored_objects)

    third = _initiate(session_client, project, file_name="state-active.pdf", file_id=file_id)
    assert third.data["version_no"] == 3
    _put_bytes(third, stored_objects)
    finished = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 3, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert finished.status_code == status.HTTP_200_OK, finished.data
    assert finished.data["version"]["status"] == FileVersion.Status.SUPERSEDED

    purged_key = FileVersion.objects.get(file_id=file_id, version_no=2).object_key
    assert S3Storage().delete_files([purged_key]) is True
    FileVersion.objects.filter(file_id=file_id, version_no=2).update(
        status=FileVersion.Status.PURGED,
        object_deleted_at=timezone.now(),
        is_active=False,
        status_changed_at=timezone.now(),
    )
    FileVersion.objects.filter(file_id=file_id, version_no=3).update(
        status=FileVersion.Status.PURGE_FAILED, status_changed_at=timezone.now()
    )

    return TERMINAL_VERSIONS, file_id


def build_trashed(session_client, _admin_client, project, stored_objects):
    """Built directly in the database: no API path trashes a file yet (T-107)."""
    _, file_id = build_active(session_client, admin_client, project, stored_objects)

    FileObject.all_objects.filter(pk=file_id).update(
        status=FileObject.Status.TRASHED, deleted_at=timezone.now()
    )

    return TRASHED, file_id


STATE_MATRIX = [
    pytest.param(build_pending, id=PENDING.id),
    pytest.param(build_active, id=ACTIVE.id),
    pytest.param(build_superseded, id=SUPERSEDED.id),
    pytest.param(build_failed, id=FAILED.id),
    pytest.param(build_superseded_only, id=SUPERSEDED_ONLY.id),
    pytest.param(build_trashed, id=TRASHED.id),
    pytest.param(build_terminal_versions, id=TERMINAL_VERSIONS.id),
    pytest.param(build_purge_failed, id=PURGE_FAILED.id),
    pytest.param(build_purged, id=PURGED.id),
]

@pytest.mark.contract
@pytest.mark.django_db
class TestStateMatrix:
    """The four read paths answer the same question the same way."""

    @pytest.mark.parametrize("builder", STATE_MATRIX)
    def test_every_read_path_agrees_for_each_state(
        self, session_client, admin_client, project, stored_objects, builder
    ):
        state, file_id = builder(session_client, admin_client, project, stored_objects)
        slug = project.workspace.slug

        # 1. the listing, on both surfaces
        default = session_client.get(files_url(slug, project.id))
        assert default.status_code == status.HTTP_200_OK
        default_ids = [row["id"] for row in default.data["results"]]
        assert (str(file_id) in default_ids) is state.visible_in_list, f"{state.id}: default listing"

        trash = session_client.get(files_url(slug, project.id), {"trashed": True})
        assert trash.status_code == status.HTTP_200_OK
        trash_ids = [row["id"] for row in trash.data["results"]]
        assert (str(file_id) in trash_ids) is state.visible_in_trash, f"{state.id}: trash listing"

        if not state.row_exists:
            # A purged file has no row at all: every door answers 404 rather than a
            # refusal, for every role, and the audit trail is what survives.
            for client in (session_client, admin_client):
                assert client.get(detail_url(slug, project.id, file_id)).status_code == status.HTTP_404_NOT_FOUND
                assert (
                    client.get(detail_url(slug, project.id, file_id), {"trashed": True}).status_code
                    == status.HTTP_404_NOT_FOUND
                )
            for url in (download_url, preview_url):
                assert session_client.get(url(slug, project.id, file_id)).status_code == status.HTTP_404_NOT_FOUND
            gone_copy = session_client.post(copy_url(slug, project.id, file_id), {}, format="json")
            assert gone_copy.status_code == status.HTTP_404_NOT_FOUND
            assert gone_copy.data == {"error": "The required object does not exist."}
            assert FileAccessLog.objects.filter(
                file_id=file_id, action=FileAccessLog.Action.PURGED
            ).exists(), f"{state.id}: the purged audit row must survive the file"
            return

        # 2 + 3. detail and the permissions it advertises
        detail = session_client.get(detail_url(slug, project.id, file_id))
        if state.visible_in_list:
            assert detail.status_code == status.HTTP_200_OK, f"{state.id}: {detail.data}"
            addressed = detail
        else:
            # A row the default surface hides is not addressable at all, which is
            # the point: the listing and the detail endpoint agree on visibility.
            assert detail.status_code == status.HTTP_404_NOT_FOUND
            addressed = session_client.get(detail_url(slug, project.id, file_id), {"trashed": True})
            assert addressed.status_code == status.HTTP_200_OK, f"{state.id}: {addressed.data}"

        advertised = addressed.data["permissions"]["can_download"]
        assert advertised is state.servable, f"{state.id}: can_download"

        if state.admin_can_address_hidden_row:
            # An ADMIN may address the trash without the flag (T-103 §4.1), and the
            # advertised affordance must not change because of who is asking.
            admin_view = admin_client.get(detail_url(slug, project.id, file_id))
            assert admin_view.status_code == status.HTTP_200_OK
            assert admin_view.data["permissions"]["can_download"] is state.servable

        # 4. the delivery endpoints, and the invariant that ties them to 3
        download = session_client.get(download_url(slug, project.id, file_id))
        preview = session_client.get(preview_url(slug, project.id, file_id))

        assert (download.status_code == status.HTTP_200_OK) is advertised, f"{state.id}: can_download vs download"
        assert (preview.status_code == status.HTTP_200_OK) is advertised, f"{state.id}: can_download vs preview"

        if state.servable:
            assert "X-Amz-Signature" in download.data["url"]
            assert "X-Amz-Signature" in preview.data["url"]
            assert download.data["version_no"] == state.served_version_no
            assert preview.data["version_no"] == state.served_version_no
        else:
            for response in (download, preview):
                assert response.status_code == state.refusal_status, f"{state.id}: {response.data}"
                assert response.data["code"] == state.refusal_code
                assert "url" not in response.data
            # ``None`` means the refusal names no version status (the key may be
            # absent or null); a specific value names the version it examined.
            assert download.data.get("version_status") == state.file_level_version_status

        for extra in state.extra_version_requests:
            asked_extra = session_client.get(
                download_url(slug, project.id, file_id), {"version": extra.version_no}
            )
            if extra.servable:
                assert asked_extra.status_code == status.HTTP_200_OK, f"{state.id}: {asked_extra.data}"
            else:
                assert asked_extra.status_code == status.HTTP_409_CONFLICT, f"{state.id}: {asked_extra.data}"
                assert asked_extra.data["code"] == extra.refusal_code
                assert asked_extra.data.get("version_status") == extra.version_status
                assert "url" not in asked_extra.data

        # 5. the write door: a copy must succeed exactly when the file is servable,
        # and the copy it makes must be servable in its own right.
        copied = session_client.post(copy_url(slug, project.id, file_id), {}, format="json")
        if state.copy_allowed:
            assert copied.status_code == status.HTTP_200_OK, f"{state.id}: {copied.data}"
            copy_id = copied.data["file"]["id"]
            assert copy_id != str(file_id)
            for key in FileVersion.objects.filter(file_id=copy_id).values_list("object_key", flat=True):
                stored_objects.append(key)
            copy_active = FileVersion.objects.get(file_id=copy_id, is_active=True)
            source_active = FileVersion.objects.get(file_id=file_id, is_active=True)
            assert copy_active.object_key != source_active.object_key, f"{state.id}: copy aliased a key"
            downloaded_copy = session_client.get(download_url(slug, project.id, copy_id))
            assert downloaded_copy.status_code == status.HTTP_200_OK, f"{state.id}: copy not servable"
        else:
            assert copied.status_code == state.copy_refusal_status, f"{state.id}: {copied.data}"
            if state.copy_refusal_code is None:
                assert copied.data == {"error": "The required object does not exist."}
            else:
                assert copied.data["code"] == state.copy_refusal_code, f"{state.id}: copy refusal code"
            assert FileObject.all_objects.filter(project=project).count() == 1, f"{state.id}: a copy was made"

        # and asking for a specific version never widens what may be served
        if state.requested_version is not None:
            asked = session_client.get(
                download_url(slug, project.id, file_id), {"version": state.requested_version}
            )
            if state.requested_servable:
                assert asked.status_code == status.HTTP_200_OK, f"{state.id}: {asked.data}"
                assert asked.data["version_no"] == state.requested_version
            else:
                assert asked.status_code == status.HTTP_409_CONFLICT
                assert asked.data["code"] == state.requested_refusal_code
                assert asked.data.get("version_status") == state.requested_version_status

    def test_hand_built_missing_active_version_row_refuses_rather_than_falling_back(
        self, session_client, admin_client, project, stored_objects
    ):
        """The one state the matrix cannot build: the active version row is gone.

        No API path removes a version row yet (T-107's purge will), so the row is
        deleted directly here - and only the row, leaving the object behind, which
        is precisely the window the old newest-stored fallback served: the file's
        own ``object_key`` names the deleted version, so a URL signed for whatever
        was stored last is a URL for an object this file does not claim.
        """
        _, file_id = build_superseded(session_client, admin_client, project, stored_objects)
        active = FileVersion.objects.get(file_id=file_id, is_active=True)
        assert active.version_no == 1
        FileVersion.objects.filter(pk=active.pk).delete()

        file_object = FileObject.objects.get(pk=file_id)
        assert file_object.object_key == active.object_key
        assert not FileVersion.objects.filter(file_id=file_id, is_active=True).exists()

        listing = session_client.get(files_url(project.workspace.slug, project.id))
        assert str(file_id) in [row["id"] for row in listing.data["results"]]

        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["version"] is None
        assert detail.data["permissions"]["can_download"] is False

        refused = session_client.get(download_url(project.workspace.slug, project.id, file_id))
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert refused.data["code"] == "object_unavailable"
        assert "url" not in refused.data

        # Asking for the surviving (superseded) version is still allowed, and it
        # is that version's key that gets signed - never a guess.
        explicit = session_client.get(
            download_url(project.workspace.slug, project.id, file_id), {"version": 2}
        )
        assert explicit.status_code == status.HTTP_200_OK
        assert explicit.data["version_no"] == 2
        assert explicit.data["url"].split("?")[0].endswith(
            FileVersion.objects.get(file_id=file_id, version_no=2).object_key
        )
