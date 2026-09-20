# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Helpers shared by the project-file endpoints.

Three rules live here so no endpoint can forget them:

* a project is always resolved from the workspace slug **and** the project id,
  so a project in another workspace never resolves (AD-06, R-ISO-2);
* an archived project is read-only, so every mutating endpoint refuses it with
  the same machine-readable answer (AC-37);
* breadcrumbs are walked from the database ``parent_id`` chain, never derived
  from an object key (R-FOLD-2, AD-02), and the walk is guarded against a cycle
  at read time as well as at write time.
"""

# Django imports
from django.core.exceptions import ObjectDoesNotExist

# Module imports
from plane.app.permissions import ROLE
from plane.db.models import FileFolder, Project, ProjectMember
from plane.utils.file_storage.errors import ProjectFileError

#: Guard against a folder cycle or a corrupted tree while walking breadcrumbs.
MAX_BREADCRUMB_DEPTH = 64


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


def member_role(request, project):
    """Return the caller's active project role value, or ``None``."""
    member = ProjectMember.objects.filter(project_id=project.id, member=request.user, is_active=True).first()
    return member.role if member is not None else None


def require_project_member(request, project):
    """Refuse a non-member with the generic 404 rather than a 403.

    A file- or folder-scoped endpoint must not confirm that a resource exists to
    somebody who cannot see the project, so the caller is answered exactly like a
    caller asking for a resource in a project it is not part of (AD-06). The
    neutral ``ObjectDoesNotExist`` is what makes the two 404 bodies identical.
    """
    if member_role(request, project) is None:
        raise ObjectDoesNotExist("The required object does not exist.")


def require_project_editor(request, project):
    """Require a writable project: member (404 otherwise), not a guest, not archived.

    Order matters: membership first (a non-member learns nothing), then the role
    (a GUEST is forbidden with the repository's standard 403 body), then the
    project state (an archived project is read-only for everyone, AC-37).
    """
    role = member_role(request, project)
    if role is None:
        raise ObjectDoesNotExist("The required object does not exist.")

    if role not in (ROLE.ADMIN.value, ROLE.MEMBER.value):
        raise ProjectFileError(
            "You don't have the required permissions.",
            code="permission_denied",
            status_code=403,
        )

    require_writable_project(project)


def parse_bool(raw, field):
    """Parse a query/body boolean, refusing anything else with a 400."""
    value = str(raw).strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False

    raise ProjectFileError(f"{field} must be true or false.", code="invalid_request", field=field)


def breadcrumbs(project, folder):
    """Return the path from the project root down to ``folder``, inclusive.

    The whole folder tree is read in one query and the walk is bounded, so a deep
    folder costs the same as a shallow one and a cycle in the data cannot spin
    (R-FOLD-2). The project root itself is implicit and is not included.
    """
    if folder is None:
        return []

    tree = {
        row["id"]: row
        for row in FileFolder.objects.filter(project_id=project.id).values("id", "name", "parent_id", "depth")
    }

    trail = []
    visited = set()
    current_id = folder.id
    for _ in range(MAX_BREADCRUMB_DEPTH):
        if current_id is None or current_id in visited:
            break
        node = tree.get(current_id)
        if node is None:
            break
        visited.add(current_id)
        trail.append({"id": str(node["id"]), "name": node["name"], "depth": node["depth"]})
        current_id = node["parent_id"]

    return list(reversed(trail))
