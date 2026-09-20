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

It is also the **one resolver** for "can this row be seen, and can it be served"
(ADV-001 §5 P-1): ``file_queryset``/``trashed_files`` decide which rows exist for
a caller, ``delivery_refusal`` decides whether a row may be served, and
``permissions_for`` reports both answers instead of computing its own. The list,
the detail endpoint, the ``permissions`` block and the two delivery endpoints all
read these three functions, so they cannot drift apart again - which is exactly
what went wrong when the list showed a row that detail 404ed, or when
``can_download`` advertised a file every delivery endpoint refused.
"""

# Python imports
import uuid
from datetime import datetime, time

# Django imports
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

# Module imports
from plane.app.permissions import ROLE
from plane.db.models import FileFolder, FileObject, FileVersion, Project, ProjectMember
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.global_paginator import PaginateCursor
from plane.utils.file_storage.verdicts import GOOD_VERSION_STATUSES, delivery_refusal, is_on_default_surface
from plane.utils.file_storage.naming import extension_of, normalize_name
from plane.utils.path_validator import sanitize_filename

#: Version states whose object exists and was verified, so it can be served.
GOOD_VERSION_STATUSES = (FileVersion.Status.ACTIVE, FileVersion.Status.SUPERSEDED)

#: Guard against a folder cycle or a corrupted tree while walking breadcrumbs.
MAX_BREADCRUMB_DEPTH = 64

#: Upper bound on the " (n)" suffixes tried when a name is already taken.
MAX_NAME_ATTEMPTS = 200


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


def can_write(request, project):
    """Return True when the caller may mutate files in this project.

    The same membership, role and archived-project rules ``require_project_editor``
    enforces, expressed as a predicate so the ``permissions`` block a response
    advertises is derived from the answer the mutating endpoints would give
    (AC-37; a GUEST is a reader, and an archived project is read-only for all).
    """
    role = member_role(request, project)

    return role in (ROLE.ADMIN.value, ROLE.MEMBER.value) and project.archived_at is None


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


def require_project_admin(request, project):
    """Require the project ADMIN role - the only role that may purge (ARCH-001 §4.5).

    Membership first (a non-member learns nothing), then the role, then the project
    state, mirroring :func:`require_project_editor`.
    """
    role = member_role(request, project)
    if role is None:
        raise ObjectDoesNotExist("The required object does not exist.")

    if role != ROLE.ADMIN.value:
        raise ProjectFileError(
            "You don't have the required permissions.",
            code="permission_denied",
            status_code=403,
        )

    require_writable_project(project)


#: Page sizes shared by every file listing, so a caller cannot ask for more.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def parse_int(raw, field, *, minimum=0):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        invalid_param(field, f"{field} must be a whole number.")

    if value < minimum:
        invalid_param(field, f"{field} must be at least {minimum}.")

    return value


    parsed = parse_datetime(value)
    if parsed is None:
        date_value = parse_date(value)
        if date_value is None:
            invalid_param(field, f"{field} must be an ISO date or datetime.")

        parsed = datetime.combine(date_value, time.max if end_of_day else time.min)

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)

    return parsed


def parse_uuid(raw, field):
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        invalid_param(field, f"{field} must be a UUID.")


def page_size(request):
    raw = (request.GET.get("page_size") or "").strip()
    if not raw:
        return DEFAULT_PAGE_SIZE

    return min(max(parse_int(raw, "page_size", minimum=1), 1), MAX_PAGE_SIZE)


def cursor_token(request):
    """Return a validated cursor string, clamping the page size it carries.

    The cursor is client-supplied, so it is parsed and rebuilt rather than passed
    through: an unparseable value is a 400 and a zero or oversized page size
    cannot reach the paginator.
    """
    raw = (request.GET.get("cursor") or "").strip()
    if not raw:
        return str(PaginateCursor(page_size(request), 0, 0))

    try:
        parsed = PaginateCursor.from_string(raw)
    except (ValueError, TypeError):
        raise ProjectFileError(
            "cursor is not a valid pagination cursor.",
            code="invalid_request",
            field="cursor",
        )

    return str(
        PaginateCursor(
            min(max(parsed.current_page_size, 1), MAX_PAGE_SIZE),
            max(parsed.current_page, 0),
            0,
        )
    )


def invalid_param(field, message):
    """Refuse a query parameter with the shared 400 and the field it named."""
    raise ProjectFileError(message, code="invalid_request", field=field)


def parse_time_bound(raw, field, *, end_of_day):
    """Parse a date or datetime bound; a bare date covers the whole day.

    ``end_of_day`` decides whether a bare date means 00:00:00 or 23:59:59.999999,
    so ``created_to=2026-09-20`` includes that whole day. A naive value is made
    aware in the project timezone before it reaches the query.
    """
    value = raw.strip()
    if not value:
        return None

    # A bare date is handled first on purpose: Django's ``parse_datetime`` accepts
    # "2026-09-20" (it leans on ``datetime.fromisoformat``) and returns midnight, so
    # testing for a datetime first would quietly make every date-only bound mean
    # 00:00 and ``created_to=2026-09-20`` would exclude the day it names.
    bare_date = "T" not in value and " " not in value
    parsed = None if bare_date else parse_datetime(value)
    if parsed is None:
        date_value = parse_date(value)
        if date_value is None:
            invalid_param(field, f"{field} must be an ISO date or datetime.")
        parsed = datetime.combine(date_value, time.max if end_of_day else time.min)

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())

    return parsed


def parse_bool(raw, field):
    """Parse a query/body boolean, refusing anything else with a 400."""
    value = str(raw).strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False

    raise ProjectFileError(f"{field} must be true or false.", code="invalid_request", field=field)


def folder_or_400(project, folder_id):
    """Resolve an optional destination folder, refusing honestly when it is gone.

    A folder that never existed (or belongs to another project) is a payload
    error, and a soft-deleted one is a state conflict, so the two never share the
    misleading "does not belong to this project" message (T-105 F-4).
    """
    if folder_id in (None, "", "null"):
        return None

    folder = FileFolder.all_objects.filter(id=folder_id, project_id=project.id).first()
    if folder is None:
        raise ProjectFileError(
            "folder_id does not belong to this project.",
            code="folder_not_found",
            status_code=400,
            field="folder_id",
        )

    if folder.deleted_at is not None:
        raise ProjectFileError(
            "This folder was deleted; restore it or choose another folder.",
            code="folder_trashed",
            status_code=409,
            field="folder_id",
        )

    return folder


def stored_name(file_name):
    """Return the name stored for display: the uploader's name without path tricks."""
    return sanitize_filename(file_name) or file_name


