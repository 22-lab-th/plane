# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Content-signature checks for the first bytes of an uploaded object (AC-39).

The server observes an object's byte length, content type and ``ETag`` with a
HEAD request, but a HEAD cannot tell whether the bytes actually are what the
client declared. Finalize therefore reads the first 512 bytes with one ranged
GET and compares them against the declared type's signature (ARCH-001 §4.2,
§5.1). A type that has no reliable signature — ``application/octet-stream``,
the ambiguous ``application/x-compressed*`` aliases — is reported as
*not checkable* rather than silently passing, so callers can record exactly what
was verified (AD-16) and never claim more than they saw.
"""

# Python imports
import codecs
import re

#: Number of leading bytes the ranged GET reads (ARCH-001 §5.1).
HEAD_BYTES = 512

#: Signatures matched at offset 0; any one of them matches.
_PREFIX_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/bmp": (b"BM",),
    "image/tiff": (b"II*\x00", b"MM\x00*"),
    "image/x-portable-bitmap": (b"P4", b"P1"),
    "image/x-portable-graymap": (b"P5", b"P2"),
    "image/x-portable-pixmap": (b"P6", b"P3"),
    "audio/mpeg": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1"),
    "audio/ogg": (b"OggS",),
    "video/ogg": (b"OggS",),
    "audio/flac": (b"fLaC",),
    "audio/midi": (b"MThd",),
    "audio/x-midi": (b"MThd",),
    "audio/aac": (b"\xff\xf1", b"\xff\xf9"),
    "font/woff": (b"wOFF",),
    "font/woff2": (b"wOF2",),
    "font/otf": (b"OTTO",),
    "font/ttf": (b"\x00\x01\x00\x00", b"true", b"ttcf"),
    "model/gltf-binary": (b"glTF",),
    "application/gzip": (b"\x1f\x8b",),
    "application/x-gzip": (b"\x1f\x8b",),
    "application/x-7z-compressed": (b"7z\xbc\xaf\x27\x1c",),
    "application/x-rar": (b"Rar!\x1a\x07",),
    "application/x-rar-compressed": (b"Rar!\x1a\x07",),
    # Zip containers, including every OOXML and ODF document type below.
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "application/x-zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "application/x-zip-compressed": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    # OLE2 compound files: legacy Word/Excel/PowerPoint and Visio documents.
    "application/msword": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "application/vnd.ms-excel": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "application/vnd.ms-powerpoint": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "application/vnd.visio": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

#: Signatures matched at a fixed offset; any one of the pairs matches.
_OFFSET_SIGNATURES: dict[str, tuple[tuple[int, bytes], ...]] = {
    "application/x-tar": ((257, b"ustar"),),
    "video/mp4": ((4, b"ftyp"),),
    "audio/x-m4a": ((4, b"ftyp"),),
    "video/quicktime": ((4, b"ftyp"), (4, b"moov"), (4, b"mdat"), (4, b"wide"), (4, b"free")),
}

#: Signatures made of several parts, all of which must match.
_STRUCTURED_SIGNATURES: dict[str, tuple[tuple[int, bytes], ...]] = {
    "image/webp": ((0, b"RIFF"), (8, b"WEBP")),
    "audio/wav": ((0, b"RIFF"), (8, b"WAVE")),
    "video/x-msvideo": ((0, b"RIFF"), (8, b"AVI ")),
}

#: Types whose content is text: validated as UTF-8 without NUL bytes.
TEXT_MIME_TYPES: frozenset[str] = frozenset(
    [
        "text/plain",
        "text/csv",
        "text/markdown",
        "text/css",
        "text/javascript",
        "text/xml",
        "text/html",
        "application/json",
        "application/xml",
        "application/rtf",
        "application/x-sql",
        "image/svg+xml",
        "model/gltf+json",
    ]
)

#: The OOXML types whose container is a zip archive.
_OOXML_MIME_TYPES: tuple[str, ...] = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)

#: The OpenDocument types whose container is a zip archive.
_ODF_MIME_TYPES: tuple[str, ...] = (
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.graphics",
    "application/vnd.oasis.opendocument.database",
)

for _zip_mime_type in _OOXML_MIME_TYPES + _ODF_MIME_TYPES:
    _PREFIX_SIGNATURES[_zip_mime_type] = _PREFIX_SIGNATURES["application/zip"]

_PARAMETER = re.compile(r";.*\Z")
_UTF8_DECODER = codecs.getincrementaldecoder("utf-8")


def normalize_mime_type(mime_type):
    """Return ``mime_type`` lowercased without parameters, or ``""``."""
    if not isinstance(mime_type, str):
        return ""
    return _PARAMETER.sub("", mime_type).strip().lower()


def is_checkable(mime_type):
    """Return True when a signature or text rule exists for ``mime_type``."""
    mime_type = normalize_mime_type(mime_type)
    return (
        mime_type in _PREFIX_SIGNATURES
        or mime_type in _OFFSET_SIGNATURES
        or mime_type in _STRUCTURED_SIGNATURES
        or mime_type in TEXT_MIME_TYPES
    )


def _is_text(head):
    """Return True when ``head`` decodes as UTF-8 and carries no NUL byte.

    ``head`` is a prefix of the object, so an incomplete multi-byte sequence at
    the 512-byte boundary is expected and tolerated; any other invalid byte is
    not.
    """
    if b"\x00" in head:
        return False

    try:
        _UTF8_DECODER().decode(head, final=False)
    except UnicodeDecodeError:
        return False

    return True


def check_magic_bytes(mime_type, head):
    """Compare the first bytes of an object with the declared type's signature.

    :param mime_type: the content type the client declared.
    :param head: the first :data:`HEAD_BYTES` bytes of the object.
    :returns: ``True`` when the bytes match the declared type, ``False`` when
        they contradict it, and ``None`` when the declared type has no reliable
        signature — a caller must record ``None`` as "not checked", never as a
        pass (AD-16).
    """
    mime_type = normalize_mime_type(mime_type)

    if not isinstance(head, (bytes, bytearray)):
        raise ValueError("head must be bytes")

    if mime_type in _PREFIX_SIGNATURES:
        return any(bytes(head).startswith(signature) for signature in _PREFIX_SIGNATURES[mime_type])

    if mime_type in _OFFSET_SIGNATURES:
        return any(
            bytes(head[offset : offset + len(signature)]) == signature
            for offset, signature in _OFFSET_SIGNATURES[mime_type]
        )

    if mime_type in _STRUCTURED_SIGNATURES:
        return all(
            bytes(head[offset : offset + len(signature)]) == signature
            for offset, signature in _STRUCTURED_SIGNATURES[mime_type]
        )

    if mime_type in TEXT_MIME_TYPES:
        return _is_text(bytes(head))

    return None
