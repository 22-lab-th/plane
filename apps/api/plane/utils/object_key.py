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
database, the category comes from the server-side allowlist, and the filename is
sanitised by :func:`sanitize_key_segment` before it reaches the key. Anything
that cannot be represented safely is rejected with :class:`ValueError` rather
than silently folded into a different value.
"""

# Python imports
import re
import unicodedata
from uuid import UUID

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
_DASH_RUN = re.compile(r"-{2,}")
_DOT_RUN = re.compile(r"\.{2,}")
#: A real extension: alphanumeric only, which is what keeps "..", "%2f" and
#: other traversal fragments in the stem where they are sanitised away.
_EXTENSION = re.compile(r"\A[a-z0-9]{1,%d}\Z" % MAX_EXTENSION_CHARS)
#: Segments that are embedded in a key verbatim. Uppercase is allowed because
#: DEC-001's approved example project prefix is ``CBUTR-smart-cbu-tracking-system``
#: and issue keys are ``CBUTR-11``; every character that could forge a path
#: (``/``, ``\\``, ``:``, ``|``, whitespace, ``%``, control characters,
#: non-ASCII) is rejected.
_KEY_SEGMENT = re.compile(r"\A[A-Za-z0-9._-]+\Z")
#: Runs of whitespace in an entity reference fold to "-" (an issue key may be
#: ``MY PROJ-11`` when the project identifier contains a space).
_ENTITY_REF_WHITESPACE = re.compile(r"\s+")
_PROJECT_KEY_DISALLOWED = re.compile(r"[^A-Za-z0-9]+")
_EDGE_CHARS = ".-"


def _sanitize_text(value):
    """Reduce an already lowercased ``value`` to an object-key segment's characters."""
    # Control characters (including NUL) are removed rather than replaced, so
    # they can never forge a separator.
    text = _CONTROL_CHARS.sub("", value)
    text = _SEGMENT_DISALLOWED.sub("-", text)
    # Dashes and dots collapse separately: collapsing them together would eat the
    # dot that separates a filename from its extension.
    text = _DASH_RUN.sub("-", text)
    text = _DOT_RUN.sub(".", text)
    return text.strip(_EDGE_CHARS)


def _split_extension(name):
    """Split an already-normalised, lowercased name into ``(head, extension)``.

    Only an alphanumeric extension is recognised, so ``..%2f`` and
    ``/etc/passwd`` stay in the head and are sanitised as text instead of being
    promoted to an extension.
    """
    stem, dot, extension = name.rpartition(".")
    if not dot or not _EXTENSION.match(extension):
        return name, ""
    return stem, extension


def _cap_segment(segment):
    """Cap a filename segment at :data:`MAX_SEGMENT_CHARS`, keeping its extension."""
    if len(segment) <= MAX_SEGMENT_CHARS:
        return segment

    stem, extension = _split_extension(segment)
    if extension:
        stem = stem[: MAX_SEGMENT_CHARS - len(extension) - 1].strip(_EDGE_CHARS)
        if stem:
            return f"{stem}.{extension}"

    return segment[:MAX_SEGMENT_CHARS].strip(_EDGE_CHARS)


def sanitize_key_segment(name, fallback_stem=""):
    """Return ``name`` as a safe, lowercased, single object-key segment.

    * NFC-normalised and lowercased;
    * control characters are removed, and every character outside ``[a-z0-9._-]``
      becomes ``-`` (this is what neutralises path separators, ``:`` and ``%``);
    * runs of dashes collapse to one ``-`` and runs of dots to one ``.``, so
      ``..``, ``../`` and their percent-encoded spellings cannot survive;
    * the final alphanumeric extension is kept, and the segment is capped at
      :data:`MAX_SEGMENT_CHARS`.

    :param fallback_stem: stem used when everything before the extension
        sanitises away — the file id, during key building — so ``เอกสาร.pdf``
        becomes ``<fallback_stem>.pdf`` instead of an empty segment. Without a
        fallback such a name sanitises to ``""``.
    """
    if not isinstance(name, str) or not name:
        return fallback_stem or ""

    normalized = _CONTROL_CHARS.sub("", unicodedata.normalize("NFC", name)).lower()
    stem, extension = _split_extension(normalized)
    stem = _sanitize_text(stem)
    extension = _sanitize_text(extension)

    if not stem:
        if not fallback_stem:
            return ""
        return _cap_segment(f"{fallback_stem}.{extension}" if extension else fallback_stem)

    return _cap_segment(f"{stem}.{extension}" if extension else stem)


