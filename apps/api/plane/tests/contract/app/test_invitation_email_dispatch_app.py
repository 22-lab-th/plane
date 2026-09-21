# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An invitation's response promises the email; a member addition's does not.

`best_effort_delay` (plane/utils/task_dispatch.py) absorbs a broker refusal so a
committed write is not reported as a failure. That is right for a follow-up and wrong
for a task whose delivery *is* the response's promise: `POST
/api/workspaces/{slug}/invitations/` and `POST
/api/workspaces/{slug}/projects/{project_id}/invitations/` answer "Email(s) sent
successfully", so a hand-off the broker refuses has to surface there instead of
returning success for an email that was never queued - in this fork it arrives as the
view base's generic 500, logged on the same `plane.exception` channel, while the
invitation rows it wrote stay durable for a retry.

The project invitation endpoint carried two older bugs that made the question moot,
because it answered 500 for every payload: its role check read `.role` off a queryset
(``AttributeError``), and its dispatch called `.delay` on the ``bulk_create`` result -
a plain list - instead of on the ``project_invitation`` task, which the module never
imported.

`POST .../members/` is the other side of the line: its response serializes the member
rows it just created, so the addition is what the caller is promised and the
notification email behind it is a follow-up that must not fail the request.
"""

# Python imports
from unittest import mock
from uuid import uuid4

# Third party imports
import pytest
from kombu.exceptions import OperationalError
from rest_framework import status

# Module imports
from plane.db.models import (
    Project,
    ProjectMember,
    ProjectMemberInvite,
    User,
    WorkspaceMember,
    WorkspaceMemberInvite,
)

WORKSPACE_INVITATIONS_URL = "/api/workspaces/{slug}/invitations/"
PROJECT_INVITATIONS_URL = "/api/workspaces/{slug}/projects/{project_id}/invitations/"
PROJECT_MEMBERS_URL = "/api/workspaces/{slug}/projects/{project_id}/members/"

INVITEE = "invitee@plane.so"


@pytest.fixture
def project(db, workspace, create_user):
    """A project the session user administers."""
    project = Project.objects.create(name="Invites", identifier="INV", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


@pytest.fixture
def colleague(db, workspace):
    """An active workspace member who is not yet on the project."""
    unique_id = uuid4().hex[:8]
    user = User.objects.create(
        email=f"colleague-{unique_id}@plane.so",
        username=f"colleague_{unique_id}",
        first_name="Colleague",
        last_name="User",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=15, is_active=True)
    return user


class TestInvitationEndpointsReportARefusedHandOff:
    @pytest.mark.django_db
    def test_the_project_invitation_is_created_and_the_email_is_handed_to_the_task(
        self, session_client, workspace, project, create_user
    ):
        payload = {"emails": [{"email": INVITEE, "role": 5}]}

        with mock.patch("plane.app.views.project.invite.project_invitation") as task:
            response = session_client.post(
                PROJECT_INVITATIONS_URL.format(slug=workspace.slug, project_id=project.id),
                payload,
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["message"] == "Email sent successfully"

        invite = ProjectMemberInvite.objects.get(project_id=project.id, email=INVITEE)
        # The task is called with the invitation it is about, not with the list of
        # rows the bulk create returned.
        task.delay.assert_called_once()
        args = task.delay.call_args.args
        assert args[0] == invite.email
        assert str(args[1]) == str(project.id)
        assert args[2] == invite.token
        assert args[4] == create_user.email

    @pytest.mark.django_db
    def test_a_refused_project_invitation_hand_off_is_not_reported_as_sent(
        self, session_client, workspace, project
    ):
        payload = {"emails": [{"email": INVITEE, "role": 5}]}

        with mock.patch("plane.app.views.project.invite.project_invitation") as task:
            task.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.post(
                PROJECT_INVITATIONS_URL.format(slug=workspace.slug, project_id=project.id),
                payload,
                format="json",
            )

        # The refusal reaches the client: this fork's view base turns it into its
        # generic 500 (and logs it on plane.exception), never into the success message
        # this endpoint returns when the hand-off was accepted.
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "sent successfully" not in str(response.data).lower()
        # The rows the invitation writes are durable; what the caller does not get is
        # a success message for an email that was never queued.
        assert ProjectMemberInvite.objects.filter(project_id=project.id, email=INVITEE).exists()

    @pytest.mark.django_db
    def test_the_workspace_invitation_is_created_and_the_email_is_handed_to_the_task(
        self, session_client, workspace, create_user
    ):
        payload = {"emails": [{"email": INVITEE, "role": 5}]}

        with mock.patch("plane.app.views.workspace.invite.workspace_invitation") as task:
            response = session_client.post(
                WORKSPACE_INVITATIONS_URL.format(slug=workspace.slug), payload, format="json"
            )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["message"] == "Emails sent successfully"

        invite = WorkspaceMemberInvite.objects.get(workspace_id=workspace.id, email=INVITEE)
        task.delay.assert_called_once()
        args = task.delay.call_args.args
        assert args[0] == invite.email
        assert str(args[1]) == str(workspace.id)
        assert args[2] == invite.token
        assert args[4] == create_user.email

    @pytest.mark.django_db
    def test_a_refused_workspace_invitation_hand_off_is_not_reported_as_sent(
        self, session_client, workspace
    ):
        payload = {"emails": [{"email": INVITEE, "role": 5}]}

        with mock.patch("plane.app.views.workspace.invite.workspace_invitation") as task:
            task.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.post(
                WORKSPACE_INVITATIONS_URL.format(slug=workspace.slug), payload, format="json"
            )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "sent successfully" not in str(response.data).lower()
        assert WorkspaceMemberInvite.objects.filter(workspace_id=workspace.id, email=INVITEE).exists()


class TestMemberAdditionKeepsItsNotificationAsAFollowUp:
    @pytest.mark.django_db
    def test_the_member_is_added_and_the_response_is_201_when_the_email_is_dropped(
        self, session_client, workspace, project, colleague, caplog
    ):
        payload = {"members": [{"member_id": str(colleague.id), "role": 15}]}

        with mock.patch("plane.app.views.project.member.project_add_user_email") as task:
            task.delay.side_effect = OperationalError("broker unavailable")
            response = session_client.post(
                PROJECT_MEMBERS_URL.format(slug=workspace.slug, project_id=project.id),
                payload,
                format="json",
            )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        member = ProjectMember.objects.get(project_id=project.id, member_id=colleague.id)
        assert member.role == 15
        task.delay.assert_called_once()

        # The dropped notification is still recorded for an operator, even though the
        # response does not depend on it.
        dropped = [
            record.getMessage()
            for record in caplog.records
            if "dropped follow-up task" in record.getMessage()
        ]
        assert dropped, "the dropped notification left no operator-visible record"
        assert any("project_add_user_email" in message for message in dropped)
