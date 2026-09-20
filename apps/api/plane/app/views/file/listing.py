# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project file listing and detail (ARCH-001 §4.1).

Both endpoints are read-only and database-backed: no listing path calls the
object store, and the storage summary comes from the counter rows rather than
from the bucket (R-NFR-1, AD-02). Every query is scoped by the workspace slug
**and** the project id from the URL, so a file that belongs to another project is
not found rather than forbidden (AD-06).
"""

# Python imports
import uuid
from datetime import datetime, time

# Django imports
from django.db.models import Count, Exists, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, allow_permission
from plane.app.serializers.file import (
    FileFolderSerializer,
    FileLinkSerializer,
    FileObjectSerializer,
    FileVersionSerializer,
)
from plane.app.views.base import BaseAPIView
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.app.views.file.base import (
    breadcrumbs,
    file_queryset,
    include_trashed,
    parse_bool,
    permissions_for,
    project_or_404,
    trashed_files,
)
from plane.db.models import FileFolder, FileLink, FileObject, FileVersion
from plane.utils.file_storage import quota
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.global_paginator import PaginateCursor, paginate
from plane.utils.magic_bytes import normalize_mime_type

#: Sort keys the clients may ask for; every key ends with ``id`` so paging is
#: stable when many rows share the sort value (R-FIND-2).
ORDERINGS = {
    "name": ("name_normalized", "id"),
    "-name": ("-name_normalized", "-id"),
    "size": ("size_bytes", "id"),
    "-size": ("-size_bytes", "-id"),
    "created": ("created_at", "id"),
    "-created": ("-created_at", "-id"),
    "updated": ("updated_at", "id"),
    "-updated": ("-updated_at", "-id"),
}
DEFAULT_ORDERING = "-created"

#: The folder browsing anchor that means "the project root".
ROOT_FOLDER = "root"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

def _invalid(field, message):
    raise ProjectFileError(message, code="invalid_request", field=field)


def link_count_expression():
    """Count a file's links in its own subquery.

    An aggregate over the listing's join would only see the links the entity
    filter selected, so a file with two links would report one whenever
    ``entity_type`` or ``entity_id`` was supplied. Counting in a dedicated
    subquery keeps ``link_count`` the same filter or no filter (index-backed on
    ``file_links.file_id``).
    """
    return Coalesce(
        Subquery(
            FileLink.objects.filter(file_id=OuterRef("pk"))
            .order_by()
            .values("file_id")
            .annotate(total=Count("id"))
            .values("total"),
            output_field=IntegerField(),
        ),
        Value(0),
        output_field=IntegerField(),
    )


def _parse_int(raw, field, *, minimum=0):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _invalid(field, f"{field} must be a whole number.")

    if value < minimum:
        _invalid(field, f"{field} must be at least {minimum}.")

    return value


def _parse_bound(raw, field, *, end_of_day):
    """Parse a date or datetime bound; a bare date covers the whole day."""
    value = raw.strip()
    if not value:
        return None

    parsed = parse_datetime(value)
    if parsed is None:
        date_value = parse_date(value)
        if date_value is None:
            _invalid(field, f"{field} must be an ISO date or datetime.")

        parsed = datetime.combine(date_value, time.max if end_of_day else time.min)

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)

    return parsed


def _parse_uuid(raw, field):
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        _invalid(field, f"{field} must be a UUID.")


def parse_filters(request):
    """Validate the list query parameters into a filter description."""
    query = request.GET
    filters = {
        "folder": None,
        "q": None,
        "mime": None,
        "extension": None,
        "uploader_id": None,
        "uploader_text": None,
        "created_from": None,
        "created_to": None,
        "size_min": None,
        "size_max": None,
        "entity_type": None,
        "entity_id": None,
        "pinned": None,
        "trashed": False,
        "ordering": DEFAULT_ORDERING,
    }

    raw_folder = (query.get("folder_id") or "").strip()
    if raw_folder:
        filters["folder"] = ROOT_FOLDER if raw_folder.lower() == ROOT_FOLDER else _parse_uuid(raw_folder, "folder_id")

    if raw_q := (query.get("q") or "").strip():
        filters["q"] = raw_q

    if raw_mime := (query.get("mime") or "").strip():
        filters["mime"] = normalize_mime_type(raw_mime)

    if raw_extension := (query.get("extension") or "").strip().lstrip(".").lower():
        filters["extension"] = raw_extension

    if raw_uploader := (query.get("uploader") or "").strip():
        try:
            filters["uploader_id"] = uuid.UUID(raw_uploader)
        except (TypeError, ValueError, AttributeError):
            filters["uploader_text"] = raw_uploader

    if raw_from := query.get("created_from"):
        filters["created_from"] = _parse_bound(raw_from, "created_from", end_of_day=False)

    if raw_to := query.get("created_to"):
        filters["created_to"] = _parse_bound(raw_to, "created_to", end_of_day=True)

    if (raw_size_min := query.get("size_min")) is not None and raw_size_min != "":
        filters["size_min"] = _parse_int(raw_size_min, "size_min")

    if (raw_size_max := query.get("size_max")) is not None and raw_size_max != "":
        filters["size_max"] = _parse_int(raw_size_max, "size_max")

    if raw_entity_type := (query.get("entity_type") or "").strip():
        if raw_entity_type not in FileLink.EntityType.values:
            _invalid("entity_type", "entity_type must be one of the supported entity types.")
        filters["entity_type"] = raw_entity_type

    if raw_entity_id := (query.get("entity_id") or "").strip():
        filters["entity_id"] = _parse_uuid(raw_entity_id, "entity_id")

    if query.get("pinned") is not None and query.get("pinned") != "":
        filters["pinned"] = parse_bool(query.get("pinned"), "pinned")

    if query.get("trashed") is not None and query.get("trashed") != "":
        filters["trashed"] = parse_bool(query.get("trashed"), "trashed")

    ordering = (query.get("ordering") or "").strip()
    if ordering:
        if ordering not in ORDERINGS:
            _invalid("ordering", f"ordering must be one of: {', '.join(sorted(ORDERINGS))}.")
        filters["ordering"] = ordering

    return filters


def _files_queryset(project, slug, filters):
    """Build the filtered, ordered, index-friendly file queryset.

    Every filter is conjunctive and the trash view is opt-in: without
    ``trashed=true`` the trashed files are excluded (R-FIND-1).

    Visibility comes from the shared resolver (``file_queryset``/``trashed_files``)
    rather than from a manager chosen here, so the rows this list shows are
    exactly the rows the detail endpoint will resolve (ADV-001 §5 P-1).
    """
    queryset = (
        trashed_files(project, slug) if filters["trashed"] else file_queryset(project, slug, include_trashed=False)
    )

    if filters["folder"] == ROOT_FOLDER:
        queryset = queryset.filter(folder__isnull=True)
    elif filters["folder"] is not None:
        queryset = queryset.filter(folder_id=filters["folder"])

    if filters["q"]:
        queryset = queryset.filter(name_display__icontains=filters["q"])

    if filters["mime"]:
        queryset = queryset.filter(mime_type=filters["mime"])

    if filters["extension"]:
        queryset = queryset.filter(extension=filters["extension"])

    if filters["uploader_id"] is not None:
        queryset = queryset.filter(created_by_id=filters["uploader_id"])
    elif filters["uploader_text"]:
        queryset = queryset.filter(created_by__email__icontains=filters["uploader_text"])

    if filters["created_from"] is not None:
        queryset = queryset.filter(created_at__gte=filters["created_from"])

    if filters["created_to"] is not None:
        queryset = queryset.filter(created_at__lte=filters["created_to"])

    if filters["size_min"] is not None:
        queryset = queryset.filter(size_bytes__gte=filters["size_min"])

    if filters["size_max"] is not None:
        queryset = queryset.filter(size_bytes__lte=filters["size_max"])

    if filters["entity_type"] or filters["entity_id"]:
        # An ``Exists`` over the same live manager ``link_count`` counts with: a join
        # would also match an *unlinked* row (the link is soft-deleted, not removed),
        # so the filtered listing would show a file whose own count reads 0 - the
        # two-answers-for-one-question shape the filter and the count must not have.
        entity_links = FileLink.objects.filter(file_id=OuterRef("pk"))
        if filters["entity_type"]:
            entity_links = entity_links.filter(entity_type=filters["entity_type"])
        if filters["entity_id"]:
            entity_links = entity_links.filter(entity_id=filters["entity_id"])
        queryset = queryset.filter(Exists(entity_links))

    if filters["pinned"] is not None:
        queryset = queryset.filter(is_pinned=filters["pinned"])

    return (
        queryset.annotate(link_count=link_count_expression())
        .select_related("created_by", "folder")
        .order_by(*ORDERINGS[filters["ordering"]])
    )


def _current_folder(project, filters):
    """Return the folder the caller is browsing, or ``None`` for the root."""
    if filters["folder"] in (None, ROOT_FOLDER):
        return None

    return FileFolder.objects.filter(id=filters["folder"], project_id=project.id).first()


def _folders_for(project, folder):
    """Return the child folders of the browsed folder, ordered by name."""
    parent_id = folder.id if folder is not None else None

    return FileFolder.objects.filter(project_id=project.id, parent_id=parent_id).order_by("name_normalized", "id")


def storage_summary(project):
    """Return the storage block of the list response (ARCH-001 §4.1, §2.8).

    The counts include trashed files on purpose: what the bucket stores is what
    the counters and the counts report, and trashed files keep consuming quota
    until they are purged (AD-09). That is also why the count reads through the
    all-objects manager: trashing sets ``deleted_at`` as well as the status.
    """
    quota_row, usage_row = quota.get_usage_rows(project)
    version_count = FileVersion.objects.filter(file__project_id=project.id).count()

    return {
        "project_used_bytes": usage_row.used_bytes,
        "workspace_used_bytes": quota_row.used_bytes,
        # The effective ceiling for this project: its own limit when it has one,
        # otherwise the workspace ceiling.
        "limit_bytes": usage_row.limit_bytes if usage_row.limit_bytes is not None else quota_row.limit_bytes,
        "warn_threshold_pct": quota_row.warn_threshold_pct,
        "file_count": FileObject.all_objects.filter(project_id=project.id).count(),
        "version_count": version_count,
    }


def _page_size(request):
    raw = (request.GET.get("page_size") or "").strip()
    if not raw:
        return DEFAULT_PAGE_SIZE

    return min(max(_parse_int(raw, "page_size", minimum=1), 1), MAX_PAGE_SIZE)


def _cursor(request):
    """Return a validated cursor string, clamping the page size it carries.

    The cursor is client-supplied, so it is parsed and rebuilt rather than passed
    through: an unparseable value is a 400 and a zero or oversized page size
    cannot reach the paginator.
    """
    raw = (request.GET.get("cursor") or "").strip()
    if not raw:
        return str(PaginateCursor(_page_size(request), 0, 0))

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


class FileListEndpoint(BaseAPIView):
    """List the files and folders of a project, with the documented filters.

    Pagination reuses the repository's cursor token, which is an **offset** token
    (``page_size:page:offset``), not a keyset: a file inserted or trashed between
    two page requests can therefore shift a row across the page boundary. That is
    the contract R-FIND-2 permits ("cursor or page pagination consistent with
    existing Plane list endpoints") and the ordering is still stable for a fixed
    data set; the trash and restore tickets must keep that in mind when they
    mutate rows a client may be paging through.

    ``trashed`` is opt-in, so the default view never shows trashed files, and the
    storage block counts them anyway because trashed files consume quota until
    they are purged (AD-09).

    Reading this endpoint may create the project's ``storage_quotas`` and
    ``project_storage_usage`` rows: they are materialised lazily on first use
    (ARCH-001 §2.8, N-02a) so the ceiling always has a row to lock, and a row
    that was soft-deleted is revived rather than duplicated.
    """

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST])
    def get(self, request, slug, project_id):
        project = project_or_404(slug, project_id)
        filters = parse_filters(request)
        folder = _current_folder(project, filters)

        if filters["folder"] not in (None, ROOT_FOLDER) and folder is None:
            raise FileFolder.DoesNotExist

        queryset = _files_queryset(project, slug, filters)
        page = paginate(
            queryset,
            queryset,
            _cursor(request),
            on_result=lambda rows: FileObjectSerializer(rows, many=True).data,
        )

        return Response(
            {
                "results": page["results"],
                "folders": FileFolderSerializer(_folders_for(project, folder), many=True).data,
                "breadcrumbs": breadcrumbs(project, folder),
                "page": {
                    "next_cursor": page["next_cursor"],
                    "prev_cursor": page["prev_cursor"],
                    "cursor": page["cursor"],
                    "page_count": page["page_count"],
                    "total_results": page["total_results"],
                    "total_pages": page["total_pages"],
                    "next_page_results": page["next_page_results"],
                    "prev_page_results": page["prev_page_results"],
                },
                "storage": storage_summary(project),
            },
            status=status.HTTP_200_OK,
        )


class FileDetailEndpoint(BaseAPIView):
    """Read, rename, move, pin or trash one file (GET T-103, PATCH T-106, DELETE T-107).

    A live-only lookup is the default, so a trashed file is not found; the caller
    may address the trash explicitly with ``?trashed=true`` (the same flag the
    listing uses, so a UI holding a trashed file id can open it to restore it),
    and a project ADMIN may address trashed rows without the flag. Either way the
    payload carries a ``trashed`` marker.
    """

    def get_throttles(self):
        """Mutations carry the project-file throttle; reads keep the defaults."""
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            return [ProjectFileUploadThrottle()]
        return super().get_throttles()

    def patch(self, request, slug, project_id, file_id):
        """Rename, move or pin the file; the work itself lives in operations.py."""
        from plane.app.views.file.operations import patch_file

        return patch_file(request, slug, project_id, file_id)

    def delete(self, request, slug, project_id, file_id):
        """Move the file to the trash (T-107); the work lives in operations.py."""
        from plane.app.views.file.operations import trash_file

        return trash_file(request, slug, project_id, file_id)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST])
    def get(self, request, slug, project_id, file_id):
        project = project_or_404(slug, project_id)

        # The same visibility source the list reads, scoped by project, so a file
        # belonging to another project is simply not found and the caller learns
        # nothing about it (AD-06).
        file_object = (
            file_queryset(project, slug, include_trashed=include_trashed(request, project))
            .annotate(link_count=link_count_expression())
            .select_related("created_by", "folder")
            .get(id=file_id)
        )

        versions = list(file_object.versions.order_by("-version_no").select_related("uploaded_by"))
        requested_version = (request.GET.get("version") or "").strip()
        if requested_version:
            version = next((item for item in versions if str(item.version_no) == requested_version), None)
            if version is None:
                raise FileVersion.DoesNotExist
        else:
            version = next((item for item in versions if item.is_active), None)

        links = file_object.links.order_by("-created_at")

        return Response(
            {
                "file": FileObjectSerializer(file_object).data,
                "version": FileVersionSerializer(version).data if version is not None else None,
                "versions": FileVersionSerializer(versions, many=True).data,
                "links": FileLinkSerializer(links, many=True).data,
                "link_count": file_object.link_count,
                "permissions": permissions_for(request, project, file_object, version=version),
            },
            status=status.HTTP_200_OK,
        )
