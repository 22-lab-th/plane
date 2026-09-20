# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Append-only audit recording for project files (ARCH-001 §2.6, R-NFR-9).

Every mutation goes through :func:`record_file_access` so no endpoint can forget
the actor, IP address or user agent, and so no endpoint has a reason to write a
presigned URL into the audit trail (AD-15, R-AUD-2). Rows are never updated or
deleted here: the project-file audit skeleton outlives the files it describes.
"""

# Python imports
import ipaddress

# Module imports
from plane.db.models import FileAccessLog
from plane.utils.ip_address import get_client_ip


def _actor_display(user):
    """Return a readable actor snapshot that survives the user row being removed."""
    if user is None or not getattr(user, "is_authenticated", False):
        return ""

    return (user.display_name or user.email or user.username or "")[:255]


def _audit_ip_address(request):
    """Return the client address, or ``None`` when it is not a valid IP.

    ``X-Forwarded-For`` is client-controlled, and the audit column is an ``inet``
    column, so an unparseable value is dropped rather than trusted or crashed on.
    """
    if request is None:
        return None

    candidate = (get_client_ip(request=request) or "").strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def record_file_access(
    request,
    *,
    action,
    project,
    file_name="",
    file_id=None,
    version_no=None,
    metadata=None,
):
    """Append one audit row for a project-file action.

    :param action: a :class:`plane.db.models.FileAccessLog.Action` value.
    :param project: the project the action happened in; its workspace is stored
        denormalised so the row stays queryable after the project is deleted.
    :param metadata: extra context (folder, target entity, mismatch evidence).
        Never a presigned URL.
    """
    user_agent = (request.META.get("HTTP_USER_AGENT") or "") if request is not None else ""
    user = getattr(request, "user", None) if request is not None else None

    return FileAccessLog.objects.create(
        workspace_id=project.workspace_id,
        project=project,
        file_id=file_id,
        file_name_snapshot=(file_name or "")[:255],
        version_no=version_no,
        action=action,
        actor=user if user is not None and getattr(user, "is_authenticated", False) else None,
        actor_display=_actor_display(user),
        ip_address=_audit_ip_address(request),
        user_agent=user_agent[:512],
        metadata=metadata or {},
    )
