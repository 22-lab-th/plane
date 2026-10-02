"""The MCP surface retains app permissions and accepts only explicit API tokens."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIClient

from plane.api.urls.extensions import ExtensionTokenAuthentication, TokenSurfaceMixin, urlpatterns
from plane.db.models import APIToken, Project, ProjectMember, Workspace, WorkspaceMember
from plane.license.models import Instance, InstanceAdmin

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(name="MCP", identifier="MCP", workspace=workspace)
    ProjectMember.objects.create(project=project, workspace=workspace, member=create_user, role=20)
    return project


def url(project, suffix="files/"):
    return f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.id}/{suffix}"


def client(token):
    result = APIClient()
    result.credentials(HTTP_X_API_KEY=token.token)
    return result


def test_every_extension_is_token_only_and_resolves_to_its_handler():
    for route in urlpatterns:
        concrete = str(route.pattern).replace("<str:slug>", "test-workspace")
        for parameter in ("project_id", "file_id", "folder_id", "link_id", "page_id", "run_id", "pk"):
            concrete = concrete.replace(f"<uuid:{parameter}>", str(uuid4()))
        concrete = concrete.replace("<int:version_no>", "1")
        for parameter in ("bookmark_id", "group_id"):
            concrete = concrete.replace(f"<uuid:{parameter}>", str(uuid4()))
        resolved = resolve("/api/v1/" + concrete)
        view = getattr(resolved.func, "view_class", None) or resolved.func.cls
        assert issubclass(view, TokenSurfaceMixin), concrete
        assert view.authentication_classes == [ExtensionTokenAuthentication]


@pytest.mark.parametrize("suffix", ["files/", "jira/runs/", "confluence/runs/", "pages-summary/"])
def test_token_can_read_project_features(project, api_token, suffix):
    response = client(api_token).get(url(project, suffix))
    assert response.status_code == 200, response.data


def test_bookmarks_and_groups_use_workspace_membership(workspace, api_token):
    token_client = client(api_token)
    response = token_client.post(f"/api/v1/workspaces/{workspace.slug}/bookmark-groups/", {"name": "Links"})
    assert response.status_code == 201, response.data
    response = token_client.post(
        f"/api/v1/workspaces/{workspace.slug}/bookmarks/",
        {"title": "Plane", "url": "https://plane.so", "group": response.data["id"]},
    )
    assert response.status_code == 201, response.data
    assert token_client.get(f"/api/v1/workspaces/{workspace.slug}/bookmarks/").status_code == 200


def test_session_login_is_not_a_fallback(project, create_user):
    session = APIClient()
    session.force_login(create_user)
    assert session.get(url(project)).status_code in (401, 403)


def test_workspace_bound_token_cannot_cross_workspaces(project, create_user):
    other = Workspace.objects.create(name="Other", slug="other", owner=create_user)
    WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
    token = APIToken.objects.create(user=create_user, workspace=other, token="bound-other")
    assert client(token).get(url(project)).status_code in (401, 403)
    token.workspace = project.workspace
    token.save()
    assert client(token).get(url(project)).status_code == 200


@pytest.mark.parametrize("revoked", ["token", "expired", "user", "workspace", "project"])
def test_revocation_is_enforced(project, api_token, create_user, revoked):
    if revoked == "token":
        api_token.is_active = False
        api_token.save()
    elif revoked == "expired":
        api_token.expired_at = timezone.now() - timedelta(days=1)
        api_token.save()
    elif revoked == "user":
        create_user.is_active = False
        create_user.save()
    elif revoked == "workspace":
        WorkspaceMember.objects.filter(workspace=project.workspace, member=create_user).update(is_active=False)
    else:
        ProjectMember.objects.filter(project=project, member=create_user).update(is_active=False)
    assert client(api_token).get(url(project)).status_code in (401, 403)


def test_guest_reads_but_cannot_create_file_folders(project, api_token, create_user):
    WorkspaceMember.objects.filter(workspace=project.workspace, member=create_user).update(role=5)
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    token_client = client(api_token)
    assert token_client.get(url(project)).status_code == 200
    response = token_client.post(url(project, "files/folders/"), {"name": "Forbidden"})
    assert response.status_code == 403, response.data


def test_archived_project_preserves_read_only_file_policy(project, api_token):
    project.archived_at = timezone.now()
    project.save()
    token_client = client(api_token)
    assert token_client.get(url(project)).status_code == 200
    response = token_client.post(url(project, "files/folders/"), {"name": "Forbidden"})
    assert response.status_code == 409, response.data
    assert response.data["code"] == "project_archived"


def test_token_upload_put_verification_and_file_revision(project, api_token, monkeypatch):
    """Verify a real signed MinIO PUT through the public token interface."""
    import hashlib
    import requests
    from plane.db.models import FileVersion
    from plane.settings.storage import S3Storage

    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")
    token_client = client(api_token)
    payload = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
    keys = []
    try:
        file_id = None
        for version_no in (1, 2):
            data = {
                "file_name": "MCP.pdf",
                "size_bytes": len(payload),
                "mime_type": "application/pdf",
                "checksum_sha256": hashlib.sha256(payload).hexdigest(),
            }
            if file_id:
                data["file_id"] = file_id
            response = token_client.post(url(project, "files/initiate-upload/"), data, format="json")
            assert response.status_code == 200, response.data
            file_id = response.data["file"]["id"]
            keys.append(FileVersion.objects.get(file_id=file_id, version_no=version_no).object_key)
            upload = response.data["upload"]
            assert requests.put(upload["url"], data=payload, headers=upload["headers"], timeout=15).status_code == 200
            response = token_client.post(
                url(project, f"files/{file_id}/complete-upload/"),
                {"version_no": version_no, "size_bytes": len(payload), "checksum_sha256": data["checksum_sha256"]},
                format="json",
            )
            assert response.status_code == 200, response.data
            assert response.data["activation_required"] is (version_no == 2)
        response = token_client.post(url(project, f"files/{file_id}/versions/2/activate/"), {}, format="json")
        assert response.status_code == 200, response.data
        assert FileVersion.objects.get(file_id=file_id, version_no=2).is_active
    finally:
        if keys:
            S3Storage().delete_files(keys)


def test_instance_requires_unscoped_admin_token(workspace, api_token, create_user):
    token_client = client(api_token)
    path = "/api/v1/instance/confluence/"
    assert token_client.get(path).status_code == 403
    instance = Instance.objects.create(
        instance_name="Test", instance_id="mcp-test", current_version="test", last_checked_at=timezone.now()
    )
    InstanceAdmin.objects.create(instance=instance, user=create_user, role=20)
    response = token_client.get(path)
    assert response.status_code == 200, response.data
    assert "api_token" not in response.data
    api_token.workspace = workspace
    api_token.save()
    assert token_client.get(path).status_code in (401, 403)


@pytest.mark.parametrize("provider", ["confluence", "jira"])
def test_instance_credentials_are_redacted_in_request_logs(provider, monkeypatch):
    from django.http import JsonResponse
    from django.test import RequestFactory
    from plane.middleware import logger

    recorded = []
    monkeypatch.setattr(logger.process_logs, "delay", lambda **kw: recorded.append(kw["log_data"]))
    req = RequestFactory().patch(
        f"/api/v1/instance/{provider}/",
        '{"api_token":"atlassian-secret"}',
        content_type="application/json",
        HTTP_X_API_KEY="plane-secret",
    )
    middleware = logger.APITokenLogMiddleware(lambda request: JsonResponse({"enabled": False}))
    middleware.process_request(req, JsonResponse({"enabled": False}), req.body)
    assert len(recorded) == 1
    assert "atlassian-secret" not in str(recorded)
    assert "plane-secret" not in str(recorded)
    assert "redacted" in recorded[0]["body"]
