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
    FileAsset,
    FileLink,
    FileObject,
    FileVersion,
    Issue,
    IssueComment,
    Module,
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

#: A real 1x1 PNG, so the finalize magic-byte check sees the type it was declared as.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def entity_links_url(slug, project_id):
    """The entity -> files direction the issue and page surfaces read."""
    return f"{files_url(slug, project_id)}links/"


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


def upload_file(
    session_client,
    project,
    *,
    name="Report.pdf",
    link=None,
    stored_objects,
    payload=PDF_BYTES,
    mime_type="application/pdf",
):
    """Create one verified version through the real pipeline; return ``(file_id, key)``."""
    body = {"file_name": name, "size_bytes": len(payload), "mime_type": mime_type}
    if link is not None:
        body["link"] = {"entity_type": link[0], "entity_id": str(link[1])}

    initiated = session_client.post(upload_url(project.workspace.slug, project.id), body, format="json")
    assert initiated.status_code == status.HTTP_200_OK, initiated.data
    file_id = initiated.data["file"]["id"]

    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=payload, headers=upload["headers"], timeout=30).status_code == 200

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(payload)},
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

    def test_a_milestone_link_is_validated_against_this_forks_module_table(
        self, session_client, project, stored_objects
    ):
        """R-LINK-1's milestone is this fork's ``Module``; both spellings store one value."""
        module = Module.objects.create(
            name="Launch", project=project, workspace=project.workspace, created_by_id=project.created_by_id
        )
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        by_milestone = attach(session_client, project, file_id, "milestone", module.id)
        second_file_id, _ = upload_file(
            session_client, project, name="Second.pdf", stored_objects=stored_objects
        )
        by_alias = attach(session_client, project, second_file_id, "module", module.id)

        assert by_milestone.status_code == status.HTTP_200_OK, by_milestone.data
        assert by_alias.status_code == status.HTTP_200_OK, by_alias.data
        for file_id_checked in (file_id, second_file_id):
            link = FileLink.objects.get(file_id=file_id_checked)
            assert link.entity_type == FileLink.EntityType.MILESTONE, "one stored spelling"
            assert link.entity_identifier == "Launch"
            assert link.entity_id == module.id

        # Scoped like every other target: another project's module is refused.
        other_project = Project.objects.create(
            name="Elsewhere", identifier="ELSE", workspace=project.workspace
        )
        foreign = Module.objects.create(
            name="Foreign", project=other_project, workspace=project.workspace
        )
        refused = attach(session_client, project, file_id, "milestone", foreign.id)
        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "invalid_request"
        assert FileLink.objects.filter(file_id=file_id).count() == 1

    def test_both_spellings_work_on_both_doors(self, session_client, project, stored_objects):
        """F-5: the alias is one vocabulary, not one per endpoint."""
        module = Module.objects.create(
            name="Aliased", project=project, workspace=project.workspace, created_by_id=project.created_by_id
        )

        for spelling in ("milestone", "module"):
            file_name = f"Upload-{spelling}.pdf"
            file_id, _ = upload_file(
                session_client, project, name=file_name, link=(spelling, module.id), stored_objects=stored_objects
            )
            assert FileLink.objects.get(file_id=file_id).entity_type == FileLink.EntityType.MILESTONE

            # The revision door shares the upload serializer, so it answers the same
            # spelling and does not duplicate the link the file already carries.
            revision = session_client.post(
                f"{detail_url(project.workspace.slug, project.id, file_id)}versions/",
                {
                    "file_name": file_name,
                    "size_bytes": len(PDF_BYTES),
                    "mime_type": "application/pdf",
                    "link": {"entity_type": spelling, "entity_id": str(module.id)},
                },
                format="json",
            )
            assert revision.status_code == status.HTTP_200_OK, (spelling, revision.data)
            assert revision.data["version_no"] == 2
            assert FileLink.objects.filter(file_id=file_id).count() == 1

    def test_a_deliverable_link_is_refused_on_both_doors(
        self, session_client, project, stored_objects
    ):
        """Not an R-LINK-1 target, and this fork has no row to validate it against."""
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)

        response = attach(session_client, project, file_id, "deliverable", uuid.uuid4())
        upload = session_client.post(
            upload_url(project.workspace.slug, project.id),
            {
                "file_name": "Deliverable.pdf",
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
                "link": {"entity_type": "deliverable", "entity_id": str(uuid.uuid4())},
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

        # Neither entity finds it any more, and the count agrees with the filter.
        for entity_type, entity_id in (("issue", issue.id), ("page", page.id)):
            filtered = session_client.get(
                files_url(project.workspace.slug, project.id),
                {"entity_type": entity_type, "entity_id": str(entity_id)},
            )
            assert filtered.data["results"] == []

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

        # F-1: a detached link must vanish from the filtered listing too, because the
        # filter and the count have to tell one story: present with count >= 1, absent
        # with count 0.
        linked = FileLink.objects.get(file_id=file_id, entity_type="issue")
        session_client.delete(link_url(project.workspace.slug, project.id, file_id, linked.id))

        after = session_client.get(
            files_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
        )
        still_by_page = session_client.get(
            files_url(project.workspace.slug, project.id),
            {"entity_type": "page", "entity_id": str(page.id)},
        )
        after_detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))

        assert after.data["results"] == [], "the unlinked entity must not find the file"
        assert after.data["page"]["total_results"] == 0
        assert [row["link_count"] for row in still_by_page.data["results"]] == [1]
        assert after_detail.data["link_count"] == 1


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


