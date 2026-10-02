"""Exercise durable jobs and actual upload/quota/version logic with fake external providers."""

import base64
import io
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from django.utils import timezone
from django.core.cache.backends.locmem import LocMemCache
from plane.bgtasks.confluence_import_task import confluence_import_task
from plane.db.models import (
    ConfluenceRun,
    ConfluenceSource,
    FileObject,
    FileVersion,
    Page,
    PageVersion,
    Project,
    ProjectMember,
)
from plane.license.models import Instance, InstanceAdmin, InstanceConfiguration
from plane.utils.confluence.client import ConfluenceClient, ConfluenceError
from plane.utils.confluence.config import CONFIG_FIELDS, save_confluence_config
from plane.utils.confluence.jobs import recover_stale_runs, serialize_run

pytestmark = [pytest.mark.contract, pytest.mark.django_db]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\nhello"


class MemoryStorage:
    objects = {}

    def __init__(self, request=None):
        pass

    def generate_presigned_put(self, object_name, content_type, expires_in=None):
        return {"url": "https://store.example/put", "method": "PUT", "headers": {}}

    def upload_file(self, stream, key, extra_args=None, content_type=None):
        self.objects[key] = (stream.read(), content_type)
        return True

    def get_object_metadata(self, key):
        if key not in self.objects:
            return None
        data, mime = self.objects[key]
        return {"ContentLength": len(data), "ContentType": mime, "ETag": "test-etag"}

    def get_object_head_bytes(self, key, size):
        return self.objects[key][0][:size]


class FakeAtlassian:
    def __init__(self):
        self.pages = [
            {"id": "1", "title": "Parent", "parentId": None, "version": {"number": 1}},
            {"id": "2", "title": "Child", "parentId": "1", "version": {"number": 1}},
        ]
        self.attachments = [
            {
                "id": "att3",
                "pageId": "1",
                "title": "diagram.png",
                "mediaType": "image/png",
                "fileSize": len(PNG),
                "downloadLink": "/wiki/download/attachments/1/diagram.png",
                "version": {"number": 1},
            },
            {
                "id": "att4",
                "pageId": "1",
                "title": "notes.pdf",
                "mediaType": "application/pdf",
                "fileSize": len(PDF),
                "downloadLink": "/wiki/download/attachments/1/notes.pdf",
                "version": {"number": 1},
            },
        ]
        self.fail_download = set()
        self.fail_inventory = False
        self.downloaded = []

    def paginate(self, path, params=None):
        if path.endswith("/pages"):
            yield from self.pages
        elif self.fail_inventory:
            raise ConfluenceError("atlassian_http_403", "Attachment permission denied.")
        elif "/pages/1/" in path:
            yield from self.attachments

    def json(self, path, params=None):
        if "/spaces/" in path:
            return {"id": "100", "name": "Engineering"}
        page = next(row for row in self.pages if path.endswith("/" + row["id"]))
        html = (
            '<p>Before</p><img src="/wiki/download/thumbnails/1/diagram.png"><p>After</p>'
            if page["id"] == "1"
            else '<p>Child body</p><a href="/wiki/spaces/ENG/pages/1">Parent</a>'
        )
        return {**page, "body": {"view": {"value": html}}}

    def download(self, attachment, limit):
        self.downloaded.append(attachment["id"])
        if attachment["id"] in self.fail_download:
            raise ConfluenceError("atlassian_http_403", "Attachment permission denied.")
        data = PNG if attachment["mediaType"] == "image/png" else PDF
        return io.BytesIO(data), len(data)


