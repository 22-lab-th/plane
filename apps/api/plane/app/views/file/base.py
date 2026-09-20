# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Helpers shared by the project-file endpoints.

Two rules are enforced here so no endpoint can forget them:

* a project is always resolved from the workspace slug **and** the project id,
  so a project in another workspace never resolves (AD-06, R-ISO-2);
* an archived project is read-only, so every mutating endpoint refuses it with
  the same machine-readable answer (AC-37).
"""

# Module imports
from plane.db.models import Project
from plane.utils.file_storage.errors import ProjectFileError


def project_or_404(slug, project_id):
    """Resolve the project from the URL.

    Soft-deleted projects are invisible to the default manager, so a deleted
    project's files are inaccessible without any extra check (AC-37); the
    resulting ``DoesNotExist`` is rendered as the shared 404 body.
    """
    return Project.objects.get(id=project_id, workspace__slug=slug)


def require_writable_project(project):
    """Refuse a mutation on an archived project (AC-37: archived is read-only)."""
    if project.archived_at is not None:
        raise ProjectFileError(
            "This project is archived, so its files are read-only.",
            code="project_archived",
            status_code=409,
        )
