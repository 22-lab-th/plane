# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Unit tests for immutable ``Project.storage_key`` assignment (DEC-001)."""

# Django imports
from django.utils import timezone

# Third party imports
import pytest

# Module imports
from plane.db.models import Project, Workspace


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Storage Key Workspace", slug="storage-key-workspace", owner=create_user)
    return Project.objects.create(name="Storage Key Project", identifier="SKP", workspace=workspace)


@pytest.mark.unit
class TestEnsureStorageKey:
    """The key is assigned once and never regenerated, even from a stale instance."""

    @pytest.mark.django_db
    def test_assignment_is_stable_across_calls(self, project):
        first = project.ensure_storage_key()
        second = project.ensure_storage_key()

        assert first == second == "SKP-storage-key-project"
        assert Project.objects.get(pk=project.pk).storage_key == first

    @pytest.mark.django_db
    def test_a_stale_instance_never_overwrites_a_committed_key(self, project):
        """The reported defect: a stale copy used to re-derive and overwrite the key."""
        stale = Project.objects.get(pk=project.pk)
        assert stale.storage_key is None

        committed = project.ensure_storage_key()
        adopted = stale.ensure_storage_key()

        assert adopted == committed
        assert Project.objects.get(pk=project.pk).storage_key == committed

    @pytest.mark.django_db
    def test_a_rename_after_assignment_does_not_re_key(self, project):
        committed = project.ensure_storage_key()

        project.name = "Renamed Project"
        project.save(update_fields=["name", "updated_at"])

        assert project.ensure_storage_key() == committed
        assert Project.objects.get(pk=project.pk).storage_key == committed

    @pytest.mark.django_db
    def test_a_successor_project_with_the_same_identifier_is_suffixed(self, project):
        committed = project.ensure_storage_key()

        # A soft-deleted project keeps its key, so the same identifier can be
        # reused and the successor must take the next suffix.
        Project.objects.filter(pk=project.pk).update(deleted_at=timezone.now())
        successor = Project.objects.create(
            name=project.name, identifier=project.identifier, workspace=project.workspace
        )

        successor_key = successor.ensure_storage_key()

        assert successor_key == f"{committed}-2"
        assert Project.all_objects.get(pk=project.pk).storage_key == committed

    @pytest.mark.django_db
    def test_two_projects_in_different_workspaces_may_share_a_prefix(self, project, create_user):
        other_workspace = Workspace.objects.create(
            name="Other Workspace", slug="other-workspace", owner=create_user
        )
        other = Project.objects.create(name=project.name, identifier=project.identifier, workspace=other_workspace)

        assert project.ensure_storage_key() == other.ensure_storage_key()
