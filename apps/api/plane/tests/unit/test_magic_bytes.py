# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Unit tests for the finalize magic-byte check (AC-39)."""

# Third party imports
import pytest

# Module imports
from plane.utils.magic_bytes import check_magic_bytes, is_checkable, normalize_mime_type

PDF_HEAD = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
ZIP_HEAD = b"PK\x03\x04\x14\x00\x00\x00"


@pytest.mark.unit
class TestCheckMagicBytes:
    def test_pdf_bytes_match_a_pdf_declaration(self):
        assert check_magic_bytes("application/pdf", PDF_HEAD) is True

    def test_zip_bytes_contradict_a_pdf_declaration(self):
        """AC-39: an object whose first bytes contradict the type is not activated."""
        assert check_magic_bytes("application/pdf", ZIP_HEAD) is False

    def test_png_bytes_match_a_png_declaration(self):
        assert check_magic_bytes("image/png", PNG_HEAD) is True

    @pytest.mark.parametrize(
        "mime_type,head",
        [
            ("application/zip", ZIP_HEAD),
            ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ZIP_HEAD),
            ("application/vnd.oasis.opendocument.text", ZIP_HEAD),
            ("image/webp", b"RIFF\x24\x00\x00\x00WEBPVP8 "),
            ("audio/wav", b"RIFF\x24\x00\x00\x00WAVEfmt "),
            ("application/x-tar", b"\x00" * 257 + b"ustar\x0000"),
            ("video/mp4", b"\x00\x00\x00\x18ftypmp42"),
            ("application/x-7z-compressed", b"7z\xbc\xaf\x27\x1c\x00\x04"),
        ],
    )
    def test_documented_containers_are_recognised(self, mime_type, head):
        assert check_magic_bytes(mime_type, head) is True

    def test_a_container_header_of_the_wrong_kind_is_rejected(self):
        # "RIFF" alone is not enough for webp: the second part must match too.
        assert check_magic_bytes("image/webp", b"RIFF\x24\x00\x00\x00WAVEfmt ") is False

    @pytest.mark.parametrize("mime_type", ["text/plain", "text/csv", "text/markdown", "application/json"])
    def test_text_types_accept_utf8_without_nul(self, mime_type):
        assert check_magic_bytes(mime_type, "id,name\n1,สอง\n".encode()) is True

    @pytest.mark.parametrize("mime_type", ["text/plain", "application/json"])
    def test_text_types_reject_nul_and_invalid_utf8(self, mime_type):
        assert check_magic_bytes(mime_type, b"name\x00value") is False
        assert check_magic_bytes(mime_type, b"\xff\xfe\x00\x01") is False

    def test_a_multibyte_character_cut_at_the_boundary_is_still_text(self):
        """The ranged GET stops at 512 bytes, so a split sequence is expected."""
        head = b"a" * 511 + "ก".encode()[:2]

        assert check_magic_bytes("text/plain", head) is True

    def test_an_empty_head_contradicts_a_binary_declaration(self):
        assert check_magic_bytes("application/pdf", b"") is False

    def test_empty_head_is_acceptable_text(self):
        assert check_magic_bytes("text/plain", b"") is True

    @pytest.mark.parametrize("mime_type", ["application/octet-stream", "application/x-compressed-tar"])
    def test_signature_less_types_are_reported_as_not_checkable(self, mime_type):
        """``None`` means "not checked" and must never be read as a pass (AD-16)."""
        assert is_checkable(mime_type) is False
        assert check_magic_bytes(mime_type, ZIP_HEAD) is None

    def test_mime_type_parameters_are_ignored(self):
        assert normalize_mime_type("Application/PDF; charset=utf-8") == "application/pdf"
        assert check_magic_bytes("application/pdf; charset=binary", PDF_HEAD) is True

    @pytest.mark.parametrize("mime_type", ["image/jpeg", "image/gif", "application/pdf", "text/plain"])
    def test_allowlisted_types_are_checkable(self, mime_type):
        assert is_checkable(mime_type) is True

    def test_head_must_be_bytes(self):
        with pytest.raises(ValueError):
            check_magic_bytes("application/pdf", "%PDF-1.7")