def split_extension(name):
    """Return ``(stem, ".ext")`` for a name, or ``(name, "")`` without an extension."""
    stem, dot, extension = name.rpartition(".")
    if not dot or not extension_of(name):
        return name, ""

    return stem, f".{extension}"


def available_display_name(project, folder, file_name):
    """Suffix the display name until it is free in this folder (R-FOLD-5).

    ``file_objects`` is unique per project and folder on the normalised live name,
    so a second file called ``Report.pdf`` in the same folder becomes
    ``Report (2).pdf`` rather than failing or overwriting anything. Names that the
    user typed for an existing file (a rename) are refused instead - see the
    rename path - because there the user is making a choice, not declaring a new
    upload.
    """
    stem, extension = split_extension(file_name)
    candidate = file_name

    for index in range(2, MAX_NAME_ATTEMPTS):
        taken = (
            FileObject.objects.filter(
                project_id=project.id,
                folder_id=folder.id if folder is not None else None,
                name_normalized=normalize_name(candidate),
            )
            .exclude(status=FileObject.Status.TRASHED)
            .exists()
        )
        if not taken:
            return candidate

        candidate = f"{stem} ({index}){extension}"

    raise ProjectFileError(
        "A unique name could not be derived for this file.",
        code="name_conflict",
        status_code=409,
    )


