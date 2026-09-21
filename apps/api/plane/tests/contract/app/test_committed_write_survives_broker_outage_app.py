# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A follow-up enqueue must not turn a committed write into a failure (DEFECT-010).

``IssueViewSet.create`` called ``issue_activity.delay()`` *after* the serializer had
saved the row, so a broker that was unreachable - kombu raises ``OperationalError``
when the connection cannot be established, which is the state this environment's
``plane_test-mq_1`` restarts into - answered **500** for a work item that had
actually been created. The same shape sat on the page save path
(``page_transaction.delay``, ``track_page_version.delay``) and in
``SoftDeleteModel.delete``, whose related-object cascade hand-off runs after the row
has already been soft-deleted, so *every* DELETE endpoint could fail the same way.

Each test here forces the hand-off to raise the way a dead broker does, then asserts
three things: the write is durable, the response is the success the write earned, and
the operator-visible record names the work that was dropped. The enqueue is stubbed
rather than awaited, so no test depends on a broker being reachable.
"""

# Python imports
import logging
from unittest import mock

# Django imports
from django.utils import timezone

# Third party imports
import pytest
from kombu.exceptions import OperationalError
from rest_framework import status

# Module imports
from plane.db.models import Issue, Page, Project, ProjectMember, ProjectPage

ISSUES_URL = "/api/workspaces/{slug}/projects/{project_id}/issues/"
PAGES_URL = "/api/workspaces/{slug}/projects/{project_id}/pages/"
PAGE_URL = "/api/workspaces/{slug}/projects/{project_id}/pages/{page_id}/"
PAGE_DESCRIPTION_URL = "/api/workspaces/{slug}/projects/{project_id}/pages/{page_id}/description/"

DROPPED = "dropped follow-up task"


@pytest.fixture
def project(db, workspace, create_user):
    """A project the session user administers."""
    project = Project.objects.create(name="Broker Outage", identifier="BRK", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


@pytest.fixture
def page(db, workspace, project, create_user):
    """A public page of that project, owned by the session user."""
    page = Page.objects.create(
        workspace=workspace, owned_by=create_user, access=Page.PUBLIC_ACCESS, name="Ordered"
    )
    ProjectPage.objects.create(workspace=workspace, project=project, page=page)
    return page


def dropped_work_records(caplog):
    """The messages an operator reading the error log would see for a dropped hand-off."""
    return [record for record in caplog.records if DROPPED in record.getMessage()]


class TestWorkItemCreateSurvivesBrokerOutage:
    @pytest.mark.django_db
    def test_the_row_is_written_and_the_response_is_201(self, session_client, workspace, project, caplog):
        payload = {"name": "Created while the broker was down"}

        with mock.patch("plane.app.views.issue.base.issue_activity") as activity:
            activity.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.post(
                ISSUES_URL.format(slug=workspace.slug, project_id=project.id), payload, format="json"
            )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        # The write the client asked for is durable, and the response describes it.
        issue = Issue.objects.get(id=response.data["id"])
        assert issue.name == payload["name"]
        assert str(issue.project_id) == str(project.id)
        # The hand-off was attempted exactly once and did not run twice.
        activity.delay.assert_called_once()

        # An operator sees which follow-up was dropped, and for which row.
        records = dropped_work_records(caplog)
        assert records, "the dropped hand-off left no operator-visible record"
        assert records[0].levelno == logging.ERROR
        assert "issue_activity" in records[0].getMessage()
        assert str(issue.id) in records[0].getMessage()


class TestPageSaveSurvivesBrokerOutage:
    @pytest.mark.django_db
    def test_the_description_is_saved_and_the_response_is_200(
        self, session_client, workspace, project, page, caplog
    ):
        """The endpoint the editor saves through hands off two follow-ups."""
        payload = {"description_html": "<p>Edited while the broker was down</p>"}
        url = PAGE_DESCRIPTION_URL.format(slug=workspace.slug, project_id=project.id, page_id=page.id)

        with mock.patch("plane.app.views.page.base.page_transaction") as transaction_task, mock.patch(
            "plane.app.views.page.base.track_page_version"
        ) as version_task:
            transaction_task.delay.side_effect = OperationalError("broker unavailable")
            version_task.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.patch(url, payload, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data
        page.refresh_from_db()
        assert page.description_html == payload["description_html"]
        transaction_task.delay.assert_called_once()
        version_task.delay.assert_called_once()

        records = dropped_work_records(caplog)
        messages = [record.getMessage() for record in records]
        assert len(messages) == 2, messages
        assert any("page_transaction" in message for message in messages)
        assert any("track_page_version" in message for message in messages)
        assert all(str(page.id) in message for message in messages)

    @pytest.mark.django_db
    def test_a_page_update_is_saved_and_the_response_is_200(
        self, session_client, workspace, project, page, caplog
    ):
        """The same page written through the page endpoint, which hands off one follow-up."""
        payload = {"description_html": "<p>Renamed while the broker was down</p>", "name": "Renamed"}
        url = PAGE_URL.format(slug=workspace.slug, project_id=project.id, page_id=page.id)

        with mock.patch("plane.app.views.page.base.page_transaction") as transaction_task:
            transaction_task.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.patch(url, payload, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data
        page.refresh_from_db()
        assert page.name == payload["name"]
        assert page.description_html == payload["description_html"]

        records = dropped_work_records(caplog)
        assert records, "the dropped hand-off left no operator-visible record"
        assert "page_transaction" in records[0].getMessage()
        assert str(page.id) in records[0].getMessage()


class TestSoftDeleteSurvivesBrokerOutage:
    @pytest.mark.django_db
    def test_the_row_is_soft_deleted_and_the_response_is_204(
        self, session_client, workspace, project, page, caplog
    ):
        """``SoftDeleteModel.delete`` soft-deletes, then hands the cascade to a worker.

        Deleting is the write; the cascade behind it is a follow-up, so a dead broker
        must not report the delete as failed.
        """
        Page.objects.filter(pk=page.pk).update(archived_at=timezone.now())
        url = PAGE_URL.format(slug=workspace.slug, project_id=project.id, page_id=page.id)

        with mock.patch("plane.db.mixins.soft_delete_related_objects") as cascade:
            cascade.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.delete(url)

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.data
        assert Page.objects.filter(pk=page.pk).exists() is False
        assert Page.all_objects.get(pk=page.pk).deleted_at is not None
        cascade.delay.assert_called_once()

        records = dropped_work_records(caplog)
        assert records, "the dropped cascade hand-off left no operator-visible record"
        assert "soft_delete_related_objects" in records[0].getMessage()
        assert str(page.id) in records[0].getMessage()
