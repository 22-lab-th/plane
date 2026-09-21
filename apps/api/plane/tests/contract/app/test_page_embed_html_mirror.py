# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-115 / DEFECT-011 + DEFECT-012: the page's html mirror is sanitised on every route.

DEFECT-011: ``PageBinaryUpdateSerializer.validate_description_html`` writes the sanitised
string back, so a reference the sanitiser drops is dropped from the row every save -
the mirror must keep a project-file embed, and must still strip script-capable markup.

DEFECT-012: the page **metadata** route wrote ``description_html`` through
``PageDetailSerializer``, which validated nothing, so the same body that was sanitised on
the description route put raw script markup in the row. Both routes now share one rule.
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
        url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/description/"
        with (
            mock.patch("plane.app.views.page.base.page_transaction.delay"),
            mock.patch("plane.app.views.page.base.track_page_version.delay"),
        ):
            return session_client.patch(
                url,
                {"description_html": html, "description_binary": binary},
                format="json",
                HTTP_IF_MATCH=session_client.get(url)["X-Plane-Document-Version"],
            )

    def test_saving_a_page_keeps_a_project_file_embed_in_its_html(self, session_client, create_user, workspace):
        from plane.db.models import Page, Project, ProjectMember, ProjectPage

        project = Project.objects.create(name="Mirror", identifier="MIRROR", workspace=workspace)
        ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
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
        ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
        page = Page.objects.create(workspace=workspace, owned_by=create_user, access=Page.PUBLIC_ACCESS, name="Hostile")
        ProjectPage.objects.create(workspace=workspace, project=project, page=page)

        html = '<img src="javascript:alert(1)" alt="x">'

        response = self._save_page(session_client, workspace, project, page, html, b"yjs-binary-placeholder\x00\x02")
        assert response.status_code == 200, response.data

        page.refresh_from_db()
        assert "javascript:" not in (page.description_html or "")


@pytest.mark.contract
@pytest.mark.django_db
class TestPageMetadataRouteSanitisesTheDescription:
    """DEFECT-012: the metadata route is not a second, unvalidated door."""

    def _page(self, workspace, create_user, name, identifier):
        from plane.db.models import Page, Project, ProjectMember, ProjectPage

        project = Project.objects.create(name=name, identifier=identifier, workspace=workspace)
        ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
        page = Page.objects.create(workspace=workspace, owned_by=create_user, access=Page.PUBLIC_ACCESS, name=name)
        ProjectPage.objects.create(workspace=workspace, project=project, page=page)
        return project, page

    def test_hostile_markup_sent_to_the_metadata_route_is_stored_sanitised(
        self, session_client, create_user, workspace
    ):
        project, page = self._page(workspace, create_user, "Metadata", "METADATA")

        html = '<p>kept</p><img src="javascript:alert(1)" alt="x"><script>alert(2)</script>'
        with (
            mock.patch("plane.app.views.page.base.page_transaction.delay"),
            mock.patch("plane.app.views.page.base.track_page_version.delay"),
        ):
            response = session_client.patch(
                f"/api/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/",
                {"description_html": html},
                format="json",
            )

        assert response.status_code == 200, response.data
        page.refresh_from_db()
        stored = page.description_html or ""
        assert "<p>kept</p>" in stored
        assert "javascript:" not in stored
        assert "<script" not in stored

    def test_the_metadata_route_still_keeps_a_project_file_embed(self, session_client, create_user, workspace):
        """The door is closed without losing what DEFECT-011 opened it for."""
        project, page = self._page(workspace, create_user, "Metadata embed", "METAEMBED")

        html = f'<image-component src="{REF}" status="uploaded"></image-component>'
        with (
            mock.patch("plane.app.views.page.base.page_transaction.delay"),
            mock.patch("plane.app.views.page.base.track_page_version.delay"),
        ):
            response = session_client.patch(
                f"/api/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/",
                {"description_html": html},
                format="json",
            )

        assert response.status_code == 200, response.data
        page.refresh_from_db()
        assert f'src="{REF}"' in (page.description_html or "")
