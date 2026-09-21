# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-115 / DEFECT-011: a saved page keeps a project-file embed in its html mirror.

``PageBinaryUpdateSerializer.validate_description_html`` writes the sanitised string
back, so a reference the sanitiser drops is dropped from the row every save. These two
tests pin the mirror: the reference survives, and a script-capable source does not.
"""

# Python imports
import base64
from unittest import mock

# Third party imports
import pytest

#: A reference as the editor writes it: the scheme and a file id.
REF = "project-file:8f0e0c1e-0000-4000-8000-000000000000"


@pytest.mark.contract
@pytest.mark.django_db
class TestPageSaveKeepsTheEmbed:
    """The mirror, not only the function: a saved page keeps the reference.

    The page save also enqueues a Celery task after the write, and these tests stub that
    enqueue: the subject here is what the serializer stores, and the broker's
    availability must not decide whether the mirror is asserted. (That an unreachable
    broker turns a successful save into a 500 is a separate, recorded defect - a
    post-save side effect must not fail the write - and is deliberately not asserted
    here.)
    """

    def _save_page(self, session_client, workspace, project, page, html, binary_seed):
        """Save the document the way the editor does: the description endpoint."""
        binary = base64.b64encode(binary_seed).decode()
        with (
            mock.patch("plane.app.views.page.base.page_transaction.delay"),
            mock.patch("plane.app.views.page.base.track_page_version.delay"),
        ):
            return session_client.patch(
                f"/api/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/description/",
                {"description_html": html, "description_binary": binary},
                format="json",
            )

    def test_saving_a_page_keeps_a_project_file_embed_in_its_html(self, session_client, create_user, workspace):
        from plane.db.models import Page, Project, ProjectMember, ProjectPage

        project = Project.objects.create(name="Mirror", identifier="MIRROR", workspace=workspace)
        ProjectMember.objects.create(
            workspace=workspace, project=project, member=create_user, role=20, is_active=True
        )
        page = Page.objects.create(
            workspace=workspace, owned_by=create_user, access=Page.PUBLIC_ACCESS, name="Embedded"
        )
        ProjectPage.objects.create(workspace=workspace, project=project, page=page)

        html = f'<p>before</p><image-component src="{REF}" status="uploaded"></image-component>'

        response = self._save_page(session_client, workspace, project, page, html, b"yjs-binary-placeholder\x00\x01")
        assert response.status_code == 200, response.data

        page.refresh_from_db()
        assert REF in page.description_html
        assert f'src="{REF}"' in page.description_html
        assert page.description_binary is not None

    def test_saving_a_page_still_strips_a_script_source(self, session_client, create_user, workspace):
        """The fix must not have opened the door it exists to close."""
        from plane.db.models import Page, Project, ProjectMember, ProjectPage

        project = Project.objects.create(name="Mirror2", identifier="MIRROR2", workspace=workspace)
        ProjectMember.objects.create(
            workspace=workspace, project=project, member=create_user, role=20, is_active=True
        )
        page = Page.objects.create(
            workspace=workspace, owned_by=create_user, access=Page.PUBLIC_ACCESS, name="Hostile"
        )
        ProjectPage.objects.create(workspace=workspace, project=project, page=page)

        html = '<img src="javascript:alert(1)" alt="x">'

        response = self._save_page(session_client, workspace, project, page, html, b"yjs-binary-placeholder\x00\x02")
        assert response.status_code == 200, response.data

        page.refresh_from_db()
        assert "javascript:" not in (page.description_html or "")
