# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Server-side object-key construction for project files (DEC-001).

Every function in this module is pure: no database access, no settings lookup and
no storage client. The canonical key pattern is

    workspace/{workspaceSlug}/projects/{projectStorageKey}/{category}/{entityRef}/{fileId}/v{version}/{sanitizedFilename}

with the ``entityRef`` segment omitted for project-level files that have no
entity binding. ``{fileId}``, ``v{version}`` and ``{sanitizedFilename}`` are
always present, so a key is unique per file and version (AD-04, AD-13).

The client never supplies a key segment: the file id and version come from the
database, the category from the server-side allowlist, and the filename is
sanitised by :func:`sanitize_key_segment` before it reaches the key.
"""

# Python imports
import re
import unicodedata

#: Categories allowed in the ``{category}`` key segment (DEC-001 taxonomy).
OBJECT_KEY_CATEGORIES: frozenset = frozenset(
    [
        "_system",
        "docs",
        "issues",
        "pages",
        "deliverables",
        "assets",
        "imports",
        "exports",
        "archive",
    ]
)

#: R2 normalises keys to NFC and rejects keys longer than 1024 bytes (RSCH-001).
MAX_OBJECT_KEY_BYTES = 1024

#: Longest filename segment kept in a key, extension included.
MAX_SEGMENT_CHARS = 120

#: Longest extension retained when a filename segment is truncated.
MAX_EXTENSION_CHARS = 32

#: ``Project.storage_key`` length, matching the model column.
MAX_PROJECT_STORAGE_KEY_CHARS = 96

#: Upper bound on collision suffixes tried before giving up.
MAX_PROJECT_STORAGE_KEY_ATTEMPTS = 1000

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SEGMENT_DISALLOWED = re.compile(r"[^a-z0-9._-]")
_SEGMENT_DISALLOWED_ANY_CASE = re.compile(r"[^A-Za-z0-9._-]")
_SEGMENT_RUN = re.compile(r"[.-]{2,}")
_PROJECT_KEY_DISALLOWED = re.compile(r"[^A-Za-z0-9]+")
_EDGE_CHARS = ".-"


def _sanitize_segment(value, *, lowercase):
    """Reduce ``value`` to the characters an object-key segment may contain."""
    if not isinstance(value, str) or not value:
        return ""

    segment = unicodedata.normalize("NFC", value)
    if lowercase:
        segment = segment.lower()

    # Control characters (including NUL) are removed rather than replaced, so
    # they can never forge a separator, and separators are mapped to "-" by the
    # allowlist pass below instead of being collapsed away.
    segment = _CONTROL_CHARS.sub("", segment)
    pattern = _SEGMENT_DISALLOWED if lowercase else _SEGMENT_DISALLOWED_ANY_CASE
    segment = pattern.sub("-", segment)
    # Runs of dots and dashes collapse to one dash, which also destroys "..",
    # ".-." and "%2e%2e%2f" style traversal fragments.
    segment = _SEGMENT_RUN.sub("-", segment)
    return segment.strip(_EDGE_CHARS)


def _cap_segment(segment):
    """Cap a filename segment at :data:`MAX_SEGMENT_CHARS`, keeping its extension."""
    if len(segment) <= MAX_SEGMENT_CHARS:
        return segment

    stem, dot, extension = segment.rpartition(".")
    if dot and 0 < len(extension) <= MAX_EXTENSION_CHARS:
        stem = stem[: MAX_SEGMENT_CHARS - len(extension) - 1].strip(_EDGE_CHARS)
        if stem:
            return f"{stem}.{extension}"

    return segment[:MAX_SEGMENT_CHARS].strip(_EDGE_CHARS)


def sanitize_key_segment(name):
    """Return ``name`` as a safe, lowercased, single object-key segment.

    * NFC-normalised and lowercased;
    * path separators, control characters and every other character outside
      ``[a-z0-9._-]`` are replaced with ``-``;
    * runs of dots and dashes collapse to a single ``-`` and leading/trailing
      dots and dashes are trimmed, so ``.``, ``..``, ``../``, ``..\\`` and
      percent-encoded equivalents normalise to ``""``;
    * capped at :data:`MAX_SEGMENT_CHARS` characters, retaining the extension.

    An empty return value means the caller must fall back to a server-generated
    identifier; it never means "use the raw input".
    """
    return _cap_segment(_sanitize_segment(name, lowercase=True))


def _sanitize_entity_ref(entity_ref):
    """Sanitise ``entity_ref`` without changing its case (issue keys are uppercase).

    ``entity_ref`` is a single key segment. Callers pass the human issue key
    (``CBUTR-11``) or the immutable page UUID; a value that carries a path
    separator is folded into one segment instead of escaping the prefix.
    """
    return _cap_segment(_sanitize_segment(entity_ref, lowercase=False))


def _require_key_segment(value, label):
    """Validate a server-provided segment that is embedded verbatim in a key."""
    if value is None:
        raise ValueError(f"{label} must not be None")

    segment = str(value)
    if (
        not segment
        or not segment.isascii()
        or segment in {".", ".."}
        or ".." in segment
        or "/" in segment
        or "\\" in segment
        or any(character.isspace() for character in segment)
        or _CONTROL_CHARS.search(segment)
    ):
        raise ValueError(f"{label} is not a usable object-key segment: {value!r}")

    return segment


def _version_segment(version_no):
    """Validate ``version_no`` and render it as the ``v{n}`` key segment."""
    try:
        version = int(version_no)
    except (TypeError, ValueError):
        raise ValueError(f"version_no must be an integer: {version_no!r}")

    if version < 1:
        raise ValueError(f"version_no must be positive: {version_no!r}")

    return f"v{version}"


def build_object_key(
    workspace_slug,
    project_storage_key,
    category,
    file_id,
    version_no,
    filename,
    entity_ref=None,
):
    """Build the canonical object key for one file version.

    :param workspace_slug: ``Workspace.slug`` at write time (kept verbatim; a
        later slug rename only affects new objects).
    :param project_storage_key: immutable ``Project.storage_key``.
    :param category: one of :data:`OBJECT_KEY_CATEGORIES`; anything else raises
        :class:`ValueError`.
    :param file_id: ``FileObject.id``; never client-supplied.
    :param version_no: 1-based ``FileVersion.version_no``.
    :param filename: original upload filename; sanitised, never trusted.
    :param entity_ref: optional single segment binding the file to an entity
        (for example ``CBUTR-11`` or a page UUID). Omitted when ``None`` or when
        nothing safe survives sanitisation.
    :raises ValueError: for an unknown category or an unusable server segment.
    """
    if category not in OBJECT_KEY_CATEGORIES:
        raise ValueError(f"unknown file category: {category!r}")

    workspace_segment = _require_key_segment(workspace_slug, "workspace_slug")
    project_segment = _require_key_segment(project_storage_key, "project_storage_key")
    file_segment = sanitize_key_segment(str(file_id))
    if not file_segment:
        raise ValueError(f"file_id is not a usable object-key segment: {file_id!r}")

    version_segment = _version_segment(version_no)
    # A filename that sanitises to nothing (".", "..", "///", control-only, ...)
    # falls back to the immutable file id, never to the raw client input.
    filename_segment = sanitize_key_segment(filename) or file_segment
    entity_segment = _sanitize_entity_ref(entity_ref) if entity_ref is not None else ""

    segments = ["workspace", workspace_segment, "projects", project_segment, category]
    if entity_segment:
        segments.append(entity_segment)
    segments.extend([file_segment, version_segment, filename_segment])

    key = "/".join(segments)
    if len(key.encode("utf-8")) > MAX_OBJECT_KEY_BYTES:
        raise ValueError(f"object key exceeds {MAX_OBJECT_KEY_BYTES} bytes: {len(key)} characters")

    return key


def slugify_project_name(name):
    """Slugify a project name for the ``Project.storage_key`` prefix.

    Lowercased, with every run of characters outside ``[A-Za-z0-9]`` collapsed
    to ``-`` and the edges trimmed. Names without ASCII alphanumerics (for
    example a fully Thai name) slugify to ``""``, leaving the identifier alone
    as the prefix.
    """
    normalized = unicodedata.normalize("NFC", name if isinstance(name, str) else "")
    return _PROJECT_KEY_DISALLOWED.sub("-", normalized.lower()).strip("-")


def build_project_storage_key(identifier, name, taken_keys=()):
    """Derive the immutable ``Project.storage_key``: ``{identifier}-{slug(name)}``.

    The identifier keeps its case (``CBUTR``) and the slugified name is
    lowercase, matching the documented example
    ``CBUTR-smart-cbu-tracking-system``. The result is truncated so that the
    whole key — suffix included — fits :data:`MAX_PROJECT_STORAGE_KEY_CHARS`, and
    collisions are resolved with ``-2``, ``-3``, … suffixes.

    :param taken_keys: keys already used in the same workspace; the caller owns
        the database lookup so this function stays pure.
    """
    prefix = _PROJECT_KEY_DISALLOWED.sub("-", unicodedata.normalize("NFC", str(identifier or ""))).strip("-")
    if not prefix:
        prefix = "project"

    slug = slugify_project_name(name)
    base = "-".join(part for part in (prefix, slug) if part)
    taken = {key for key in taken_keys if key}

    for index in range(1, MAX_PROJECT_STORAGE_KEY_ATTEMPTS):
        suffix = "" if index == 1 else f"-{index}"
        candidate = base[: MAX_PROJECT_STORAGE_KEY_CHARS - len(suffix)].strip(_EDGE_CHARS) + suffix
        if candidate not in taken:
            return candidate

    raise ValueError(
        f"could not derive a unique project storage key from identifier {identifier!r} and name {name!r}"
    )
