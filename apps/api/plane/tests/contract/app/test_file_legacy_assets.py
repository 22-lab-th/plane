# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The legacy-object guarantee (AC-23, R-NFR-10, R-DEL-5, T-117).

Migration of legacy ``FileAsset`` objects into project prefixes is deferred to
Phase 2 by owner decision (2026-09-20), so the project-scoped pipeline has to
leave every pre-existing object exactly where it is. This module is the
regression fence around the three guarantees, each carried by its own
assertions:

1. a pre-existing object at its legacy key still downloads - the provider
   returns the same bytes and the same content type through the legacy endpoint,
   and the object at the legacy key is unchanged;
2. every URL a legacy row hands out still resolves - the ``asset_url`` an issue
   attachment, issue description, workspace logo or avatar saved before the
   change is a redirect, not a 404;
3. the project pipeline never rewrites a legacy key or row - after project
   uploads, a copy, the download path and both sweep jobs have run, the
   ``FileAsset`` table and the object at the legacy key are identical.

The fixtures create their rows through the legacy endpoints themselves
(``IssueAttachmentV2Endpoint``, ``WorkspaceFileAssetEndpoint``,
``ProjectAssetEndpoint``, ``UserAssetsV2Endpoint``) instead of hand-rolling a
key, so the key under test is the one production code derives, and the bytes are
stored with the presigned POST those endpoints return. The legacy ``PATCH`` that
marks an upload complete enqueues a Celery task and the test stack has no broker
(DEFECT-010), so the harness sets ``is_uploaded`` directly - the only step a
missing queue changes.
"""

# Python imports
from datetime import timedelta

# Django imports
from django.utils import timezone

# Third party imports
import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError
from rest_framework import status

# Module imports
from plane.bgtasks.file_purge_task import purge_expired_files
from plane.bgtasks.file_sweep_task import cleanup_unverified_objects, recheck_deleted_objects
from plane.db.models import (
    FileAsset,
    FileObject,
    FileVersion,
    Issue,
    Project,
    ProjectMember,
    State,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage

MINIO_ENDPOINT = "http://test-minio:9000"

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"

# The segment every project-scoped key contains and no legacy key does: the
# guarantee is that the pipeline never introduces it into a legacy row's key
# (ARCH-001 AD-02: keys are write-once and never recomputed).
PROJECT_PREFIX_MARKER = "/projects/"


def workspace_assets_url(slug):
    return f"/api/assets/v2/workspaces/{slug}/"


def project_assets_url(slug, project_id):
    return f"/api/assets/v2/workspaces/{slug}/projects/{project_id}/"


def user_assets_url():
    return "/api/assets/v2/user-assets/"


def attachments_url(slug, project_id, issue_id):
    return f"{project_assets_url(slug, project_id)}issues/{issue_id}/attachments/"


def attachment_download_url(slug, project_id, issue_id, asset_id):
    return f"{attachments_url(slug, project_id, issue_id)}{asset_id}/"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def initiate_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/complete-upload/"


def download_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/download/"


def copy_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/copy/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    """Sign browser-facing URLs against the reachable test endpoint.

    The developer environment signs with a loopback MinIO address a container
    cannot dereference; pointing both variables at the test service is what lets
    these tests follow the redirect for real.
    """
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", MINIO_ENDPOINT)
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", MINIO_ENDPOINT)


@pytest.fixture
def stored_objects():
    """Delete whatever a test stored; the database rolls back, MinIO does not."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def independent_store():
    """A second boto3 client, so object claims do not go through the app adapter."""
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=S3Storage().aws_access_key_id,
        aws_secret_access_key=S3Storage().aws_secret_access_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Legacy Workspace", slug="legacy-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Legacy Project", identifier="LEG", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


@pytest.fixture
def issue(project):
    state = State.objects.create(
        name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
    )
    return Issue.objects.create(
        name="An issue with a pre-existing attachment",
        project=project,
        workspace=project.workspace,
        state=state,
    )


def object_metadata(key):
    return S3Storage().get_object_metadata(key)


