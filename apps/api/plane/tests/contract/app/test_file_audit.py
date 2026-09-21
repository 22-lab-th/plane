# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Audit recording, reading, immutability and masking (AC-18, AC-34, AC-38, R-AUD-1/3).

Two properties are asserted rather than assumed:

* **one row per mutation, from one helper** - the flow below runs every mutation the
  feature has and then checks, for *every* row, that it carries an actor id and a
  display snapshot, a parseable address, the caller's user agent, a timestamp, and
  metadata that never holds a presigned URL or a signature;
* **the read APIs agree with the rows** - the per-file activity in the detail payload
  is compared with the database, not trusted on its own.
"""

# Python imports
import ipaddress
import json
import uuid
from datetime import timedelta

# Django imports
from django.utils import timezone

# Third party imports
import pytest
import requests
from rest_framework import status

# Module imports
from plane.bgtasks.file_audit_task import mask_audit_pii
from plane.bgtasks import file_audit_task
from plane.db.models import (
    FileAccessLog,
    FileFolder,
    FileObject,
    FileVersion,
    Issue,
    Project,
    ProjectMember,
    State,
    StorageQuota,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
USER_AGENT = "audit-suite/1.0"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def activity_url(slug, project_id):
    return f"{files_url(slug, project_id)}activity/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}complete-upload/"


def abort_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}abort-upload/"


def copy_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}copy/"


def links_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}links/"


def download_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}download/"


def purge_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}purge/?confirm=true"


def folders_url(slug, project_id):
    return f"{files_url(slug, project_id)}folders/"


def folder_url(slug, project_id, folder_id):
    return f"{folders_url(slug, project_id)}{folder_id}/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture(autouse=True)
def caller_agent(session_client):
    """Every request from this suite carries a known user agent (AC-18 asserts it)."""
    session_client.credentials(HTTP_USER_AGENT=USER_AGENT)
    return session_client


@pytest.fixture
def stored_objects():
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Audit Workspace", slug="audit-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Audit Project", identifier="AUDT", workspace=workspace)
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
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=user)
    client.credentials(HTTP_USER_AGENT=USER_AGENT)
    return client


def make_issue(project):
    return Issue.objects.create(
        name="Audited issue",
        project=project,
        workspace=project.workspace,
        state=State.objects.create(
            name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
        ),
    )


def upload_file(session_client, project, *, name="Report.pdf", stored_objects):
    initiated = session_client.post(
        upload_url(project.workspace.slug, project.id),
        {"file_name": name, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
        format="json",
    )
    assert initiated.status_code == status.HTTP_200_OK, initiated.data
    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200
    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, initiated.data["file"]["id"]),
        {"version_no": 1, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data
    stored_objects.append(FileVersion.objects.get(file_id=initiated.data["file"]["id"]).object_key)
    return initiated.data["file"]["id"]


def assert_every_row_is_complete(project):
    """AC-18's column set, asserted on every row this suite produced."""
    rows = list(FileAccessLog.objects.filter(project_id=project.id))
    assert rows, "the flow must have written audit rows"

    for row in rows:
        assert row.action in FileAccessLog.Action.values
        assert row.actor_id is not None, row.action
        assert row.actor_display, row.action
        assert row.created_at is not None
        if row.ip_address is not None:
            ipaddress.ip_address(row.ip_address)
        assert row.user_agent == USER_AGENT, row.action

        serialised = json.dumps(row.metadata or {})
        for forbidden in ("Signature=", "X-Amz-Signature", "test-minio", "http://", "https://"):
            assert forbidden not in serialised, f"{row.action}: {serialised}"
        # No usage numbers in the trail: the counters and the storage API own those,
        # so nothing here can disagree with the version rows (T-110 carry-forward).
        assert "used_bytes" not in (row.metadata or {}), row.action

    return rows