@pytest.mark.contract
@pytest.mark.django_db
class TestMilestoneKeySegment:
    """F-6 / DEC-001: the segment is frozen at creation, for every link type."""

    def test_the_segment_is_the_module_id_and_survives_a_revision_and_an_activation(
        self, session_client, project, stored_objects
    ):
        module = Module.objects.create(
            name="Renamable module",
            project=project,
            workspace=project.workspace,
            created_by_id=project.created_by_id,
        )
        file_id, first_key = upload_file(
            session_client, project, link=("milestone", module.id), stored_objects=stored_objects
        )
        assert f"/{module.id}/" in first_key
        assert "Renamable" not in first_key and "renamable" not in first_key

        revision = session_client.post(
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

        # Same segment in both keys: the revision inherits it, it does not recompute it.
        assert f"/{module.id}/" in second_key

        def up_to_segment(key):
            """Everything up to and including the entityRef segment."""
            return key.split(f"/{module.id}/")[0] + f"/{module.id}/"

        assert up_to_segment(first_key) == up_to_segment(second_key)

        activated = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))
        assert activated.status_code == status.HTTP_200_OK, activated.data
        assert FileObject.objects.get(pk=file_id).object_key == second_key

        # The row keeps the human name for search; the key keeps the id (a rename
        # must not move a key).
        link = FileLink.objects.get(file_id=file_id)
        assert link.entity_identifier == "Renamable module"
        Module.objects.filter(pk=module.pk).update(name="Renamed later")
        third = session_client.post(
            f"{detail_url(project.workspace.slug, project.id, file_id)}versions/",
            {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )
        assert third.status_code == status.HTTP_200_OK, third.data
        planned = FileVersion.objects.get(file_id=file_id, version_no=3).object_key
        assert f"/{module.id}/" in planned
        assert "Renamed" not in planned and "renamed" not in planned
@pytest.mark.contract
@pytest.mark.django_db
class TestEntityLinkListing:
    """T-115 / AC-16, AC-17: an entity's own files, as the issue and page surfaces read them.

    The upload door already accepts a ``link``, so an attachment is created by the
    same pipeline a Files-tab upload uses: one ``file_objects`` row, one stored
    object, one link. What these tests pin is that the entity -> file direction
    answers with **that** row - the same id the Files view lists - and that
    unlinking it takes the file off the entity without touching the file.
    """

    def test_an_upload_that_declares_a_link_is_one_row_in_both_surfaces(
        self, session_client, project, stored_objects
    ):
        issue = make_issue(project)

        file_id, object_key = upload_file(
            session_client, project, link=("issue", issue.id), stored_objects=stored_objects
        )

        listed = session_client.get(
            entity_links_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
        )
        assert listed.status_code == status.HTTP_200_OK, listed.data
        assert len(listed.data["results"]) == 1
        row = listed.data["results"][0]
        assert row["file"]["id"] == file_id
        assert row["file"]["name_display"] == "Report.pdf"
        assert row["file"]["size_bytes"] == len(PDF_BYTES)
        assert row["file"]["link_count"] == 1
        assert row["link"]["entity_type"] == "issue"
        assert row["link"]["entity_id"] == str(issue.id)
        assert row["link"]["entity_identifier"] == issue_key(project, issue)

        # The Files view lists the same row, by the same id, once.
        files = session_client.get(files_url(project.workspace.slug, project.id), {"folder_id": "root"})
        assert files.status_code == status.HTTP_200_OK, files.data
        rows = [result for result in files.data["results"] if result["id"] == file_id]
        assert len(rows) == 1
        assert rows[0]["link_count"] == 1

        # One stored object, not one per surface (R-LINK-1): an entity link never
        # duplicates bytes.
        assert FileObject.objects.filter(project_id=project.id).count() == 1
        assert FileVersion.objects.filter(file_id=file_id).count() == 1
        assert FileVersion.objects.get(file_id=file_id).object_key == object_key
        assert FileLink.objects.filter(file_id=file_id).count() == 1

    def test_a_page_embed_is_a_project_file_the_editor_can_render_inline(
        self, session_client, create_user, project, stored_objects
    ):
        page = Page.objects.create(name="Design notes", workspace=project.workspace, owned_by=create_user)
        ProjectPage.objects.create(page=page, project=project, workspace=project.workspace)

        file_id, object_key = upload_file(
            session_client,
            project,
            name="diagram.png",
            link=("page", page.id),
            stored_objects=stored_objects,
            payload=PNG_BYTES,
            mime_type="image/png",
        )

        stored = FileObject.objects.get(pk=file_id)
        assert stored.category == FileObject.Category.PAGES
        assert f"/{page.id}/" in object_key
        assert f"/{page.id}/" in stored.object_key

        listed = session_client.get(
            entity_links_url(project.workspace.slug, project.id),
            {"entity_type": "page", "entity_id": str(page.id)},
        )
        assert listed.status_code == status.HTTP_200_OK, listed.data
        assert [row["file"]["id"] for row in listed.data["results"]] == [file_id]

        # What the editor resolves: a presigned URL for the project-scoped preview.
        preview = session_client.get(f"{detail_url(project.workspace.slug, project.id, file_id)}preview/")
        assert preview.status_code == status.HTTP_200_OK, preview.data
        assert preview.data["disposition"] == "inline"
        assert preview.data["file_name"] == "diagram.png"

        served = requests.get(preview.data["url"], timeout=30)
        assert served.status_code == 200
        assert served.content == PNG_BYTES
        assert served.headers["Content-Type"].startswith("image/png")

    def test_unlinking_an_issue_attachment_leaves_the_file_listed_and_downloadable(
        self, session_client, project, stored_objects, independent_store
    ):
        issue = make_issue(project)
        file_id, object_key = upload_file(
            session_client, project, link=("issue", issue.id), stored_objects=stored_objects
        )
        link_id = session_client.get(
            entity_links_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
        ).data["results"][0]["link"]["id"]

        removed = session_client.delete(f"{links_url(project.workspace.slug, project.id, file_id)}{link_id}/")
        assert removed.status_code == status.HTTP_204_NO_CONTENT

        # The entity no longer renders it...
        after = session_client.get(
            entity_links_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
        )
        assert after.data["results"] == []

        # ...while the file itself, its object and its download are untouched (AC-21).
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK, detail.data
        assert detail.data["link_count"] == 0
        assert detail.data["file"]["id"] == file_id
        assert detail.data["permissions"]["can_download"] is True

        still_listed = session_client.get(files_url(project.workspace.slug, project.id), {"folder_id": "root"})
        assert [row["id"] for row in still_listed.data["results"]] == [file_id]

        download = session_client.get(download_url(project.workspace.slug, project.id, file_id))
        assert download.status_code == status.HTTP_200_OK, download.data
        assert download.data["disposition"] == "attachment"
        assert requests.get(download.data["url"], timeout=30).content == PDF_BYTES
        assert object_exists(independent_store, object_key) is True
        unlinked_row = FileLink.all_objects.get(file_id=file_id)
        assert unlinked_row.deleted_at is not None

    def test_a_trashed_file_stops_being_an_attachment_and_returns_on_restore(
        self, session_client, project, stored_objects
    ):
        issue = make_issue(project)
        file_id, _ = upload_file(session_client, project, link=("issue", issue.id), stored_objects=stored_objects)
        query = {"entity_type": "issue", "entity_id": str(issue.id)}

        trashed = session_client.delete(detail_url(project.workspace.slug, project.id, file_id))
        assert trashed.status_code == status.HTTP_204_NO_CONTENT
        assert session_client.get(entity_links_url(project.workspace.slug, project.id), query).data["results"] == []
        # Trashing marks the link inactive but keeps the row, so restore can revive it.
        assert FileLink.all_objects.get(file_id=file_id).deleted_at is not None

        restored = session_client.post(restore_url(project.workspace.slug, project.id, file_id))
        assert restored.status_code == status.HTTP_200_OK, restored.data
        assert [row["file"]["id"] for row in session_client.get(
            entity_links_url(project.workspace.slug, project.id), query
        ).data["results"]] == [file_id]

    def test_the_entity_listing_refuses_an_unusable_query(self, session_client, project):
        issue = make_issue(project)
        base = entity_links_url(project.workspace.slug, project.id)

        unknown_type = session_client.get(base, {"entity_type": "epic", "entity_id": str(issue.id)})
        assert unknown_type.status_code == status.HTTP_400_BAD_REQUEST
        assert unknown_type.data["code"] == "invalid_request"
        assert unknown_type.data["field"] == "entity_type"

        missing_id = session_client.get(base, {"entity_type": "issue"})
        assert missing_id.status_code == status.HTTP_400_BAD_REQUEST
        assert missing_id.data["field"] == "entity_id"

        malformed_id = session_client.get(base, {"entity_type": "issue", "entity_id": "not-a-uuid"})
        assert malformed_id.status_code == status.HTTP_400_BAD_REQUEST
        assert malformed_id.data["field"] == "entity_id"

    def test_an_entity_of_another_project_cannot_be_read_through_this_project(self, project, create_user):
        other_project = Project.objects.create(name="Elsewhere", identifier="ELSE", workspace=project.workspace)
        foreign_issue = make_issue(other_project, name="Foreign")
        outsider = User.objects.create(email="outsider@example.com", username="outsider")
        outsider.set_password("test-password")
        outsider.save()
        client = APIClient()
        client.force_authenticate(user=outsider)

        response = client.get(
            entity_links_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(foreign_issue.id)},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.contract
@pytest.mark.django_db
class TestLegacyIssueAttachment:
    """T-115: the attachments an issue already had keep listing and downloading."""

    def test_a_legacy_file_asset_attachment_still_lists_and_serves_its_bytes(
        self, session_client, create_user, project, independent_store
    ):
        issue = make_issue(project)
        key = f"{project.workspace_id}/{uuid.uuid4().hex}-legacy.pdf"
        independent_store.put_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key, Body=PDF_BYTES, ContentType="application/pdf"
        )
        try:
            asset = FileAsset.objects.create(
                attributes={"name": "legacy.pdf", "type": "application/pdf", "size": len(PDF_BYTES)},
                asset=key,
                size=len(PDF_BYTES),
                workspace_id=project.workspace_id,
                project_id=project.id,
                issue_id=issue.id,
                entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
                created_by=create_user,
                is_uploaded=True,
            )

            listed = session_client.get(
                f"/api/assets/v2/workspaces/{project.workspace.slug}/projects/{project.id}"
                f"/issues/{issue.id}/attachments/"
            )
            assert listed.status_code == status.HTTP_200_OK, listed.data
            rows = [row for row in listed.data if str(row["id"]) == str(asset.id)]
            assert len(rows) == 1
            assert rows[0]["attributes"]["name"] == "legacy.pdf"

            # The download the attachment row points at: an app path that redirects
            # to the signed object URL the legacy path has always used.
            redirect = session_client.get(rows[0]["asset_url"])
            assert redirect.status_code == status.HTTP_302_FOUND
            served = requests.get(redirect["Location"], timeout=30)
            assert served.status_code == 200
            assert served.content == PDF_BYTES
        finally:
            independent_store.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
