# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Entity links: attach, unlink and the orphan contract (AC-16, AC-21).

Two invariants shape every test here:

* a link is a **row**, never a copy - unlinking one never removes bytes, and the
  file id is what every operation keys off, so a pointer movement (activation or
  repair) cannot disturb links (T-108 carry-forward 1);
* servability is never read from a status or from the pointer columns - a link
  operation on a file whose bytes are gone must leave the delivery answer at 409
  and must not invite a download (T-108 carry-forwards 2 and 3).
"""

# Python imports
import uuid
from unittest import mock

# Django imports
from django.utils import timezone

# Third party imports
import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from django.conf import settings
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileLink,
    FileObject,
    FileVersion,
    Issue,
    IssueComment,
    Page,
    Project,
    ProjectMember,
    ProjectPage,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def links_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}links/"


def link_url(slug, project_id, file_id, link_id):
    return f"{links_url(slug, project_id, file_id)}{link_id}/"


def download_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}download/"


def restore_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}restore/"


def activate_url(slug, project_id, file_id, version_no):
    return f"{detail_url(slug, project_id, file_id)}versions/{version_no}/activate/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}complete-upload/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def independent_store():
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION or "us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def object_exists(store, key):
    try:
        store.head_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


@pytest.fixture
def stored_objects():
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Link Workspace", slug="link-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Link Project", identifier="LINK", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def add_member(project, *, email, role, active=True):
    local = email.split("@")[0]
    user = User.objects.create(email=email, username=local, first_name=local)
    user.set_password("test-password")
    user.save()
    ProjectMember.objects.create(
        project=project, member=user, workspace=project.workspace, role=role, is_active=active
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_state(project):
    return State.objects.create(
        name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
    )


def make_issue(project, *, name="Linked issue"):
    return Issue.objects.create(
        name=name, project=project, workspace=project.workspace, state=make_state(project)
    )


def issue_key(project, issue):
    return f"{project.identifier}-{issue.sequence_id}"


def upload_file(session_client, project, *, name="Report.pdf", link=None, stored_objects):
    """Create one verified version through the real pipeline; return ``(file_id, key)``."""
    payload = {"file_name": name, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"}
    if link is not None:
        payload["link"] = {"entity_type": link[0], "entity_id": str(link[1])}

    initiated = session_client.post(upload_url(project.workspace.slug, project.id), payload, format="json")
    assert initiated.status_code == status.HTTP_200_OK, initiated.data
    file_id = initiated.data["file"]["id"]

    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data

    key = FileVersion.objects.get(file_id=file_id, version_no=1).object_key
    stored_objects.append(key)

    return file_id, key


def attach(session_client, project, file_id, entity_type, entity_id):
    return session_client.post(
        links_url(project.workspace.slug, project.id, file_id),
        {"entity_type": entity_type, "entity_id": str(entity_id)},
        format="json",
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestAttachLink:
    """AC-16 / R-LINK-1: one file, many links, each validated against this project."""

    def test_attaching_an_issue_records_the_issue_key_and_touches_no_key(
        self, session_client, project, stored_objects
    ):
        issue = make_issue(project)
        file_id, object_key = upload_file(session_client, project, stored_objects=stored_objects)

        response = attach(session_client, project, file_id, "issue", issue.id)

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["link"]["entity_type"] == "issue"
        assert response.data["link"]["entity_id"] == str(issue.id)

        link = FileLink.objects.get(file_id=file_id)
        assert link.entity_identifier == issue_key(project, issue)
        audit = FileAccessLog.objects.get(file_id=file_id, action=FileAccessLog.Action.LINKED)
        assert audit.metadata["entity_identifier"] == issue_key(project, issue)

        # A link added later never rewrites the key or the category (DEC-001).
        stored = FileObject.objects.get(pk=file_id)
        assert stored.object_key == object_key
        assert stored.category == FileObject.Category.ASSETS

    def test_a_page_and_a_comment_can_be_linked_too(self, session_client, create_user, project, stored_objects):
        page = Page.objects.create(name="Doc", workspace=project.workspace, owned_by=create_user)
        ProjectPage.objects.create(page=page, project=project, workspace=project.workspace)
        issue = make_issue(project)
        comment = IssueComment.objects.create(
            comment_stripped="Looks good",
            comment_html="<p>Looks good</p>",
            issue=issue,
            project=project,
            workspace=project.workspace,
            actor=create_user,
        )
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        page_response = attach(session_client, project, file_id, "page", page.id)
        comment_response = attach(session_client, project, file_id, "comment", comment.id)

        assert page_response.status_code == status.HTTP_200_OK, page_response.data
        assert comment_response.status_code == status.HTTP_200_OK, comment_response.data
        assert FileLink.objects.filter(file_id=file_id).count() == 2

    def test_a_duplicate_link_is_refused_with_one_live_row(
        self, session_client, project, stored_objects
    ):
        issue = make_issue(project)
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
        first = attach(session_client, project, file_id, "issue", issue.id)
        assert first.status_code == status.HTTP_200_OK, first.data

        duplicate = attach(session_client, project, file_id, "issue", issue.id)

        assert duplicate.status_code == status.HTTP_409_CONFLICT
        assert duplicate.data["code"] == "link_exists"
        assert FileLink.objects.filter(file_id=file_id, entity_type="issue", entity_id=issue.id).count() == 1
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.LINKED).count() == 1

    def test_an_entity_from_another_project_is_refused_without_a_row(
        self, session_client, project, stored_objects
    ):
        other_project = Project.objects.create(
            name="Elsewhere", identifier="ELSE", workspace=project.workspace
        )
        foreign_issue = make_issue(other_project, name="Foreign")
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        response = attach(session_client, project, file_id, "issue", foreign_issue.id)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "invalid_request"
        assert response.data["field"] == "link.entity_id"
        assert FileLink.objects.filter(file_id=file_id).count() == 0

    def test_an_unknown_entity_is_refused_without_a_row(self, session_client, project, stored_objects):
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        response = attach(session_client, project, file_id, "issue", uuid.uuid4())

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "invalid_request"
        assert FileLink.objects.filter(file_id=file_id).count() == 0

    def test_an_unvalidatable_entity_type_is_refused_on_both_doors(
        self, session_client, project, stored_objects
    ):
        """This fork has no milestone table, so the type is refused rather than stored."""
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        response = attach(session_client, project, file_id, "milestone", uuid.uuid4())
        upload = session_client.post(
            upload_url(project.workspace.slug, project.id),
            {
                "file_name": "Milestone.pdf",
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
                "link": {"entity_type": "milestone", "entity_id": str(uuid.uuid4())},
            },
            format="json",
        )

        for answer in (response, upload):
            assert answer.status_code == status.HTTP_400_BAD_REQUEST
            assert answer.data["code"] == "unsupported_entity_type"
            assert answer.data["field"] == "link.entity_type"
        assert FileLink.objects.filter(file_id=file_id).count() == 0
        assert FileObject.objects.filter(project=project).count() == 1

    def test_an_unknown_field_is_refused(self, session_client, project, stored_objects):
        issue = make_issue(project)
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        response = session_client.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(issue.id), "role": "attachment"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "unsupported_field"
        assert response.data["field"] == "role"
        assert FileLink.objects.filter(file_id=file_id).count() == 0

    def test_the_link_door_checks_role_state_and_visibility(self, session_client, project, stored_objects):
        guest = add_member(project, email="links-guest@example.com", role=5)
        outsider = add_member(project, email="links-outsider@example.com", role=20, active=False)
        issue = make_issue(project)
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        assert attach(guest, project, file_id, "issue", issue.id).status_code == status.HTTP_403_FORBIDDEN
        assert attach(outsider, project, file_id, "issue", issue.id).status_code == status.HTTP_404_NOT_FOUND
        assert (
            attach(session_client, project, uuid.uuid4(), "issue", issue.id).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        trashed = attach(session_client, project, file_id, "issue", issue.id)
        assert trashed.status_code == status.HTTP_409_CONFLICT
        assert trashed.data["code"] == "file_trashed"

        Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())
        assert session_client.post(restore_url(project.workspace.slug, project.id, file_id)).status_code == 409
        assert FileLink.objects.filter(file_id=file_id).count() == 0


@pytest.mark.contract
@pytest.mark.django_db
class TestUnlink:
    """AC-21: unlinking is a row operation and touches nothing else."""

    def test_unlinking_marks_the_row_inactive_and_leaves_everything_else(
        self, session_client, project, stored_objects, independent_store
    ):
        issue = make_issue(project)
        file_id, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        attached = attach(session_client, project, file_id, "issue", issue.id)
        link_id = attached.data["link"]["id"]
        before = FileObject.all_objects.get(pk=file_id)

        response = session_client.delete(link_url(project.workspace.slug, project.id, file_id, link_id))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert FileLink.objects.filter(id=link_id).count() == 0
        marked = FileLink.all_objects.get(id=link_id)
        assert marked.deleted_at is not None
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.UNLINKED).count() == 1

        after = FileObject.all_objects.get(pk=file_id)
        assert after.name_display == before.name_display
        assert after.status == before.status
        assert after.deleted_at == before.deleted_at
        assert after.object_key == before.object_key == object_key
        assert after.current_version_no == before.current_version_no
        assert object_exists(independent_store, object_key) is True

        # A repeated delete is a 404, not a second silent success.
        again = session_client.delete(link_url(project.workspace.slug, project.id, file_id, link_id))
        assert again.status_code == status.HTTP_404_NOT_FOUND

    def test_an_unlinked_entity_can_be_linked_again(self, session_client, project, stored_objects):
        issue = make_issue(project)
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
        first = attach(session_client, project, file_id, "issue", issue.id)
        session_client.delete(link_url(project.workspace.slug, project.id, file_id, first.data["link"]["id"]))

        again = attach(session_client, project, file_id, "issue", issue.id)

        assert again.status_code == status.HTTP_200_OK, again.data
        assert again.data["link"]["id"] != first.data["link"]["id"]
        assert FileLink.objects.filter(file_id=file_id).count() == 1
        assert FileLink.all_objects.filter(file_id=file_id).count() == 2

    def test_the_unlink_door_checks_role_and_visibility(self, session_client, project, stored_objects):
        guest = add_member(project, email="unlink-guest@example.com", role=5)
        issue = make_issue(project)
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
        attached = attach(session_client, project, file_id, "issue", issue.id)
        link_id = attached.data["link"]["id"]
        url = link_url(project.workspace.slug, project.id, file_id, link_id)

        assert guest.delete(url).status_code == status.HTTP_403_FORBIDDEN
        assert session_client.delete(url).status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.contract
@pytest.mark.django_db
class TestOrphanBehaviour:
    """AC-21: the last unlink leaves an intact, listed, downloadable orphan."""

    def test_the_last_unlink_leaves_the_file_listed_downloadable_and_counted_zero(
        self, session_client, create_user, project, stored_objects, independent_store
    ):
        issue = make_issue(project)
        page = Page.objects.create(name="Doc", workspace=project.workspace, owned_by=create_user)
        ProjectPage.objects.create(page=page, project=project, workspace=project.workspace)
        file_id, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        first = attach(session_client, project, file_id, "issue", issue.id)
        second = attach(session_client, project, file_id, "page", page.id)

        both = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert both.data["link_count"] == 2

        for link_id in (first.data["link"]["id"], second.data["link"]["id"]):
            assert (
                session_client.delete(link_url(project.workspace.slug, project.id, file_id, link_id)).status_code
                == status.HTTP_204_NO_CONTENT
            )

        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["link_count"] == 0
        assert detail.data["links"] == []
        assert detail.data["permissions"]["can_download"] is True

        listing = session_client.get(files_url(project.workspace.slug, project.id))
        rows = {row["id"]: row for row in listing.data["results"]}
        assert file_id in rows
        assert rows[file_id]["link_count"] == 0

        # Still downloadable, and the object is still there - an orphan, not a deletion.
        signed = session_client.get(download_url(project.workspace.slug, project.id, file_id))
        assert signed.status_code == status.HTTP_200_OK
        assert object_exists(independent_store, object_key) is True
        assert FileObject.objects.filter(pk=file_id).exists() is True
        assert FileVersion.objects.filter(file_id=file_id).count() == 1

    def test_link_count_is_filter_independent(self, session_client, create_user, project, stored_objects):
        """T-103 F-1: the count must not reuse the filter's join."""
        issue = make_issue(project)
        page = Page.objects.create(name="Doc", workspace=project.workspace, owned_by=create_user)
        ProjectPage.objects.create(page=page, project=project, workspace=project.workspace)
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
        attach(session_client, project, file_id, "issue", issue.id)
        attach(session_client, project, file_id, "page", page.id)

        unfiltered = session_client.get(files_url(project.workspace.slug, project.id))
        searched = session_client.get(files_url(project.workspace.slug, project.id), {"q": "Report"})
        by_entity = session_client.get(
            files_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
        )
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))

        counts = [
            response.data["results"][0]["link_count"] if response.data["results"] else None
            for response in (unfiltered, searched, by_entity)
        ]
        assert counts == [2, 2, 2]
        assert detail.data["link_count"] == 2
        # Only the entity-filtered listing is entitled to narrow the rows.
        assert [row["id"] for row in by_entity.data["results"]] == [file_id]