@pytest.fixture
def import_context(workspace, create_user, monkeypatch):
    project = Project.objects.create(workspace=workspace, name="Confluence", identifier="CONF")
    ProjectMember.objects.create(project=project, workspace=workspace, member=create_user, role=20, is_active=True)
    source = ConfluenceSource.objects.create(
        project=project,
        owner=create_user,
        site_url="https://team.atlassian.net",
        space_id="100",
        space_name="Engineering",
    )
    config = {
        "enabled": True,
        "site_url": source.site_url,
        "email": "test@example.com",
        "api_token": "token",
        "cloud_id": "",
    }
    atlassian = FakeAtlassian()
    monkeypatch.setattr("plane.bgtasks.confluence_import_task.get_confluence_config", lambda **kwargs: config)
    monkeypatch.setattr("plane.bgtasks.confluence_import_task.ConfluenceClient", lambda values: atlassian)
    monkeypatch.setattr("plane.app.views.confluence.get_confluence_config", lambda **kwargs: config)
    monkeypatch.setattr("plane.app.views.confluence.ConfluenceClient", lambda values: atlassian)
    MemoryStorage.objects = {}
    monkeypatch.setattr("plane.utils.confluence.storage.S3Storage", MemoryStorage)
    monkeypatch.setattr("plane.app.views.file.upload.S3Storage", MemoryStorage)
    monkeypatch.setattr("plane.app.views.file.versions.S3Storage", MemoryStorage)
    monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "test-secret")

    def convert(url, *, json, headers, timeout):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "description_html": json["description_html"],
                "description_json": {"type": "doc", "content": []},
                "description_binary": base64.b64encode(b"test-converted-binary").decode(),
            },
        )

    if os.environ.get("CONFLUENCE_TEST_LIVE_URL"):
        monkeypatch.setenv("PLANE_YJS_REPLACE_URL", os.environ["CONFLUENCE_TEST_LIVE_URL"])
        monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "confluence-test-secret")
    else:
        monkeypatch.setattr("plane.utils.confluence.documents.requests.post", convert)
    return SimpleNamespace(project=project, user=create_user, source=source, api=atlassian)


def run_import(context, mode="changed", selection=None):
    run = ConfluenceRun.objects.create(
        source=context.source, initiated_by=context.user, mode=mode, selection=selection or []
    )
    confluence_import_task(str(run.pk))
    run.refresh_from_db()
    return run


def test_initial_import_preserves_hierarchy_media_and_type_counts(import_context):
    run = run_import(import_context)
    assert run.status == "completed", list(run.results.values("status", "error_message"))
    counts = serialize_run(run)
    assert counts["counts"]["completed"] == 4
    assert counts["types"]["page"]["total"] == 2 and counts["types"]["image"]["completed"] == 1
    assert counts["types"]["application/pdf"]["completed"] == 1
    parent = run.source.items.get(kind="page", remote_id="1")
    child = run.source.items.get(kind="page", remote_id="2")
    image = run.source.items.get(remote_id="att3")
    assert child.page.parent_id == parent.folder_id
    assert parent.page.parent_id == parent.folder_id
    assert parent.folder.parent_id == run.source.root_id
    assert (
        parent.page.description_html.index("Before")
        < parent.page.description_html.index(f"project-file:{image.file_id}")
        < parent.page.description_html.index("After")
    )
    assert str(parent.page_id) in child.page.description_html
    assert image.file.links.filter(entity_id=parent.page_id).exists()
    assert image.file.current_version_no == 1


def test_unchanged_sync_skips_and_overwrite_preserves_ids_and_old_bytes(import_context):
    first = run_import(import_context)
    assert first.status == "completed"
    mappings = list(first.source.items.order_by("remote_id").values("remote_id", "page_id", "file_id"))
    old_objects = dict(MemoryStorage.objects)
    import_context.api.downloaded.clear()
    unchanged = run_import(import_context)
    assert unchanged.status == "completed" and unchanged.results.filter(status="skipped").count() == 4
    assert import_context.api.downloaded == []
    overwrite = run_import(import_context, "all")
    assert overwrite.status == "completed", list(overwrite.results.values("error_message"))
    assert list(first.source.items.order_by("remote_id").values("remote_id", "page_id", "file_id")) == mappings
    assert all(MemoryStorage.objects[key] == value for key, value in old_objects.items())
    assert FileObject.objects.filter(current_version_no=2).count() == 2
    assert FileVersion.objects.filter(is_active=True).count() == 2
    assert FileVersion.objects.filter(status="superseded").count() == 2
    assert PageVersion.objects.count() == 4