def file_queryset(project, slug, *, include_trashed):
    """The one visibility source for project files.

    ``include_trashed=False`` is the default surface: the rows the live manager
    returns (``deleted_at IS NULL``) **and** whose status is not ``trashed``. Both
    halves matter - trashing sets the two together (R-FOLD-4), and a row where
    they disagree must be hidden by every path rather than listed by one and
    refused by another.

    ``include_trashed=True`` adds the soft-deleted rows back, so the trash
    surface and an explicit ``?trashed=true`` lookup can address what the default
    surface hides. Deleted rows are read through ``all_objects``; a row that a
    purge removed for good is absent from both.
    """
    manager = FileObject.all_objects if include_trashed else FileObject.objects
    queryset = manager.filter(project_id=project.id, workspace__slug=slug)

    if not include_trashed:
        queryset = queryset.exclude(status=FileObject.Status.TRASHED)

    return queryset


#: The statuses the trash surface shows and ``restore``/``purge`` accept. A file
#: whose purge failed stays here deliberately: it still holds its objects and its
#: quota, so hiding it would drop a failed deletion out of sight (R3-02).
TRASHED_STATUSES = (FileObject.Status.TRASHED, FileObject.Status.PURGE_FAILED)


def trashed_files(project, slug):
    """The trash surface: the rows that are in the trash, and only those."""
    return file_queryset(project, slug, include_trashed=True).filter(status__in=TRASHED_STATUSES)


def include_trashed(request, project):
    """True when the caller may address rows the default surface hides.

    ``?trashed=true`` asks for the trash explicitly, and a project ADMIN may address
    those rows without the flag. One function, so the detail endpoint and the version
    history cannot answer the question differently (ADV-001 §5 P-1).
    """
    raw = request.query_params.get("trashed")
    flagged = parse_bool(raw, "trashed") if raw not in (None, "") else False

    return flagged or member_role(request, project) == ROLE.ADMIN.value


def file_for_write(project, slug, file_id):
    """Resolve a file for a mutation with the visibility verdict the read paths use.

    A trashed row is resolved and reported as such - the documented 409
    ``file_trashed`` a caller can act on by restoring it (T-105 F-6) - while a row
    the default surface hides for any other reason is treated as absent, so no
    mutation can touch a row the listing does not show and the detail endpoint
    404s (T-106 verification F-2).
    """
    file_object = file_queryset(project, slug, include_trashed=True).get(id=file_id)

    if file_object.status == FileObject.Status.TRASHED:
        raise ProjectFileError(
            "This file is in the trash; restore it before changing it.",
            code="file_trashed",
            status_code=409,
        )

    if not is_on_default_surface(file_object):
        raise ObjectDoesNotExist("The required object does not exist.")

    return file_object


def permissions_for(request, project, file_object, *, version=None):
    """Return the caller's affordances for this file, from the shared predicates.

    ``can_download`` is true exactly when ``delivery_refusal`` would let the
    delivery endpoints sign: a trashed, quarantined or object-less version
    reports false here *and* is refused there, so the advertised affordance and
    the real answer cannot drift apart (ADV-001 §4 P-1).

    ``can_edit``/``can_delete`` come from ``can_write``: a GUEST may list and read
    but never mutate, and an archived project is read-only for everyone. A file
    that is already trashed has no edit affordance because it is restored rather
    than edited, while deleting it again stays allowed.
    """
    write_allowed = can_write(request, project)

    return {
        "can_edit": write_allowed and file_object.status != FileObject.Status.TRASHED,
        "can_delete": write_allowed,
        "can_download": delivery_refusal(file_object, version) is None,
    }


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