@pytest.mark.contract
@pytest.mark.django_db
class TestLinksAcrossPointerMovement:
    """T-108 carry-forward 1: links key off the file id, not the file's key."""

    def _link_and_move_the_pointer(self, session_client, project, stored_objects):
        """Link a file, activate a revision, and partially purge then repair it."""
        issue = make_issue(project)
        file_id, first_key = upload_file(session_client, project, stored_objects=stored_objects, link=("issue", issue.id))
        link_id = FileLink.objects.get(file_id=file_id).id

        revision = session_client.post(
            # A revision of the same file: the key changes, the links must not care.
            f"{detail_url(project.workspace.slug, project.id, file_id)}versions/",
            {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )
        assert revision.status_code == status.HTTP_200_OK, revision.data
        upload = revision.data["upload"]
        assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200
        completed = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 2, "size_bytes": len(PDF_BYTES)},
            format="json",
        )
        assert completed.status_code == status.HTTP_200_OK, completed.data
        second_key = FileVersion.objects.get(file_id=file_id, version_no=2).object_key
        stored_objects.append(second_key)

        activated = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))
        assert activated.status_code == status.HTTP_200_OK, activated.data
        assert FileObject.objects.get(pk=file_id).object_key == second_key

        return file_id, link_id, issue, first_key, second_key

    def test_a_link_survives_an_activation(self, session_client, project, stored_objects):
        file_id, link_id, issue, first_key, second_key = self._link_and_move_the_pointer(
            session_client, project, stored_objects
        )

        assert first_key != second_key
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.data["link_count"] == 1
        assert detail.data["file"]["object_key"] == second_key
        assert [link["id"] for link in detail.data["links"]] == [str(link_id)]
        assert detail.data["links"][0]["entity_identifier"] == issue_key(project, issue)
        listing = session_client.get(files_url(project.workspace.slug, project.id))
        assert listing.data["results"][0]["link_count"] == 1

    def test_a_link_survives_a_partial_purge_and_a_repair(self, session_client, project, stored_objects):
        file_id, link_id, issue, first_key, second_key = self._link_and_move_the_pointer(
            session_client, project, stored_objects
        )
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204

        real_delete = S3Storage.delete_files
        failure = EndpointConnectionError(endpoint_url="http://test-minio:9000")

        def flaky_delete(self, object_names):
            if first_key in object_names:
                raise failure
            return real_delete(self, object_names)

        with mock.patch.object(S3Storage, "delete_files", flaky_delete):
            failed = session_client.delete(
                f"{detail_url(project.workspace.slug, project.id, file_id)}purge/?confirm=true"
            )
        assert failed.status_code == status.HTTP_502_BAD_GATEWAY

        restored = session_client.post(restore_url(project.workspace.slug, project.id, file_id))
        assert restored.status_code == status.HTTP_200_OK, restored.data
        repaired = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 1))
        assert repaired.status_code == status.HTTP_200_OK, repaired.data
        assert repaired.data["repaired"] is True

        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.data["link_count"] == 1
        assert [link["id"] for link in detail.data["links"]] == [str(link_id)]
        assert detail.data["permissions"]["can_download"] is True
        assert detail.data["file"]["object_key"] == first_key
        assert detail.data["file"]["current_version_no"] == 1
        assert session_client.get(download_url(project.workspace.slug, project.id, file_id)).status_code == 200

    def test_link_operations_never_invite_a_download_on_a_file_with_no_bytes(
        self, session_client, project, stored_objects, independent_store
    ):
        """T-108 carry-forwards 2 and 3: servability comes from `can_download`."""
        issue = make_issue(project)
        file_id, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        first = attach(session_client, project, file_id, "issue", issue.id)
        link_id = first.data["link"]["id"]

        # The only version's bytes go away outside the application; activating the
        # version refuses and strands the file with no active version.
        assert S3Storage().delete_files([object_key]) is True
        stored_objects.remove(object_key)
        assert (
            session_client.post(activate_url(project.workspace.slug, project.id, file_id, 1)).status_code
            == status.HTTP_409_CONFLICT
        )

        unlinked = session_client.delete(link_url(project.workspace.slug, project.id, file_id, link_id))
        relinked = attach(session_client, project, file_id, "issue", issue.id)

        assert unlinked.status_code == status.HTTP_204_NO_CONTENT
        assert relinked.status_code == status.HTTP_200_OK, relinked.data

        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.data["link_count"] == 1
        assert detail.data["permissions"]["can_download"] is False
        assert detail.data["file"]["current_version_no"] == 0

        refused = session_client.get(download_url(project.workspace.slug, project.id, file_id))
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert refused.data["code"] == "object_unavailable"
        assert "url" not in refused.data
        assert object_exists(independent_store, object_key) is False
