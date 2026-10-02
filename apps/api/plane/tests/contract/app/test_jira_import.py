"""Exercise real Plane models and import endpoints; only external Jira/storage/Live are substituted."""

import base64
import io
import os
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from django.utils import timezone
from plane.bgtasks.jira_import_task import jira_import_task
from plane.db.models import (
    Cycle,
    CycleIssue,
    FileObject,
    Issue,
    IssueAssignee,
    IssueComment,
    IssueDescriptionVersion,
    IssueLabel,
    IssueRelation,
    IssueVersion,
    JiraRun,
    JiraSource,
    Project,
    ProjectMember,
    WorkspaceMember,
)
from plane.license.models import Instance, InstanceAdmin, InstanceConfiguration
from plane.utils.confluence.client import ConfluenceError
from plane.utils.confluence.jobs import recover_stale_runs
from plane.utils.jira.config import CONFIG_FIELDS, save_jira_config
from plane.utils.jira.jobs import serialize_run

pytestmark = [pytest.mark.contract, pytest.mark.django_db]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class MemoryStorage:
    objects = {}

    def __init__(self, request=None):
        pass

    def upload_file(self, stream, key, extra_args=None, content_type=None):
        self.objects[key] = (stream.read(), content_type)
        return True

    def get_object_metadata(self, key):
        if key not in self.objects:
            return None
        value, mime = self.objects[key]
        return {"ContentLength": len(value), "ContentType": mime, "ETag": "jira-test-etag"}

    def get_object_head_bytes(self, key, size):
        return self.objects[key][0][:size]


class FakeJira:
    def __init__(self, user):
        self.sprint = {
            "id": 10,
            "name": "Sprint 10",
            "state": "active",
            "startDate": "2026-10-01T00:00:00Z",
            "endDate": "2026-10-14T00:00:00Z",
            "goal": "Launch",
        }
        account = {"accountId": "jira-user", "displayName": "Original Jira user", "emailAddress": user.email}
        fields = {
            "summary": "Parent",
            "updated": "2026-10-02T00:00:00Z",
            "project": {"id": "100"},
            "status": {"id": "1", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            "priority": {"name": "High"},
            "labels": ["backend"],
            "assignee": account,
            "reporter": account,
            "issuetype": {"name": "Task"},
            "customfield_10020": [self.sprint],
            "attachment": [
                {
                    "id": "300",
                    "filename": "diagram.png",
                    "size": len(PNG),
                    "mimeType": "image/png",
                    "content": "https://team.atlassian.net/secure/attachment/300/diagram.png",
                }
            ],
        }
        self.rows = [
            {
                "id": "1",
                "key": "ENG-1",
                "fields": fields,
                "renderedFields": {
                    "description": '<p>Before</p><img src="/secure/attachment/300/diagram.png"><p>After</p>'
                },
            },
            {
                "id": "2",
                "key": "ENG-2",
                "fields": {**fields, "summary": "Subtask", "attachment": [], "parent": {"id": "1"}},
                "renderedFields": {"description": "<p>Child</p>"},
            },
        ]
        self.comments = [
            {
                "id": "200",
                "body": {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Comment text"}]}],
                },
                "author": account,
                "created": "2026-10-01T01:00:00Z",
                "updated": "2026-10-01T01:00:00Z",
            }
        ]
        self.fail_download = False
        self.fail_comments = False
        self.fail_sprints = False
        self.downloaded = []

    def json(self, path, params=None):
        if path.endswith("/field"):
            return [{"id": "customfield_10020", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}}]
        if path.endswith("/project/100"):
            return {"id": "100", "key": "ENG", "name": "Engineering"}
        return {"values": [{"id": "100", "key": "ENG", "name": "Engineering"}], "isLast": True}

    def pages(self, path, params=None, key="values"):
        if path.endswith("/board"):
            if self.fail_sprints:
                raise ConfluenceError("atlassian_http_403", "Sprint permission denied.")
            yield {"id": 50, "name": "Engineering board", "type": "scrum"}
        elif path.endswith("/sprint"):
            yield deepcopy(self.sprint)
        elif path.endswith("/comment"):
            if self.fail_comments:
                raise ConfluenceError("atlassian_http_403", "Comment permission denied.")
            if "/issue/1/" in path:
                yield from deepcopy(self.comments)

    def issues(self, project_id, fields):
        yield from deepcopy(self.rows)

    def download(self, remote, limit):
        self.downloaded.append(remote["id"])
        if self.fail_download:
            raise ConfluenceError("atlassian_http_403", "Attachment permission denied.")
        return io.BytesIO(PNG), len(PNG)