def _require_key_segment(value, label):
    """Validate a server-provided segment that is embedded verbatim in a key."""
    if value is None:
        raise ValueError(f"{label} must not be None")

    segment = str(value)
    if not _KEY_SEGMENT.match(segment) or segment in {".", ".."} or ".." in segment:
        raise ValueError(f"{label} is not a usable object-key segment: {value!r}")

    return segment


def _require_entity_ref(value):
    """Validate an entity reference, or return ``""`` when there is none.

    The reference is embedded verbatim (NFC-normalised, case preserved), with one
    exception: runs of whitespace fold to ``-``, because a project identifier may
    legally contain a space (``MY PROJ``), which makes ``MY PROJ-11`` a real issue
    key. Anything else that would have to be changed to fit ``[A-Za-z0-9._-]``
    (``:``, ``|``, ``/``, ``%``, …) is rejected rather than silently folded into
    something else.
    """
    if value is None:
        return ""

    if not isinstance(value, str):
        raise ValueError(f"entity_ref is not a usable object-key segment: {value!r}")

    reference = _ENTITY_REF_WHITESPACE.sub("-", unicodedata.normalize("NFC", value))
    if not _KEY_SEGMENT.match(reference) or reference in {".", ".."} or ".." in reference:
        raise ValueError(f"entity_ref is not a usable object-key segment: {value!r}")
    if len(reference) > MAX_SEGMENT_CHARS:
        raise ValueError(f"entity_ref is longer than {MAX_SEGMENT_CHARS} characters: {value!r}")

    return reference


def _version_segment(version_no):
    """Validate ``version_no`` and render it as the ``v{n}`` key segment."""
    if isinstance(version_no, bool) or not isinstance(version_no, int):
        raise ValueError(f"version_no must be an int: {version_no!r}")

    if version_no < 1:
        raise ValueError(f"version_no must be positive: {version_no!r}")

    return f"v{version_no}"


def _file_segment(file_id):
    """Return the canonical UUID segment for ``file_id``.

    The file id is database-generated, never client-supplied; anything that is
    not a UUID is rejected so a malformed caller cannot inject a segment.
    """
    try:
        return str(UUID(str(file_id)))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"file_id must be a UUID: {file_id!r}")


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
    :param file_id: ``FileObject.id`` as a UUID (or its string form); never
        client-supplied.
    :param version_no: 1-based ``FileVersion.version_no`` as an ``int``.
    :param filename: original upload filename; sanitised, never trusted.
    :param entity_ref: optional single segment binding the file to an entity
        (for example ``CBUTR-11`` or a page UUID). Omitted when ``None``; runs of
        whitespace fold to ``-`` and anything else outside ``[A-Za-z0-9._-]``
        raises :class:`ValueError`.
    :raises ValueError: for an unknown category, an unusable server segment or a
        filename that cannot be represented safely.
    """
    if category not in OBJECT_KEY_CATEGORIES:
        raise ValueError(f"unknown file category: {category!r}")

    workspace_segment = _require_key_segment(workspace_slug, "workspace_slug")
    project_segment = _require_key_segment(project_storage_key, "project_storage_key")
    file_segment = _file_segment(file_id)
    version_segment = _version_segment(version_no)
    # A filename whose stem sanitises away keeps its extension on the file id, so
    # no object is ever stored without a readable suffix and no key segment is
    # ever empty.
    filename_segment = sanitize_key_segment(filename, fallback_stem=file_segment)
    entity_segment = _require_entity_ref(entity_ref)

    segments = ["workspace", workspace_segment, "projects", project_segment, category]
    if entity_segment:
        segments.append(entity_segment)
    segments.extend([file_segment, version_segment, filename_segment])

    key = "/".join(segments)
    key_bytes = len(key.encode("utf-8"))
    if key_bytes > MAX_OBJECT_KEY_BYTES:
        raise ValueError(f"object key is {key_bytes} bytes, over the {MAX_OBJECT_KEY_BYTES}-byte limit")

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