def test_failed_attachment_retry_repairs_page_and_reuses_failed_file(import_context):
    import_context.api.fail_download.add("att3")
    first = run_import(import_context)
    assert first.status == "partial"
    assert first.results.filter(status="failed").count() == 2
    failed = first.results.get(item__remote_id="att3")
    assert failed.error_code == "atlassian_http_403" and "permission" in failed.error_message
    import_context.api.fail_download.clear()
    import_context.api.downloaded.clear()
    retry = run_import(import_context, "selected", [str(failed.item_id)])
    assert retry.status == "completed", list(retry.results.values("error_message"))
    assert import_context.api.downloaded == ["att3"]
    assert retry.results.get(item__remote_id="1").status == "completed"
    assert retry.results.get(item__remote_id="att4").status == "skipped"


def test_changed_attachment_sync_updates_same_file_and_owner_page(import_context):
    first = run_import(import_context)
    file_id = first.source.items.get(remote_id="att3").file_id
    import_context.api.attachments[0]["version"]["number"] = 2
    import_context.api.downloaded.clear()
    update = run_import(import_context)
    assert update.status == "completed"
    assert import_context.api.downloaded == ["att3"]
    assert update.source.items.get(remote_id="att3").file_id == file_id
    assert FileObject.objects.get(pk=file_id).current_version_no == 2
    assert update.results.get(item__remote_id="1").status == "completed"


def test_inventory_failure_is_reported_with_incomplete_totals(import_context):
    import_context.api.fail_inventory = True
    run = run_import(import_context)
    assert run.status == "partial" and not run.inventory_complete
    assert run.results.filter(error_code="attachment_inventory_failed", status="failed").count() == 2


def test_revoked_project_membership_stops_worker(import_context):
    ProjectMember.objects.filter(project=import_context.project).update(is_active=False)
    run = run_import(import_context)
    assert run.status == "failed" and run.error_code == "permission_denied"
    assert not Page.objects.exists() and not FileObject.objects.exists()


def test_stale_worker_records_failure_and_new_run_can_start(import_context):
    run = ConfluenceRun.objects.create(source=import_context.source, initiated_by=import_context.user)
    ConfluenceRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
    recover_stale_runs(ConfluenceRun.objects.all())
    run.refresh_from_db()
    assert run.status == "failed" and run.error_code == "worker_interrupted"
    assert run_import(import_context).status == "completed"


