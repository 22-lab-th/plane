# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

from plane.license.models import SSOAuditEvent
from plane.utils.exception_logger import log_exception
from plane.utils.ip_address import get_client_ip


def get_sso_correlation_id(request):
    correlation_id = getattr(request, "sso_correlation_id", None) or request.META.get("HTTP_X_REQUEST_ID")
    if not correlation_id:
        correlation_id = str(uuid.uuid4())
    request.sso_correlation_id = str(correlation_id)[:100]
    return request.sso_correlation_id


def record_sso_event(request, event, outcome, provider=None, actor=None, metadata=None):
    """Write a bounded audit event without credentials, tokens, or raw claims."""

    safe_metadata = {
        str(key)[:100]: str(value)[:500]
        for key, value in (metadata or {}).items()
        if key not in {"token", "id_token", "access_token", "refresh_token", "client_secret"}
    }
    correlation_id = get_sso_correlation_id(request)
    try:
        return SSOAuditEvent.objects.create(
            provider=provider,
            actor=actor if actor and actor.is_authenticated else None,
            event=event[:100],
            outcome=outcome[:20],
            correlation_id=correlation_id,
            ip_address=get_client_ip(request=request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:2000],
            metadata=safe_metadata,
        )
    except Exception as exc:
        log_exception(exc)
        return None