@pytest.fixture
def jira_context(workspace, create_user, monkeypatch):
    project = Project.objects.create(workspace=workspace, name="Jira", identifier="JIRA")
    ProjectMember.objects.create(project=project, workspace=workspace, member=create_user, role=20, is_active=True)
    source = JiraSource.objects.create(
        project=project,
        owner=create_user,
        site_url="https://team.atlassian.net",
        remote_project_id="100",
        project_key="ENG",
        project_name="Engineering",
    )
    config = {
        "enabled": True,
        "site_url": source.site_url,
        "email": "test@example.com",
        "api_token": "token",
        "cloud_id": "",
    }
    jira = FakeJira(create_user)
    for module in ("plane.bgtasks.jira_import_task", "plane.app.views.jira"):
        monkeypatch.setattr(module + ".get_jira_config", lambda **kwargs: config)
        monkeypatch.setattr(module + ".JiraClient", lambda values: jira)
    MemoryStorage.objects = {}
    for module in ("plane.utils.confluence.storage", "plane.app.views.file.upload", "plane.app.views.file.versions"):
        monkeypatch.setattr(module + ".S3Storage", MemoryStorage)
    monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "jira-test-secret")

    def convert(url, *, json, headers, timeout):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "description_html": json["description_html"],
                "description_json": {"type": "doc", "content": []},
                "description_binary": base64.b64encode(b"jira-editor-document").decode(),
            },
        )

    if os.environ.get("JIRA_TEST_LIVE_URL"):
        monkeypatch.setenv("PLANE_YJS_REPLACE_URL", os.environ["JIRA_TEST_LIVE_URL"])
    else:
        monkeypatch.setattr("plane.utils.jira.content.requests.post", convert)
    return SimpleNamespace(project=project, user=create_user, source=source, api=jira)


def run_import(context, mode="changed", selection=None):
    run = JiraRun.objects.create(source=context.source, initiated_by=context.user, mode=mode, selection=selection or [])
    jira_import_task(str(run.pk))
    run.refresh_from_db()
    return run


def test_import_maps_hierarchy_state_labels_users_comments_files_and_sprint(jira_context):
    jira_context.api.rows[0]["fields"]["issuelinks"] = [
        {"id": "400", "type": {"name": "Blocks"}, "outwardIssue": {"id": "2"}},
    ]
    jira_context.api.rows[1]["fields"]["issuelinks"] = [
        {"id": "400", "type": {"name": "Blocks"}, "inwardIssue": {"id": "1"}},
    ]
    run = run_import(jira_context)
    assert run.status == "completed", list(run.results.values("status", "error_code", "error_message"))
    assert serialize_run(run)["counts"]["completed"] == 5
    parent = run.source.items.get(kind="issue", remote_id="1").issue
    child = run.source.items.get(kind="issue", remote_id="2").issue
    file = run.source.items.get(kind="attachment").file
    assert child.parent_id == parent.pk and parent.state.group == "started" and parent.priority == "high"
    assert IssueRelation.objects.count() == 1
    assert IssueRelation.objects.filter(issue=child, related_issue=parent, relation_type="blocked_by").exists()
    assert IssueLabel.objects.filter(issue=parent, label__name="backend").exists()
    assert IssueAssignee.objects.filter(issue=parent, assignee=jira_context.user).exists()
    assert IssueComment.objects.get().actor_id == jira_context.user.pk
    assert f"project-file:{file.id}" in parent.description_html
    assert (
        parent.description_html.index("Before")
        < parent.description_html.index("project-file:")
        < parent.description_html.index("After")
    )
    assert file.links.filter(entity_type="issue", entity_id=parent.id).exists()
    assert Cycle.objects.get().name == "Sprint 10" and CycleIssue.objects.count() == 2
    jira_context.project.refresh_from_db()
    assert jira_context.project.cycle_view