def test_api_enforces_project_scope_and_detects_duplicate_jobs(import_context, session_client, monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr("plane.app.views.confluence.confluence_import_task.delay", enqueue)
    url = (
        f"/api/workspaces/{import_context.project.workspace.slug}/projects/{import_context.project.id}/confluence/runs/"
    )
    payload = {"source_id": str(import_context.source.id), "mode": "all"}
    first = session_client.post(url, payload, format="json")
    assert first.status_code == 202, first.data
    duplicate = session_client.post(url, payload, format="json")
    assert duplicate.status_code == 409 and enqueue.call_count == 1
    other = Project.objects.create(workspace=import_context.project.workspace, name="Other", identifier="OTHER")
    ProjectMember.objects.create(
        project=other, workspace=other.workspace, member=import_context.user, role=20, is_active=True
    )
    foreign_url = f"/api/workspaces/{other.workspace.slug}/projects/{other.id}/confluence/runs/"
    assert session_client.post(foreign_url, payload, format="json").status_code == 404


def test_queue_failure_is_a_visible_failed_run(import_context, session_client, monkeypatch):
    monkeypatch.setattr(
        "plane.app.views.confluence.confluence_import_task.delay", Mock(side_effect=RuntimeError("broker"))
    )
    url = (
        f"/api/workspaces/{import_context.project.workspace.slug}/projects/{import_context.project.id}/confluence/runs/"
    )
    response = session_client.post(url, {"source_id": str(import_context.source.id)}, format="json")
    assert response.status_code == 503 and response.data["run"]["error_code"] == "queue_unavailable"
    assert ConfluenceRun.objects.get().status == "failed"


def test_config_token_is_encrypted_masked_preserved_and_admin_only(import_context, session_client):
    instance = Instance.objects.create(
        instance_name="Test", instance_id="test-instance", last_checked_at=timezone.now()
    )
    InstanceAdmin.objects.create(instance=instance, user=import_context.user, role=20)
    values = {
        "enabled": True,
        "site_url": "https://team.atlassian.net/wiki",
        "email": "user@example.com",
        "api_token": "secret-token",
    }
    saved = save_confluence_config(values)
    assert saved["token_configured"] and "api_token" not in saved
    token = InstanceConfiguration.objects.get(key=CONFIG_FIELDS["api_token"])
    assert token.is_encrypted and "secret-token" not in token.value
    save_confluence_config({"email": "new@example.com"})
    token.refresh_from_db()
    assert token.value and "secret-token" not in token.value
    from plane.license.api.serializers.configuration import InstanceConfigurationSerializer

    assert InstanceConfigurationSerializer(token).data["value"] == ""
    InstanceAdmin.objects.all().delete()
    assert session_client.get("/api/instances/confluence/").status_code == 403


def test_upload_failure_retry_reuses_file_row_and_releases_quota(import_context, monkeypatch):
    original_upload = MemoryStorage.upload_file

    def fail_image(self, stream, key, extra_args=None, content_type=None):
        return False if content_type == "image/png" else original_upload(self, stream, key, extra_args, content_type)

    monkeypatch.setattr(MemoryStorage, "upload_file", fail_image)
    failed_run = run_import(import_context)
    image = failed_run.source.items.get(remote_id="att3")
    destination_id = image.file_id
    assert destination_id is not None
    assert failed_run.results.get(item=image).error_code == "storage_upload_failed"
    from plane.db.models import ProjectStorageUsage

    assert ProjectStorageUsage.objects.get(project=import_context.project).reserved_bytes == 0
    monkeypatch.setattr(MemoryStorage, "upload_file", original_upload)
    retry = run_import(import_context, "failed")
    assert retry.status == "completed", list(retry.results.values("error_message"))
    image.refresh_from_db()
    assert image.file_id == destination_id and FileObject.objects.count() == 2
    assert image.file.current_version_no == 2


def test_failure_keeps_previous_page_content_and_versions(import_context, monkeypatch):
    assert run_import(import_context).status == "completed"
    item = import_context.source.items.get(kind="page", remote_id="1")
    original = item.page.description_html
    versions = PageVersion.objects.filter(page=item.page).count()
    import_context.api.pages[0]["version"]["number"] = 2
    monkeypatch.setattr(
        "plane.bgtasks.confluence_import_task.replace_imported_page",
        Mock(side_effect=ConfluenceError("live_conversion_failed", "Live unavailable.")),
    )
    failed_run = run_import(import_context)
    item.page.refresh_from_db()
    assert failed_run.results.get(item=item).error_code == "live_conversion_failed"
    assert item.page.description_html == original and PageVersion.objects.filter(page=item.page).count() == versions


def test_api_returns_paginated_status_and_errors(import_context, session_client):
    import_context.api.fail_download.add("att3")
    run = run_import(import_context)
    project = import_context.project
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/confluence/runs/{run.pk}/"
    response = session_client.get(url, {"status": "failed", "offset": "0"})
    assert response.status_code == 200
    assert response.data["count"] == 2 and response.data["run"]["counts"]["failed"] == 2
    assert {row["error_code"] for row in response.data["results"]} == {"atlassian_http_403", "media_dependency_failed"}
    assert session_client.get(url, {"offset": "invalid"}).status_code == 400


def test_disabled_config_cannot_be_enabled_without_a_token(import_context):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        save_confluence_config(
            {"enabled": True, "site_url": "https://team.atlassian.net", "email": "user@example.com", "api_token": ""}
        )
    assert not InstanceConfiguration.objects.filter(key=CONFIG_FIELDS["enabled"], value="1").exists()


def test_overwrite_keeps_deduplicated_names_stable(import_context):
    FileObject.objects.create(
        project=import_context.project,
        name_original="diagram.png",
        name_display="diagram.png",
        name_normalized="diagram.png",
        mime_type="image/png",
        bucket="uploads",
        object_key="unrelated-file",
        status="active",
    )
    first = run_import(import_context)
    assert first.status == "completed"
    image = first.source.items.get(remote_id="att3")
    assert image.file.name_display == "diagram (2).png"
    assert run_import(import_context, "all").status == "completed"
    image.file.refresh_from_db()
    assert image.file.name_display == "diagram (2).png"


def test_sync_adds_new_page_under_an_existing_parent(import_context):
    first = run_import(import_context)
    previous_ids = {item.remote_id: item.page_id for item in first.source.items.filter(kind="page")}
    import_context.api.pages.append({"id": "5", "title": "New child", "parentId": "2", "version": {"number": 1}})
    synced = run_import(import_context)
    assert synced.status == "completed"
    assert synced.results.filter(status="completed").count() == 1
    assert synced.results.filter(status="skipped").count() == 4
    for remote_id, page_id in previous_ids.items():
        assert synced.source.items.get(remote_id=remote_id).page_id == page_id
    child = synced.source.items.get(remote_id="5")
    assert child.page.parent_id == synced.source.items.get(remote_id="2").folder_id


def test_spaces_endpoint_searches_beyond_first_page_and_enforces_project_access(
    import_context, session_client, monkeypatch
):
    client = ConfluenceClient(
        {"site_url": "https://team.atlassian.net", "email": "test@example.com", "api_token": "token"}
    )
    client.json = Mock(
        side_effect=[
            {
                "results": [{"id": "1", "name": "Unrelated", "key": "FIRST"}],
                "_links": {"next": "/wiki/api/v2/spaces?cursor=later"},
            },
            {"results": [{"id": "200", "name": "Later Engineering", "key": "ENG"}]},
        ]
    )
    local = LocMemCache("confluence-space-api-tests", {})
    local.clear()
    monkeypatch.setattr("plane.utils.confluence.spaces.cache", local)
    monkeypatch.setattr("plane.app.views.confluence.ConfluenceClient", lambda values: client)
    project = import_context.project
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/confluence/spaces/"
    result = session_client.get(url, {"search": "  engineering  "})
    assert result.status_code == 200 and result.data["results"] == [
        {"id": "200", "name": "Later Engineering", "key": "ENG"}
    ]
    assert result.data["count"] == 1 and client.json.call_count == 2
    assert session_client.get(url, {"search": "eng"}).data["results"][0]["id"] == "200"
    assert client.json.call_count == 2
    ProjectMember.objects.filter(project=project).update(is_active=False)
    assert session_client.get(url, {"search": "eng"}).status_code == 403


def test_spaces_endpoint_validates_search_and_keeps_browse_pagination(import_context, session_client, monkeypatch):
    client = Mock()
    client.json.return_value = {
        "results": [{"id": "200", "name": "Later Engineering", "key": "ENG"}],
        "_links": {"next": "/wiki/api/v2/spaces?cursor=next"},
    }
    monkeypatch.setattr("plane.app.views.confluence.ConfluenceClient", lambda values: client)
    project = import_context.project
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/confluence/spaces/"
    assert session_client.get(url, {"search": "x" * 201}).status_code == 400
    assert session_client.get(url, {"search": "eng", "cursor": "bad"}).status_code == 400
    client.json.assert_not_called()
    response = session_client.get(url, {"cursor": "previous"})
    assert response.status_code == 200 and response.data["next_cursor"] == "next"
    client.json.assert_called_once_with("/wiki/api/v2/spaces", {"limit": 100, "cursor": "previous"})