def object_exists(store, key):
    """True when the exact key is stored; a missing key is a real answer, not an error."""
    try:
        store.head_object(Bucket=S3Storage().aws_storage_bucket_name, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def mark_uploaded(asset):
    """Do what the legacy PATCH does, minus the Celery enqueue the queue-less stack cannot serve."""
    asset.is_uploaded = True
    asset.storage_metadata = object_metadata(asset.asset.name)
    asset.save(update_fields=["is_uploaded", "storage_metadata"])
    asset.refresh_from_db()
    return asset


def create_legacy_asset(
    session_client,
    project,
    issue,
    *,
    entity_type,
    name="Legacy.png",
    mime="image/png",
    content=PNG_BYTES,
    stored_objects,
):
    """Create a legacy ``FileAsset`` the way the legacy code creates one.

    The row comes from the legacy endpoint, so its ``asset`` key is production's
    derivation; the bytes go to the key the presigned POST in that response
    names, which must be the very same key.
    """
    payload = {"name": name, "type": mime, "size": len(content), "entity_type": entity_type}

    if entity_type == FileAsset.EntityTypeContext.ISSUE_ATTACHMENT:
        # The attachment endpoint names the entity in the URL and hardcodes the type.
        del payload["entity_type"]
        url = attachments_url(project.workspace.slug, project.id, issue.id)
    elif entity_type == FileAsset.EntityTypeContext.ISSUE_DESCRIPTION:
        payload["entity_identifier"] = str(issue.id)
        url = project_assets_url(project.workspace.slug, project.id)
    elif entity_type == FileAsset.EntityTypeContext.WORKSPACE_LOGO:
        payload["entity_identifier"] = str(project.workspace_id)
        url = workspace_assets_url(project.workspace.slug)
    elif entity_type == FileAsset.EntityTypeContext.PROJECT_COVER:
        # Covers are uploaded through the workspace-level endpoint, not the project one.
        payload["entity_identifier"] = str(project.id)
        url = workspace_assets_url(project.workspace.slug)
    elif entity_type == FileAsset.EntityTypeContext.USER_AVATAR:
        url = user_assets_url()
    else:  # pragma: no cover - a case added without a legacy endpoint is a test bug
        raise AssertionError(f"no legacy endpoint covers {entity_type}")

    response = session_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_200_OK, response.data

    presigned = response.data["upload_data"]
    key = presigned["fields"]["key"]
    posted = requests.post(
        presigned["url"], data=presigned["fields"], files={"file": (name, content, mime)}, timeout=30
    )
    assert posted.status_code in (200, 201, 204), (posted.status_code, posted.text[:300])

    asset = FileAsset.all_objects.get(id=response.data["asset_id"])
    # The row and the stored object agree on the key, and the key is legacy-shaped.
    assert asset.asset.name == key
    assert PROJECT_PREFIX_MARKER not in key

    stored_objects.append(key)
    mark_uploaded(asset)
    return asset


def fetch_through(location):
    """Follow the redirect to the provider and return the fetched response."""
    assert location, "the legacy endpoint returned no Location header"
    return requests.get(location, timeout=30)


def upload_project_file(session_client, project, stored_objects, *, name="Project.pdf"):
    """Take the project pipeline through presign -> PUT -> verified finalize."""
    initiated = session_client.post(
        initiate_url(project.workspace.slug, project.id),
        {"file_name": name, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
        format="json",
    )
    assert initiated.status_code == status.HTTP_200_OK, initiated.data
    file_id = initiated.data["file"]["id"]
    upload = initiated.data["upload"]
    # The pipeline's own key shape, asserted so the comparison below is meaningful.
    assert PROJECT_PREFIX_MARKER in initiated.data["file"]["object_key"]
    stored_objects.append(initiated.data["file"]["object_key"])

    put = requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30)
    assert put.status_code == status.HTTP_200_OK, put.text[:300]

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data
    return file_id


def abandon_project_upload(session_client, project, stored_objects):
    """Store bytes for a project upload and never finalize it, as an abandoned attempt is."""
    abandoned = session_client.post(
        initiate_url(project.workspace.slug, project.id),
        {"file_name": "Abandoned.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
        format="json",
    )
    assert abandoned.status_code == status.HTTP_200_OK, abandoned.data
    key = abandoned.data["file"]["object_key"]
    stored_objects.append(key)
    upload = abandoned.data["upload"]
    put = requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30)
    assert put.status_code == status.HTTP_200_OK, put.text[:300]

    # Age the attempt past the URL TTL plus the margin, as the sweep's predicate reads.
    when = timezone.now() - timedelta(hours=20)
    FileVersion.objects.filter(file_id=abandoned.data["file"]["id"], version_no=1).update(
        reservation_expires_at=when, status_changed_at=when
    )
    return abandoned.data["file"]["id"], key


def legacy_row_state(workspace_id):
    """Every legacy row in the workspace, as the tuple a rewrite would move."""
    return {
        str(asset.id): (
            asset.asset.name,
            asset.entity_type,
            asset.is_uploaded,
            asset.is_deleted,
            asset.size,
            asset.attributes,
            str(asset.project_id),
            str(asset.issue_id),
            asset.updated_at.isoformat(),
        )
        for asset in FileAsset.all_objects.filter(workspace_id=workspace_id).order_by("id")
    }


@pytest.mark.contract
@pytest.mark.django_db
class TestLegacyObjectStillDownloads:
    """AC-23/T-117 #1: the object at the legacy key still downloads, byte for byte."""

    def test_a_pre_existing_attachment_downloads_byte_identical(
        self, session_client, project, issue, stored_objects
    ):
        asset = create_legacy_asset(
            session_client,
            project,
            issue,
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            name="Legacy.png",
            stored_objects=stored_objects,
        )
        legacy_key = asset.asset.name
        before = object_metadata(legacy_key)
        assert before is not None

        response = session_client.get(
            attachment_download_url(project.workspace.slug, project.id, issue.id, asset.id)
        )

        assert response.status_code == status.HTTP_302_FOUND, response.data
        fetched = fetch_through(response["Location"])

        assert fetched.status_code == status.HTTP_200_OK, fetched.text[:300]
        assert fetched.content == PNG_BYTES
        assert fetched.headers["Content-Type"] == "image/png"
        assert fetched.headers["Content-Disposition"].startswith("attachment")

        # The row still names the legacy key and the object there is untouched.
        asset.refresh_from_db()
        assert asset.asset.name == legacy_key
        assert asset.is_uploaded is True
        after = object_metadata(legacy_key)
        assert after["ETag"] == before["ETag"]
        assert after["ContentLength"] == len(PNG_BYTES)
        assert after["ContentType"] == "image/png"

        # The signed URL really is for the legacy key, not for a recomputed one.
        assert fetched.url.split("?")[0].endswith(legacy_key)

    def test_a_pre_existing_attachment_downloads_on_the_workspace_route_too(
        self, session_client, project, issue, stored_objects
    ):
        """AC-23/R-NFR-10: the second legacy attachment route serves the same bytes."""
        asset = create_legacy_asset(
            session_client,
            project,
            issue,
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            name="LegacyToo.png",
            stored_objects=stored_objects,
        )

        response = session_client.get(
            f"/api/assets/v2/workspaces/{project.workspace.slug}/download/{asset.id}/"
        )

        assert response.status_code == status.HTTP_302_FOUND, response.data
        fetched = fetch_through(response["Location"])
        assert fetched.status_code == status.HTTP_200_OK
        assert fetched.content == PNG_BYTES
        assert fetched.headers["Content-Type"] == "image/png"


@pytest.mark.contract
@pytest.mark.django_db
class TestLegacyAssetUrlsResolve:
    """AC-23/T-117 #2: the URL a legacy row hands out resolves, never 404."""

    @pytest.mark.parametrize(
        "entity_type,url_prefix",
        [
            (FileAsset.EntityTypeContext.ISSUE_ATTACHMENT, "/api/assets/v2/workspaces/"),
            (FileAsset.EntityTypeContext.ISSUE_DESCRIPTION, "/api/assets/v2/workspaces/"),
            (FileAsset.EntityTypeContext.WORKSPACE_LOGO, "/api/assets/v2/static/"),
            (FileAsset.EntityTypeContext.PROJECT_COVER, "/api/assets/v2/static/"),
            (FileAsset.EntityTypeContext.USER_AVATAR, "/api/assets/v2/static/"),
        ],
    )
    def test_the_stored_asset_url_resolves(
        self, session_client, project, issue, stored_objects, entity_type, url_prefix
    ):
        asset = create_legacy_asset(
            session_client, project, issue, entity_type=entity_type, stored_objects=stored_objects
        )

        asset_url = asset.asset_url
        assert asset_url is not None
        assert asset_url.startswith(url_prefix)
        # The URL names the row, which is what a stored reference keys on.
        assert asset_url.endswith(f"/{asset.id}/")

        response = session_client.get(asset_url)

        assert response.status_code == status.HTTP_302_FOUND, (asset_url, response.data)
        fetched = fetch_through(response["Location"])
        assert fetched.status_code == status.HTTP_200_OK
        assert fetched.content == PNG_BYTES
        assert fetched.headers["Content-Type"] == "image/png"

    def test_the_legacy_attachment_listing_hands_out_a_working_url(
        self, session_client, project, issue, stored_objects
    ):
        """The listing a client re-reads publishes the URL as well."""
        asset = create_legacy_asset(
            session_client,
            project,
            issue,
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            name="Listed.png",
            stored_objects=stored_objects,
        )

        listed = session_client.get(attachments_url(project.workspace.slug, project.id, issue.id))

        assert listed.status_code == status.HTTP_200_OK, listed.data
        rows = [row for row in listed.data if str(row["id"]) == str(asset.id)]
        assert len(rows) == 1
        assert rows[0]["asset_url"] == asset.asset_url

        response = session_client.get(rows[0]["asset_url"])
        assert response.status_code == status.HTTP_302_FOUND
        assert fetch_through(response["Location"]).content == PNG_BYTES


@pytest.mark.contract
@pytest.mark.django_db
class TestProjectPipelineLeavesLegacyStateAlone:
    """AC-23/T-117 #3: nothing the project pipeline runs rewrites or migrates a legacy row."""

    def test_legacy_rows_keys_and_bytes_survive_the_project_pipeline(
        self, session_client, project, issue, stored_objects, independent_store
    ):
        asset = create_legacy_asset(
            session_client,
            project,
            issue,
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            name="Untouched.png",
            stored_objects=stored_objects,
        )
        legacy_key = asset.asset.name
        rows_before = legacy_row_state(project.workspace_id)
        object_before = object_metadata(legacy_key)

        # (a) The project write path: presign, PUT, verified finalize.
        project_file_id = upload_project_file(session_client, project, stored_objects)

        # (b) The project read and copy paths.
        downloaded = session_client.get(download_url(project.workspace.slug, project.id, project_file_id))
        assert downloaded.status_code == status.HTTP_200_OK, downloaded.data
        assert fetch_through(downloaded.data["url"]).content == PDF_BYTES

        copied = session_client.post(
            copy_url(project.workspace.slug, project.id, project_file_id), {}, format="json"
        )
        assert copied.status_code == status.HTTP_200_OK, copied.data
        stored_objects.append(FileObject.objects.get(id=copied.data["file"]["id"]).object_key)

        # (c) The deletion paths, with a real abandoned attempt so the sweep does delete.
        _, abandoned_key = abandon_project_upload(session_client, project, stored_objects)
        swept = cleanup_unverified_objects(batch_size=50)
        assert swept["swept"] >= 1, swept
        assert swept["failed"] == 0, swept
        rechecked = recheck_deleted_objects(batch_size=50)
        assert rechecked["failed"] == 0, rechecked
        purge_expired_files()

        # The deletion mechanism ran and removed the abandoned object by its exact key...
        assert object_exists(independent_store, abandoned_key) is False
        # ...while the legacy object at its legacy key is still there, unchanged.
        assert object_exists(independent_store, legacy_key) is True
        assert object_metadata(legacy_key) == object_before

        # No legacy row was rewritten, removed or added.
        assert legacy_row_state(project.workspace_id) == rows_before

        # Every key the project pipeline wrote is project-scoped and distinct from the legacy key.
        project_keys = [
            row.object_key for row in FileObject.objects.filter(project=project)
        ] + [row.object_key for row in FileVersion.objects.filter(project=project)]
        assert project_keys
        assert all(PROJECT_PREFIX_MARKER in key for key in project_keys)
        assert legacy_key not in project_keys

        # And the legacy download still works after all of it.
        response = session_client.get(
            attachment_download_url(project.workspace.slug, project.id, issue.id, asset.id)
        )
        assert response.status_code == status.HTTP_302_FOUND
        assert fetch_through(response["Location"]).content == PNG_BYTES
