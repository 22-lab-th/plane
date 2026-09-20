# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The project's audit trail, read-only (ARCH-001 §2.6, §4.1, R-AUD-1/3).

Rows are written by the single :func:`plane.utils.file_storage.audit.record_file_access`
helper and never by hand, and nothing in the API can change or delete one: this module
only reads. The trail is append-only by construction - there is no update or delete
view for a row - and it survives the file it describes, because the file column is a
plain id and every purge path leaves ``file_access_logs`` alone (AD-08, AD-15).

Who may read it: the project-wide trail is the audit *export* the RBAC table reserves
for ADMIN, while a file's own history travels with that file (R-AUD-3) and is returned
by the detail endpoint to anyone who may read the file.

What is *not* recorded, said out loud: capacity events are audited (``quota_rejected``,
and ``upload_failed`` for an attempt that ended badly), while a refusal that changes
nothing - a 404, a duplicate link, a trashed file, a validation error - writes no row,
because the trail records what happened to the file's bytes rather than what a client
asked for and was told no. ``FileAccessLog.Action.PERMISSION_DENIED`` is consequently
unwritten today: every denial on this surface is answered by the project RBAC before a
row could name the file, so the value is reserved for the authorization layer that can
see the file, and dropping it would cost a migration for a value the next layer needs.
"""

# Python imports
import uuid

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.serializers.file import FileAccessLogSerializer
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    cursor_token,
    invalid_param,
    parse_time_bound,
    project_or_404,
    require_project_admin,
)
from plane.db.models import FileAccessLog
from plane.utils.global_paginator import paginate


def parse_activity_filters(request):
    """Validate the activity query parameters into a filter description."""
    query = request.query_params
    filters = {
        "file_id": None,
        "action": None,
        "actor_id": None,
        "actor_text": None,
        "created_from": None,
        "created_to": None,
    }

    if raw_file_id := query.get("file_id"):
        try:
            filters["file_id"] = uuid.UUID(raw_file_id)
        except (TypeError, ValueError):
            invalid_param("file_id", "file_id must be a UUID.")

    if raw_action := query.get("action"):
        if raw_action not in FileAccessLog.Action.values:
            invalid_param(
                "action", f"action must be one of: {', '.join(sorted(FileAccessLog.Action.values))}."
            )
        filters["action"] = raw_action

    if raw_actor := query.get("actor"):
        # An id when it parses as one, otherwise a substring of the display snapshot
        # (which is where the email lands for a user who has no display name).
        try:
            filters["actor_id"] = uuid.UUID(raw_actor)
        except (TypeError, ValueError):
            filters["actor_text"] = raw_actor

    if raw_from := query.get("created_from"):
        filters["created_from"] = parse_time_bound(raw_from, "created_from", end_of_day=False)
    if raw_to := query.get("created_to"):
        filters["created_to"] = parse_time_bound(raw_to, "created_to", end_of_day=True)

    return filters


def activity_queryset(project, filters):
    """The filtered, ordered audit rows for one project."""
    queryset = FileAccessLog.objects.filter(project_id=project.id).select_related("actor")

    if filters["file_id"] is not None:
        queryset = queryset.filter(file_id=filters["file_id"])
    if filters["action"] is not None:
        queryset = queryset.filter(action=filters["action"])
    if filters["actor_id"] is not None:
        queryset = queryset.filter(actor_id=filters["actor_id"])
    elif filters["actor_text"]:
        queryset = queryset.filter(actor_display__icontains=filters["actor_text"])
    if filters["created_from"] is not None:
        queryset = queryset.filter(created_at__gte=filters["created_from"])
    if filters["created_to"] is not None:
        queryset = queryset.filter(created_at__lte=filters["created_to"])

    return queryset.order_by("-created_at", "-id")


class FileActivityEndpoint(BaseAPIView):
    """Read the project's audit trail, newest first (ARCH-001 §4.1).

    No throttle: this endpoint neither signs a URL nor appends an audit row, which is
    the rule the delivery and mutation endpoints are held to.
    """

    def get(self, request, slug, project_id):
        project = project_or_404(slug, project_id)
        # The project-wide trail is the audit export the RBAC table keeps for ADMIN;
        # a non-member still gets the generic 404 and learns nothing.
        require_project_admin(request, project)

        queryset = activity_queryset(project, parse_activity_filters(request))
        page = paginate(
            queryset,
            queryset,
            cursor_token(request),
            on_result=lambda rows: FileAccessLogSerializer(rows, many=True).data,
        )

        return Response(
            {
                "results": page["results"],
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
            },
            status=status.HTTP_200_OK,
        )
