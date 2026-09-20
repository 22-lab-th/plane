# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework.throttling import SimpleRateThrottle


class ProjectFileUploadThrottle(SimpleRateThrottle):
    """Throttle presign/finalize per user and project (R-UPL-5, AC-22).

    Keyed by user **and** project rather than by asset id, so the bucket is the
    burst a single client is producing in one project: a burst in one project
    cannot starve another (R-UPL-5), and the caller cannot escape the limit by
    switching the file it is uploading.
    """

    scope = "project_file_upload"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None

        project_id = view.kwargs.get("project_id") if hasattr(view, "kwargs") else None
        if not project_id:
            return None

        return self.cache_format % {"scope": self.scope, "ident": f"{user.pk}:{project_id}"}
