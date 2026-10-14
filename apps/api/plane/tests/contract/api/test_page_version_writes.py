"""API-token content writes must create the same history as editor writes."""

import base64
from types import SimpleNamespace

import pytest

from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page, PageVersion, Project, ProjectMember, ProjectPage


@pytest.fixture
def page_context(workspace, create_user):
    project = Project.objects.create(name="Page history", identifier="HIST", workspace=workspace)
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20)
    page = Page.objects.create(workspace=workspace, owned_by=create_user, description_html="<p>Before</p>")
    ProjectPage.objects.create(workspace=workspace, project=project, page=page)
    return project, page


@pytest.mark.contract
@pytest.mark.django_db
@pytest.mark.parametrize("project_scope", [True, False])
def test_token_content_write_dispatches_history_after_commit(
    api_key_client, workspace, create_user, page_context, project_scope, monkeypatch, mocker,
    django_capture_on_commit_callbacks,
):
    project, page = page_context
    monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "test-live-secret")
    converted = {
        "description_html": "<p>After</p>",
        "description_json": {"type": "doc", "content": [{"type": "paragraph"}]},
        "description_binary": base64.b64encode(b"converted-yjs-state").decode(),
    }
    mocker.patch("plane.api.views.page.requests.post", return_value=SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: converted,
    ))
    mocker.patch("plane.api.views.page.page_transaction.delay")
    dispatched = mocker.patch("plane.api.views.page.track_page_version.delay", side_effect=track_page_version)
    scope = f"projects/{project.id}/" if project_scope else ""
    url = f"/api/v1/workspaces/{workspace.slug}/{scope}pages/{page.id}/"
    with django_capture_on_commit_callbacks(execute=True):
        result = api_key_client.patch(url, {"description_html": "<p>After</p>"}, format="json")
        assert result.status_code == 200, result.data
        assert not dispatched.called
    assert dispatched.call_count == 1
    version = PageVersion.objects.get(page=page)
    assert version.description_html == converted["description_html"]
    assert version.description_json == converted["description_json"]
    assert bytes(version.description_binary) == b"converted-yjs-state"
    assert version.owned_by_id == create_user.id


@pytest.mark.contract
@pytest.mark.django_db
def test_token_metadata_write_does_not_queue_content_history(
    api_key_client, workspace, page_context, mocker, django_capture_on_commit_callbacks,
):
    project, page = page_context
    dispatched = mocker.patch("plane.api.views.page.track_page_version.delay")
    url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/"
    with django_capture_on_commit_callbacks(execute=True):
        result = api_key_client.patch(url, {"name": "Renamed"}, format="json")
    assert result.status_code == 200
    assert not dispatched.called
    assert not PageVersion.objects.filter(page=page).exists()