def test_unchanged_sync_and_full_overwrite_keep_stable_ids_and_versions(jira_context):
    assert run_import(jira_context).status == "completed"
    before = list(jira_context.source.items.order_by("kind").values("issue_id", "comment_id", "cycle_id", "file_id"))
    skipped = run_import(jira_context)
    assert skipped.results.filter(status="skipped").count() == 5
    assert run_import(jira_context, "all").status == "completed"
    assert (
        list(jira_context.source.items.order_by("kind").values("issue_id", "comment_id", "cycle_id", "file_id"))
        == before
    )
    assert Issue.objects.count() == 2 and IssueComment.objects.count() == 1 and Cycle.objects.count() == 1
    assert FileObject.objects.get().current_version_no == 2 and IssueDescriptionVersion.objects.count() == 2
    assert IssueVersion.objects.count() == 2


def test_failed_attachment_is_visible_and_selected_retry_repairs_owner(jira_context):
    jira_context.api.fail_download = True
    failed = run_import(jira_context)
    assert failed.status == "partial"
    result = failed.results.get(item__kind="attachment")
    assert result.error_code == "atlassian_http_403"
    jira_context.api.fail_download = False
    retry = run_import(jira_context, "selected", [str(result.item_id)])
    assert retry.status == "completed", list(retry.results.values("error_code", "error_message"))
    assert retry.results.get(item__kind="attachment").status == "completed"
    assert retry.results.get(item__kind="issue", item__remote_id="1").status == "completed"
    assert retry.results.get(item__kind="issue", item__remote_id="2").status == "skipped"


def test_changed_sprint_updates_same_cycle_and_relinks_work_items(jira_context):
    assert run_import(jira_context).status == "completed"
    cycle_id = Cycle.objects.get().pk
    jira_context.api.sprint["name"] = "Renamed sprint"
    changed = run_import(jira_context)
    assert changed.status == "completed"
    assert Cycle.objects.get().pk == cycle_id and Cycle.objects.get().name == "Renamed sprint"


def test_unmapped_user_keeps_author_attribution_and_can_be_mapped_on_sync(jira_context):
    for issue in jira_context.api.rows:
        issue["fields"]["assignee"] = {"accountId": "hidden-user", "displayName": "Hidden email"}
    jira_context.api.comments[0]["author"] = {"accountId": "hidden-user", "displayName": "Original author"}
    run = run_import(jira_context)
    assert run.status == "completed"
    assert not IssueAssignee.objects.exists() and IssueComment.objects.get().actor_id is None
    assert "Original author" in IssueComment.objects.get().comment_html
    assert run.results.filter(warning__contains="hidden-user").count() == 3
    jira_context.source.user_mapping = {"hidden-user": str(jira_context.user.id)}
    jira_context.source.save()
    assert run_import(jira_context).status == "completed"
    assert IssueAssignee.objects.count() == 2 and IssueComment.objects.get().actor_id == jira_context.user.id


def test_comment_and_agile_inventory_failures_are_visible_and_recover(jira_context):
    jira_context.api.fail_comments = True
    jira_context.api.fail_sprints = True
    run = run_import(jira_context)
    assert run.status == "partial" and not run.inventory_complete
    assert run.results.filter(item__kind="inventory", status="failed").count() == 3
    jira_context.api.fail_comments = False
    jira_context.api.fail_sprints = False
    recovered = run_import(jira_context, "failed")
    assert recovered.status == "completed" and recovered.inventory_complete
    assert IssueComment.objects.count() == 1


