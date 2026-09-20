# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Name and extension derivation for project files."""

# Python imports
import re

_EXTENSION = re.compile(r"\A[a-z0-9]{1,32}\Z")


def normalize_name(name):
    """Return the case-folded, whitespace-collapsed form used for unique names."""
    return " ".join((name or "").split()).casefold()


def extension_of(name):
    """Return the lowercase alphanumeric extension of ``name``, or ``""``.

    The extension is derived server-side from the stored name, never taken from
    the request, and only a plausible extension survives (DEC-001 keeps the same
    rule inside the object-key builder).
    """
    _, dot, extension = (name or "").rpartition(".")
    if not dot:
        return ""

    extension = extension.strip().lower()
    return extension if _EXTENSION.match(extension) else ""