@pytest.mark.contract
@pytest.mark.django_db
class TestRecordingCompleteness:
    """AC-18 / R-AUD-1: every mutation writes exactly one row, through one helper."""

    def test_the_whole_flow_writes_one_complete_row_per_action(
        self, caller_agent, project, stored_objects
    ):
        session_client = caller_agent
        issue = make_issue(project)
        folder = session_client.post(folders_url(project.workspace.slug, project.id), {"name": "Docs"}, format="json")
        assert folder.status_code == status.HTTP_200_OK
        folder_id = folder.data["folder"]["id"]

        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        # rename + move
        assert session_client.patch(
            detail_url(project.workspace.slug, project.id, file_id),
            {"name_display": "Renamed.pdf", "folder_id": folder_id},
            format="json",
        ).status_code == status.HTTP_200_OK
        # a revision (version_created) and its activation
        revision = session_client.post(
            f"{detail_url(project.workspace.slug, project.id, file_id)}versions/",
            {"file_name": "Renamed.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )
        assert revision.status_code == status.HTTP_200_OK, revision.data
        stored_objects.append(FileVersion.objects.get(file_id=file_id, version_no=2).object_key)
        upload = revision.data["upload"]
        assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200
        assert session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 2, "size_bytes": len(PDF_BYTES)},
            format="json",
        ).status_code == status.HTTP_200_OK
        assert session_client.post(
            f"{detail_url(project.workspace.slug, project.id, file_id)}versions/2/activate/"
        ).status_code == status.HTTP_200_OK
        # a copy, then links
        copied = session_client.post(copy_url(project.workspace.slug, project.id, file_id), {}, format="json")
        assert copied.status_code == status.HTTP_200_OK, copied.data
        stored_objects.extend(
            FileVersion.objects.filter(file_id=copied.data["file"]["id"]).values_list("object_key", flat=True)
        )
        linked = session_client.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
            format="json",
        )
        assert linked.status_code == status.HTTP_200_OK
        assert session_client.delete(
            f"{links_url(project.workspace.slug, project.id, file_id)}{linked.data['link']['id']}/"
        ).status_code == status.HTTP_204_NO_CONTENT
        # a download, then an aborted attempt and a failed finalize
        assert session_client.get(download_url(project.workspace.slug, project.id, file_id)).status_code == 200
        abandoned = session_client.post(
            upload_url(project.workspace.slug, project.id),
            {"file_name": "Abandoned.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )
        assert abandoned.status_code == status.HTTP_200_OK
        assert session_client.post(
            abort_url(project.workspace.slug, project.id, abandoned.data["file"]["id"]), {"version_no": 1}, format="json"
        ).status_code == status.HTTP_204_NO_CONTENT
        # folder rename and move
        assert session_client.patch(
            folder_url(project.workspace.slug, project.id, folder_id), {"name": "Documents"}, format="json"
        ).status_code == status.HTTP_200_OK
        # trash, restore, trash, purge
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        assert session_client.post(
            f"{detail_url(project.workspace.slug, project.id, file_id)}restore/"
        ).status_code == status.HTTP_200_OK
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        assert session_client.delete(purge_url(project.workspace.slug, project.id, file_id)).status_code == 204

        rows = assert_every_row_is_complete(project)
        actions = [row.action for row in rows]
        # The flow performs each mutation once, except the two that happen twice:
        # an initiate for the file and for the abandoned attempt, and two trashes
        # around the restore.
        expected_counts = {
            "folder_created": 1,
            "upload_initiated": 3,  # the file, the revision and the abandoned attempt
            "upload_completed": 1,
            "renamed": 1,
            "moved": 1,
            "version_created": 1,
            "version_activated": 1,
            "copied": 1,
            "linked": 1,
            "unlinked": 1,
            "downloaded": 1,
            "upload_failed": 1,
            "folder_renamed": 1,
            "trashed": 2,
            "restored": 1,
            "purged": 1,
        }
        for action, count in expected_counts.items():
            assert actions.count(action) == count, f"{action}: {actions}"

        # The purge must not have removed a single row (AD-08, R-NFR-9).
        assert FileAccessLog.objects.filter(file_id=file_id).count() >= 8
        assert FileObject.all_objects.filter(pk=file_id).exists() is False

    def test_a_quota_refusal_records_the_numbers_it_turned_on(
        self, caller_agent, project, stored_objects
    ):
        from plane.utils.file_storage.quota import get_usage_rows

        get_usage_rows(project)
        StorageQuota.objects.filter(workspace=project.workspace).update(limit_bytes=100)

        refused = caller_agent.post(
            upload_url(project.workspace.slug, project.id),
            {"file_name": "Too big.pdf", "size_bytes": 5_000, "mime_type": "application/pdf"},
            format="json",
        )

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "quota_exceeded"
        row = FileAccessLog.objects.get(project_id=project.id, action=FileAccessLog.Action.QUOTA_REJECTED)
        assert row.metadata["limit_bytes"] == 100
        assert row.metadata["projected_bytes"] == 5_000
        assert row.metadata["level"] == "workspace"
        assert_every_row_is_complete(project)

    def test_each_failure_writes_exactly_one_row(self, caller_agent, project, stored_objects):
        initiated = caller_agent.post(
            upload_url(project.workspace.slug, project.id),
            {"file_name": "Fail.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )
        upload = initiated.data["upload"]
        assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200
        failed = caller_agent.post(
            complete_url(project.workspace.slug, project.id, initiated.data["file"]["id"]),
            {"version_no": 1, "size_bytes": len(PDF_BYTES) + 3},
            format="json",
        )
        assert failed.status_code == status.HTTP_400_BAD_REQUEST
        assert failed.data["code"] == "size_mismatch"

        rows = FileAccessLog.objects.filter(project_id=project.id, action=FileAccessLog.Action.UPLOAD_FAILED)
        assert rows.count() == 1
        assert rows.first().metadata["code"] == "size_mismatch"
        assert_every_row_is_complete(project)


@pytest.mark.contract
@pytest.mark.django_db
class TestActivityReadApi:
    """R-AUD-3: the project trail and the file's own history, filtered and paged."""

    def test_the_trail_filters_and_pages(self, caller_agent, project, stored_objects):
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        assert caller_agent.patch(
            detail_url(project.workspace.slug, project.id, file_id), {"name_display": "N.pdf"}, format="json"
        ).status_code == status.HTTP_200_OK
        caller_agent.get(download_url(project.workspace.slug, project.id, file_id))

        everything = caller_agent.get(activity_url(project.workspace.slug, project.id))
        assert everything.status_code == status.HTTP_200_OK, everything.data
        assert everything.data["page"]["total_results"] == 4, f"everything: {everything.data}"
        assert [row["action"] for row in everything.data["results"]][0] == "downloaded"

        by_action = caller_agent.get(activity_url(project.workspace.slug, project.id), {"action": "renamed"})
        assert [row["action"] for row in by_action.data["results"]] == ["renamed"]
        assert by_action.data["page"]["total_results"] == 1

        by_file = caller_agent.get(activity_url(project.workspace.slug, project.id), {"file_id": file_id})
        assert by_file.data["page"]["total_results"] == 4, f"by_file: {by_file.data}"

        by_actor = caller_agent.get(
            activity_url(project.workspace.slug, project.id), {"actor": str(project.created_by_id or "")}
        )
        assert by_actor.status_code == status.HTTP_200_OK  # an unparseable/empty actor is a substring match

        tomorrow = (timezone.now() + timedelta(days=1)).date().isoformat()
        future = caller_agent.get(activity_url(project.workspace.slug, project.id), {"created_from": tomorrow})
        assert future.data["page"]["total_results"] == 0
        today = timezone.now().date().isoformat()
        # A bare date must mean the *whole* day: Django's parse_datetime accepts a
        # bare date and returns midnight, which made created_to exclude the very day
        # it named before this ticket (the T-103 bug the shared helper now pins).
        to_today = caller_agent.get(activity_url(project.workspace.slug, project.id), {"created_to": today})
        assert to_today.data["page"]["total_results"] == 4, f"created_to today: {to_today.data}"

        bad = caller_agent.get(activity_url(project.workspace.slug, project.id), {"action": "nope"})
        assert bad.status_code == status.HTTP_400_BAD_REQUEST
        assert bad.data["code"] == "invalid_request"
        assert bad.data["field"] == "action"

        # The cursor is the repository's token, so a page can be walked.
        paged = caller_agent.get(
            activity_url(project.workspace.slug, project.id), {"page_size": 1}
        )
        assert len(paged.data["results"]) == 1
        assert paged.data["page"]["next_cursor"]
        assert paged.data["page"]["total_results"] == 4, f"paged: {paged.data}"

    def test_the_trail_is_admin_only_and_the_detail_carries_the_file_history(
        self, caller_agent, project, stored_objects
    ):
        member = add_member(project, email="audit-member@example.com", role=15)
        outsider = add_member(project, email="audit-outsider@example.com", role=20, active=False)
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)

        assert caller_agent.get(activity_url(project.workspace.slug, project.id)).status_code == 200
        assert member.get(activity_url(project.workspace.slug, project.id)).status_code == 403
        assert outsider.get(activity_url(project.workspace.slug, project.id)).status_code == 404

        detail = caller_agent.get(detail_url(project.workspace.slug, project.id, file_id))
        assert detail.status_code == status.HTTP_200_OK
        expected = list(
            FileAccessLog.objects.filter(file_id=file_id)
            .order_by("-created_at", "-id")
            .values_list("action", flat=True)
        )
        assert [row["action"] for row in detail.data["activity"]] == expected
        assert [row["id"] for row in detail.data["activity"]] == [
            str(row_id)
            for row_id in FileAccessLog.objects.filter(file_id=file_id)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        ]
        # A GUEST may read the file, so the file's own history comes with it.
        guest = add_member(project, email="audit-guest@example.com", role=5)
        assert guest.get(detail_url(project.workspace.slug, project.id, file_id)).status_code == 200


@pytest.mark.contract
@pytest.mark.django_db
class TestImmutabilityAndSurvival:
    """AD-08/AD-15: append-only through the API, and surviving the file."""

    def test_the_api_exposes_no_way_to_change_or_delete_a_row(
        self, caller_agent, project, stored_objects
    ):
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        row = FileAccessLog.objects.filter(file_id=file_id).first()
        url = f"{activity_url(project.workspace.slug, project.id)}{row.id}/"

        for method in ("patch", "put", "delete"):
            response = getattr(caller_agent, method)(url, {}, format="json")
            assert response.status_code in (404, 405), (method, response.status_code)

        assert FileAccessLog.objects.filter(id=row.id).exists() is True
        assert FileAccessLog.objects.get(id=row.id).metadata == row.metadata

    def test_rows_survive_unlink_relink_activation_and_purge(self, caller_agent, project, stored_objects):
        issue = make_issue(project)
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        linked = caller_agent.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
            format="json",
        )
        link_id = linked.data["link"]["id"]
        caller_agent.delete(f"{links_url(project.workspace.slug, project.id, file_id)}{link_id}/")
        caller_agent.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
            format="json",
        )
        before = {
            row.id: (row.action, row.created_at, json.dumps(row.metadata, sort_keys=True))
            for row in FileAccessLog.objects.filter(file_id=file_id)
        }

        assert caller_agent.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        assert caller_agent.delete(purge_url(project.workspace.slug, project.id, file_id)).status_code == 204

        after = {
            row.id: (row.action, row.created_at, json.dumps(row.metadata, sort_keys=True))
            for row in FileAccessLog.objects.filter(file_id=file_id)
        }
        # The purge adds its own row; every earlier row is byte-identical and none is gone.
        assert set(before) <= set(after), "no audit row may be removed"
        for row_id, snapshot in before.items():
            assert after[row_id] == snapshot, f"row {row_id} was rewritten"
        assert after != before
        new_rows = set(after) - set(before)
        assert len(new_rows) == 2, "the trash and the purge each add exactly one row"
        assert {
            action for row_id, (action, _, _) in after.items() if row_id in new_rows
        } == {"trashed", "purged"}
        assert FileObject.all_objects.filter(pk=file_id).exists() is False
        # And the trail is still readable through the API after the purge.
        assert caller_agent.get(activity_url(project.workspace.slug, project.id), {"file_id": file_id}).data[
            "page"
        ]["total_results"] == len(after)


@pytest.mark.contract
@pytest.mark.django_db
class TestAuditMasking:
    """AC-38 / R-NFR-13: the personal columns go, the skeleton stays."""

    @staticmethod
    def age(row, days):
        """Back-date a row by ``days``; ``created_at`` is ``auto_now_add`` on insert."""
        FileAccessLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=days))
        return FileAccessLog.objects.get(pk=row.pk)

    @staticmethod
    def masking_events(project):
        """The run's own ``pii_masked`` rows, oldest first."""
        return FileAccessLog.objects.filter(
            project=project, action=FileAccessLog.Action.PII_MASKED
        ).order_by("created_at", "id")

    def test_masking_clears_pii_and_keeps_the_skeleton(self, caller_agent, project, stored_objects, settings):
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        old_row = self.age(FileAccessLog.objects.filter(file_id=file_id).first(), 400)
        fresh = FileAccessLog.objects.create(
            workspace_id=project.workspace_id,
            project=project,
            file_id=file_id,
            file_name_snapshot="Fresh.pdf",
            action=FileAccessLog.Action.DOWNLOADED,
            actor_id=project.created_by_id,
            actor_display="Fresh Person",
            ip_address="10.0.0.9",
            user_agent="fresh-agent",
            metadata={},
        )

        summary = mask_audit_pii(batch_size=10)

        assert summary["masked_rows"] >= 1
        assert summary["retention_days"] == settings.AUDIT_PII_RETENTION_DAYS
        masked = FileAccessLog.objects.get(pk=old_row.pk)
        assert masked.ip_address is None
        assert masked.user_agent == ""
        assert masked.actor_display == ""
        assert masked.file_name_snapshot == ""
        # The skeleton survives: actor id, action, target and timestamps.
        assert masked.actor_id == old_row.actor_id
        assert masked.action == old_row.action
        assert masked.file_id == old_row.file_id
        assert masked.project_id == project.id
        assert masked.created_at == old_row.created_at
        assert masked.metadata == old_row.metadata

        # The run recorded itself once, with the numbers and the window it applied.
        event = self.masking_events(project).get()
        assert event.file_id is None
        assert event.metadata["masked_rows"] == 1
        assert event.metadata["retention_days"] == summary["retention_days"]
        assert event.metadata["cutoff"] == summary["cutoff"]
        # No request wrote it, so it holds nothing personal: nothing for the next run
        # to mask, and nothing for it to recurse into.
        assert event.actor_id is None
        assert event.actor_display == ""
        assert event.ip_address is None
        assert event.user_agent == ""
        assert event.file_name_snapshot == ""

        # A fresh row is untouched, and a second run changes nothing at all - not the
        # rows, not the trail (no new event for a run that masked nothing).
        untouched = FileAccessLog.objects.get(pk=fresh.pk)
        assert untouched.user_agent == "fresh-agent"
        assert untouched.actor_display == "Fresh Person"
        assert mask_audit_pii(batch_size=10)["masked_rows"] == 0
        assert self.masking_events(project).count() == 1

        # The read API still serves the masked skeleton.
        served = caller_agent.get(activity_url(project.workspace.slug, project.id), {"file_id": file_id})
        assert served.status_code == status.HTTP_200_OK
        assert served.data["page"]["total_results"] == len(FileAccessLog.objects.filter(file_id=file_id))
        masked_payload = next(row for row in served.data["results"] if row["id"] == str(old_row.pk))
        assert masked_payload["actor"]["id"] == str(old_row.actor_id)
        assert masked_payload["ip_address"] is None
        assert masked_payload["actor_display"] == ""

        # And the run's own row is a first-class audit row: the project trail serves
        # it, and the action filter knows its value.
        events = caller_agent.get(
            activity_url(project.workspace.slug, project.id), {"action": FileAccessLog.Action.PII_MASKED}
        )
        assert events.status_code == status.HTTP_200_OK
        assert events.data["page"]["total_results"] == 1
        assert events.data["results"][0]["actor"]["id"] is None
        assert events.data["results"][0]["metadata"]["masked_rows"] == 1

    def test_the_window_is_the_configured_number_of_days(
        self, caller_agent, project, stored_objects, settings
    ):
        """The cutoff moves with ``AUDIT_PII_RETENTION_DAYS``, in both directions."""
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        rows = list(FileAccessLog.objects.filter(file_id=file_id).order_by("created_at", "id"))
        assert len(rows) >= 2, "the upload flow writes at least the initiate and complete rows"
        outside, inside = self.age(rows[0], 8), self.age(rows[1], 6)

        settings.AUDIT_PII_RETENTION_DAYS = 7
        assert mask_audit_pii(batch_size=10)["masked_rows"] == 1

        assert FileAccessLog.objects.get(pk=outside.pk).ip_address is None
        assert FileAccessLog.objects.get(pk=inside.pk).user_agent == USER_AGENT
        assert FileAccessLog.objects.get(pk=inside.pk).actor_display
        event = self.masking_events(project).get()
        assert event.metadata["retention_days"] == 7
        assert event.metadata["masked_rows"] == 1

        # Widen the window past both rows: the six-day row is inside it now, nothing
        # qualifies, and the run adds no event.
        settings.AUDIT_PII_RETENTION_DAYS = 9
        assert mask_audit_pii(batch_size=10)["masked_rows"] == 0
        assert FileAccessLog.objects.get(pk=inside.pk).user_agent == USER_AGENT
        assert self.masking_events(project).count() == 1

        # Narrow it under the six-day row and the very same run masks it.
        settings.AUDIT_PII_RETENTION_DAYS = 3
        assert mask_audit_pii(batch_size=10)["masked_rows"] == 1
        assert FileAccessLog.objects.get(pk=inside.pk).user_agent == ""
        assert FileAccessLog.objects.get(pk=inside.pk).file_name_snapshot == ""
        events = self.masking_events(project)
        assert events.count() == 2
        assert events.last().metadata["retention_days"] == 3
        assert events.last().metadata["masked_rows"] == 1

    def test_a_zero_day_window_masks_everything_the_run_sees(
        self, caller_agent, project, stored_objects, settings
    ):
        """``0`` is a window, not a toggle: the trail keeps its skeleton either way."""
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        fresh = FileAccessLog.objects.filter(file_id=file_id).first()

        settings.AUDIT_PII_RETENTION_DAYS = 0
        summary = mask_audit_pii(batch_size=10)

        assert summary["retention_days"] == 0
        masked = FileAccessLog.objects.get(pk=fresh.pk)
        assert masked.ip_address is None
        assert masked.user_agent == ""
        assert masked.actor_display == ""
        assert masked.file_name_snapshot == ""
        # ... and the skeleton is still there, for the project's lifetime.
        assert masked.action == fresh.action
        assert masked.actor_id == fresh.actor_id
        assert masked.created_at == fresh.created_at
        assert masked.project_id == project.id

        # The event holds nothing to mask, so the next run leaves it (and everything
        # else) alone: the sharpest window still cannot recurse.
        event = self.masking_events(project).get()
        assert event.metadata["retention_days"] == 0
        assert event.metadata["masked_rows"] == FileAccessLog.objects.filter(project_id=project.id).count() - 1
        assert mask_audit_pii(batch_size=10)["masked_rows"] == 0
        assert self.masking_events(project).count() == 1

    def test_a_masked_row_with_no_project_is_still_masked(self, caller_agent, project, stored_objects):
        """A hard-deleted project leaves rows that are masked without an event.

        The row below has exactly the shape that FK leaves behind: the workspace and
        every column intact, the project id null. Masking must not depend on a project
        existing to attach an event to, and the run must not fail for lack of one - and
        the count it could not attribute has to be reported rather than dropped.
        """
        orphan = FileAccessLog.objects.create(
            workspace_id=project.workspace_id,
            project=None,
            file_name_snapshot="Orphaned.pdf",
            action=FileAccessLog.Action.TRASHED,
            actor_id=None,
            actor_display="Orphan Person",
            ip_address="10.2.2.2",
            user_agent="orphan-agent",
            metadata={},
        )
        orphan = self.age(orphan, 400)

        summary = mask_audit_pii(batch_size=10)

        assert summary["masked_rows"] >= 1
        assert summary["unrecorded_rows"] == 1
        masked = FileAccessLog.objects.get(pk=orphan.pk)
        assert masked.ip_address is None
        assert masked.user_agent == ""
        assert masked.actor_display == ""
        assert masked.file_name_snapshot == ""
        # The skeleton is what remains, and it is still the skeleton.
        assert masked.action == FileAccessLog.Action.TRASHED
        assert masked.workspace_id == project.workspace_id
        assert masked.created_at == orphan.created_at
        # No project, so nowhere to attach the run's event: nothing else was maskable
        # in this database, and the run still reports what it did.
        assert FileAccessLog.objects.filter(action=FileAccessLog.Action.PII_MASKED).count() == 0

    def test_a_soft_deleted_project_still_gets_its_event(self, caller_agent, project, stored_objects):
        """Deleting a project is Plane's ordinary action; its masking must be recorded.

        ``Project.objects`` is soft-deletion-aware, so looking the project up through it
        would silently skip every project a user deleted - the most common path of all -
        and mask its rows without a word. The lookup goes through ``all_objects``, so the
        event survives the project's ``deleted_at``.
        """
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        self.age(FileAccessLog.objects.filter(file_id=file_id).first(), 400)

        # The state the delete-project action leaves: the row stays, deleted_at is set.
        # (Setting the column directly, not calling .delete(), keeps the view's Celery
        # hand-off - soft_delete_related_objects.delay - out of a broker-less suite.)
        Project.objects.filter(pk=project.pk).update(deleted_at=timezone.now())
        assert Project.objects.filter(pk=project.pk).exists() is False
        assert Project.all_objects.filter(pk=project.pk).exists() is True

        assert mask_audit_pii(batch_size=10)["masked_rows"] >= 1

        event = FileAccessLog.objects.get(project_id=project.pk, action=FileAccessLog.Action.PII_MASKED)
        assert event.metadata["masked_rows"] >= 1
        assert event.workspace_id == project.workspace_id
        assert event.file_id is None

    def test_a_window_longer_than_the_calendar_masks_nothing(
        self, caller_agent, project, stored_objects, settings
    ):
        """A nonsense window degrades into "nothing qualifies", never a crashed task."""
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        old_row = self.age(FileAccessLog.objects.filter(file_id=file_id).first(), 400)

        settings.AUDIT_PII_RETENTION_DAYS = 3_000_000
        summary = mask_audit_pii(batch_size=10)

        assert summary["masked_rows"] == 0
        assert summary["unrecorded_rows"] == 0
        assert summary["retention_days"] == 3_000_000
        assert self.masking_events(project).count() == 0
        # The row is a 400-day-old row, and a three-million-day window is longer than
        # the calendar, so it keeps its personal data.
        assert FileAccessLog.objects.get(pk=old_row.pk).ip_address == "127.0.0.1"

    def test_an_overlapping_run_cannot_inflate_the_event(
        self, caller_agent, project, stored_objects, settings, mocker
    ):
        """The event counts rows this run cleared, not rows it selected (T-119 F-1).

        The interleave is forced rather than raced: the first ``UPDATE`` the task issues
        is wrapped, and a second run with a wider window clears part of the batch in
        between the selection and the update. Counting the selection would then report
        rows the other run had already cleared.

        The masking task now issues one ``UPDATE ... RETURNING`` per batch through
        ``_mask_batch``; that is the chokepoint to wrap. The wrapper invokes the real
        function once the recursive ``mask_audit_pii`` has cleared what it can, so
        the outer run's ``RETURNING`` reports only what it itself changed.
        """
        rows = [
            self.age(
                FileAccessLog.objects.create(
                    workspace_id=project.workspace_id,
                    project=project,
                    file_name_snapshot=f"Overlap {index}.pdf",
                    action=FileAccessLog.Action.TRASHED,
                    actor_id=None,
                    actor_display=f"Overlap {index}",
                    ip_address="10.3.3.3",
                    user_agent="overlap-agent",
                    metadata={},
                ),
                age_days,
            )
            for index, age_days in enumerate((400, 390, 380, 375, 370))
        ]

        real_mask_batch = file_audit_task._mask_batch
        state = {"fired": False}

        def mask_batch(candidate_ids, masked_per_project):
            if not state["fired"]:
                state["fired"] = True
                settings.AUDIT_PII_RETENTION_DAYS = 385
                # The recursive run goes through the full pipeline (selection +
                # update + event) so its retention window narrows its own candidate
                # set to the two rows that are older than 385 days; the outer run's
                # next call then ``RETURNING``s only the ones the outer run itself
                # changed.
                mask_audit_pii(batch_size=10)
            # The outer run goes through the real ``_mask_batch`` with its own
            # (empty) accumulator. The ``UPDATE ... RETURNING`` filters out the two
            # rows the recursive run already masked, so this ``RETURNING`` returns
            # exactly the rows the outer run changed.
            return real_mask_batch(candidate_ids, masked_per_project)

        mocker.patch.object(file_audit_task, "_mask_batch", mask_batch)

        settings.AUDIT_PII_RETENTION_DAYS = 90
        summary = mask_audit_pii(batch_size=10)

        # Five rows lost their personal data: two to the other run, three to this one.
        assert state["fired"] is True
        assert summary["masked_rows"] == 3
        assert summary["unrecorded_rows"] == 0
        assert all(
            FileAccessLog.objects.get(pk=row.pk).ip_address is None for row in rows
        ), "every row in the batch ends up masked, whichever run got there first"

        events = self.masking_events(project)
        assert [event.metadata["masked_rows"] for event in events] == [2, 3], (
            "each run reports its own rows: the selection of the outer run was five, "
            "and claiming five would have counted the other run's two as its own"
        )
        assert sum(event.metadata["masked_rows"] for event in events) == 5

    def test_the_masking_event_is_not_exempt_from_the_window(
        self, caller_agent, project, stored_objects
    ):
        """The run's row is an ordinary row: no action is excluded from the sweep."""
        file_id = upload_file(caller_agent, project, stored_objects=stored_objects)
        self.age(FileAccessLog.objects.filter(file_id=file_id).first(), 400)
        assert mask_audit_pii(batch_size=10)["masked_rows"] >= 1
        event = self.masking_events(project).get()

        # Suppose a masking event carried personal data anyway. Nothing about the
        # action exempts it: past the window it is masked and reported like any row.
        FileAccessLog.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(days=400),
            ip_address="10.1.1.1",
            user_agent="event-agent",
            actor_display="Event Person",
        )
        assert mask_audit_pii(batch_size=10)["masked_rows"] == 1

        masked_event = FileAccessLog.objects.get(pk=event.pk)
        assert masked_event.action == FileAccessLog.Action.PII_MASKED
        assert masked_event.ip_address is None
        assert masked_event.user_agent == ""
        assert masked_event.actor_display == ""
        assert masked_event.actor_id is None
        # Masking its own row does not cost the trail its counts.
        assert masked_event.metadata == event.metadata
        # That run recorded itself once more - one event per run that changed rows,
        # never a chain.
        events = self.masking_events(project)
        assert events.count() == 2
        assert events.last().metadata["masked_rows"] == 1