def test_revoked_project_membership_stops_worker(jira_context):
    ProjectMember.objects.filter(project=jira_context.project).update(is_active=False)
    run = run_import(jira_context)
    assert run.status == "failed" and run.error_code == "permission_denied"
    assert not Issue.objects.exists() and not Cycle.objects.exists()


def test_api_scopes_source_selected_items_and_prevents_duplicate_jobs(jira_context, session_client, monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr("plane.app.views.jira.jira_import_task.delay", enqueue)
    url = f"/api/workspaces/{jira_context.project.workspace.slug}/projects/{jira_context.project.id}/jira/runs/"
    body = {"source_id": str(jira_context.source.pk), "mode": "all"}
    assert session_client.post(url, body, format="json").status_code == 202
    assert session_client.post(url, body, format="json").status_code == 409 and enqueue.call_count == 1
    other = Project.objects.create(workspace=jira_context.project.workspace, name="Other", identifier="OTHER")
    ProjectMember.objects.create(
        project=other, workspace=other.workspace, member=jira_context.user, role=20, is_active=True
    )
    other_url = f"/api/workspaces/{other.workspace.slug}/projects/{other.id}/jira/runs/"
    assert session_client.post(other_url, body, format="json").status_code == 404
    assert (
        session_client.post(
            other_url,
            {"remote_project_id": "100", "user_mapping": {"account": "00000000-0000-4000-8000-000000000000"}},
            format="json",
        ).status_code
        == 400
    )


def test_queue_failure_and_stale_worker_are_retryable(jira_context, session_client, monkeypatch):
    monkeypatch.setattr("plane.app.views.jira.jira_import_task.delay", Mock(side_effect=RuntimeError("broker")))
    url = f"/api/workspaces/{jira_context.project.workspace.slug}/projects/{jira_context.project.id}/jira/runs/"
    response = session_client.post(url, {"source_id": str(jira_context.source.pk)}, format="json")
    assert response.status_code == 503 and response.data["run"]["error_code"] == "queue_unavailable"
    run = JiraRun.objects.create(source=jira_context.source, initiated_by=jira_context.user)
    JiraRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
    recover_stale_runs(JiraRun.objects.all())
    run.refresh_from_db()
    assert run.status == "failed" and run.error_code == "worker_interrupted"
    assert run_import(jira_context).status == "completed"


def test_config_encrypts_masks_preserves_token_and_requires_god_mode(jira_context, session_client):
    instance = Instance.objects.create(instance_name="Test", instance_id="jira-test", last_checked_at=timezone.now())
    InstanceAdmin.objects.create(instance=instance, user=jira_context.user, role=20)
    saved = save_jira_config(
        {
            "enabled": True,
            "site_url": "https://team.atlassian.net",
            "email": "user@example.com",
            "api_token": "jira-sensitive",
        }
    )
    assert saved["token_configured"] and "api_token" not in saved
    token = InstanceConfiguration.objects.get(key=CONFIG_FIELDS["api_token"])
    assert token.is_encrypted and "jira-sensitive" not in token.value
    save_jira_config({"email": "changed@example.com"})
    from plane.license.api.serializers.configuration import InstanceConfigurationSerializer

    assert InstanceConfigurationSerializer(token).data["value"] == ""
    assert (
        session_client.patch("/api/instances/configurations/", {"JIRA_API_TOKEN": "leak"}, format="json").status_code
        == 400
    )
    InstanceAdmin.objects.all().delete()
    assert session_client.get("/api/instances/jira/").status_code == 403


def test_changed_issue_updates_existing_work_item(jira_context):
    assert run_import(jira_context).status == "completed"
    item = jira_context.source.items.get(kind="issue", remote_id="1")
    old_id = item.issue_id
    jira_context.api.rows[0]["fields"].update(summary="Updated title", labels=["new-label"], priority={"name": "Low"})
    assert run_import(jira_context).status == "completed"
    issue = Issue.objects.get(pk=old_id)
    assert issue.name == "Updated title" and issue.priority == "low"
    assert list(IssueLabel.objects.filter(issue=issue).values_list("label__name", flat=True)) == ["new-label"]
    assert Issue.objects.count() == 2


def test_failed_editor_conversion_preserves_previous_content(jira_context, monkeypatch):
    assert run_import(jira_context).status == "completed"
    issue = jira_context.source.items.get(kind="issue", remote_id="1").issue
    old_html = issue.description_html
    old_versions = IssueDescriptionVersion.objects.count()
    jira_context.api.rows[0]["fields"]["summary"] = "Must roll back"
    monkeypatch.setattr(
        "plane.utils.jira.destinations.convert_document",
        Mock(side_effect=ConfluenceError("editor_unavailable", "Plane editor is unavailable.")),
    )
    run = run_import(jira_context)
    assert run.status == "partial" and run.results.get(item__remote_id="1", item__kind="issue").status == "failed"
    issue.refresh_from_db()
    assert issue.name == "Parent" and issue.description_html == old_html
    assert IssueDescriptionVersion.objects.count() == old_versions


def test_run_details_filter_validate_selection_and_require_membership(jira_context, session_client):
    jira_context.api.fail_download = True
    run = run_import(jira_context)
    url = f"/api/workspaces/{jira_context.project.workspace.slug}/projects/{jira_context.project.id}/jira/runs/"
    detail = session_client.get(f"{url}{run.id}/", {"status": "failed"})
    assert detail.status_code == 200 and detail.data["count"] >= 1
    assert all(item["status"] == "failed" for item in detail.data["results"])
    assert session_client.get(f"{url}{run.id}/", {"offset": "invalid"}).status_code == 400
    assert (
        session_client.post(
            url,
            {"source_id": str(jira_context.source.id), "mode": "selected", "item_ids": [str(run.id)]},
            format="json",
        ).status_code
        == 400
    )
    ProjectMember.objects.filter(project=jira_context.project).update(role=5)
    WorkspaceMember.objects.filter(workspace=jira_context.project.workspace, member=jira_context.user).update(role=5)
    assert session_client.get(url).status_code == 403


def test_failed_sprint_update_rolls_back_its_member_work_items(jira_context, monkeypatch):
    assert run_import(jira_context).status == "completed"
    jira_context.api.sprint["name"] = "Failed rename"
    jira_context.api.rows[0]["fields"]["summary"] = "Must roll back"
    monkeypatch.setattr(
        "plane.bgtasks.jira_import_task.save_sprint",
        Mock(side_effect=ConfluenceError("cycle_failed", "Cycle update failed.")),
    )
    run = run_import(jira_context)
    assert run.status == "partial"
    assert run.results.filter(item__kind="issue", error_code="sprint_not_imported").count() == 2
    assert Cycle.objects.get().name == "Sprint 10"
    assert jira_context.source.items.get(kind="issue", remote_id="1").issue.name == "Parent"


def test_comment_update_does_not_overwrite_local_work_item_edits(jira_context):
    assert run_import(jira_context).status == "completed"
    issue = jira_context.source.items.get(kind="issue", remote_id="1").issue
    Issue.objects.filter(pk=issue.id).update(name="Local title")
    jira_context.api.comments[0]["updated"] = "2026-10-02T02:00:00Z"
    jira_context.api.comments[0]["body"]["content"][0]["content"][0]["text"] = "Updated comment"
    run = run_import(jira_context)
    assert run.status == "completed"
    assert run.results.get(item__kind="issue", item__remote_id="1").status == "skipped"
    assert Issue.objects.get(pk=issue.id).name == "Local title"
    assert "Updated comment" in IssueComment.objects.get().comment_html
