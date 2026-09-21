# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import base64

import pytest
from rest_framework.test import APIRequestFactory

from plane.api.views.page import PageQuerysetMixin
from plane.db.models import Page, Project, ProjectMember, ProjectPage


@pytest.fixture
def page(db, workspace, create_user, mocker):
    mocker.patch("plane.app.views.page.base.page_transaction.delay")
    mocker.patch("plane.app.views.page.base.track_page_version.delay")
    mocker.patch("plane.api.views.page.page_transaction.delay")
    project = Project.objects.create(name="CAS", identifier="CAS", workspace=workspace, created_by=create_user)
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    page = Page.objects.create(workspace=workspace, owned_by=create_user, name="CAS page", access=0)
    ProjectPage.objects.create(page=page, project=project, workspace=workspace)
    page.test_url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/description/"
    return page


@pytest.mark.contract
@pytest.mark.django_db
class TestConditionalPageWrites:
    def test_second_writer_cannot_overwrite_first_writer(self, page, session_client):
        snapshot = session_client.get(page.test_url)
        assert snapshot.status_code == 200
        assert b"".join(snapshot.streaming_content) == b""
        etag = snapshot["ETag"]
        assert snapshot["X-Plane-Document-Version"] == etag
        first = session_client.patch(
            page.test_url, {"description_html": "<p>first writer</p>"}, format="json", HTTP_IF_MATCH=etag
        )
        assert first.status_code == 200
        assert first["ETag"] != etag
        stale = session_client.patch(
            page.test_url, {"description_html": "<p>stale writer</p>"}, format="json", HTTP_IF_MATCH=etag
        )
        assert stale.status_code == 412
        page.refresh_from_db()
        assert page.description_html == "<p>first writer</p>"

    def test_html_change_invalidates_empty_binary_snapshot(self, page, session_client):
        etag = session_client.get(page.test_url)["ETag"]
        Page.objects.filter(pk=page.pk).update(description_html="<p>replacement</p>")
        response = session_client.patch(
            page.test_url, {"description_html": "<p>old conversion</p>"}, format="json", HTTP_IF_MATCH=etag
        )
        assert response.status_code == 412

    def test_public_replacement_uses_current_locked_base_and_rejects_old_editor(
        self, page, session_client, create_user, mocker, monkeypatch
    ):
        etag = session_client.get(page.test_url)["ETag"]
        # Simulate a save after the public endpoint originally selected its page.
        Page.objects.filter(pk=page.pk).update(description_binary=b"newer editor state")
        monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "test-only-secret")
        conversion = mocker.patch("plane.api.views.page.requests.post")
        conversion.return_value.json.return_value = {
            "description_binary": base64.b64encode(b"replacement binary").decode(),
            "description_html": "<p>API replacement</p>",
            "description_json": {"type": "doc"},
        }
        request = APIRequestFactory().patch("/", {}, format="json")
        request.user = create_user
        request.data = {"description_html": "<p>API replacement</p>"}
        view = PageQuerysetMixin()
        view.kwargs = {"slug": page.workspace.slug, "project_id": page.projects.first().pk}
        response = view.update_page(request, page)
        assert response.status_code == 200
        assert conversion.call_args.kwargs["json"]["base_binary"] == base64.b64encode(b"newer editor state").decode()
        stale = session_client.patch(
            page.test_url, {"description_html": "<p>obsolete editor</p>"}, format="json", HTTP_IF_MATCH=etag
        )
        assert stale.status_code == 412
        page.refresh_from_db()
        assert bytes(page.description_binary) == b"replacement binary"
        assert page.description_html == "<p>API replacement</p>"

    def test_old_cached_client_cannot_bypass_revision_check(self, page, session_client):
        response = session_client.patch(page.test_url, {"description_html": "<p>legacy</p>"}, format="json")
        assert response.status_code == 428
        page.refresh_from_db()
        assert page.description_html != "<p>legacy</p>"

    def test_logical_revision_survives_http_compression(self, page, session_client):
        import gzip

        Page.objects.filter(pk=page.pk).update(description_binary=b"X" * 2048)
        response = session_client.get(page.test_url, HTTP_ACCEPT_ENCODING="gzip")
        body = b"".join(response.streaming_content)
        assert response["Content-Encoding"] == "gzip"
        assert gzip.decompress(body) == b"X" * 2048
        version = response["X-Plane-Document-Version"]
        assert version.startswith('"')
        assert response["ETag"] == "W/" + version
        saved = session_client.patch(
            page.test_url, {"description_html": "<p>saved</p>"}, format="json", HTTP_IF_MATCH=version
        )
        assert saved.status_code == 200
