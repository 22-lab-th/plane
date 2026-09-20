# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Unit tests for the server-side object-key builder (AC-05, R-ISO-3, DEC-001)."""

# Python imports
import unicodedata
from uuid import UUID

# Third party imports
import pytest

# Module imports
from plane.utils.object_key import (
    MAX_OBJECT_KEY_BYTES,
    MAX_PROJECT_STORAGE_KEY_CHARS,
    MAX_SEGMENT_CHARS,
    OBJECT_KEY_CATEGORIES,
    build_object_key,
    build_project_storage_key,
    sanitize_key_segment,
    slugify_project_name,
)

FILE_ID = UUID("0193f0a1-6b7c-7d21-9f4a-2c5b8e0d1a44")
FILE_ID_STR = str(FILE_ID)
WORKSPACE_SLUG = "acme"
PROJECT_STORAGE_KEY = "CBUTR-smart-cbu-tracking-system"
KEY_PREFIX = f"workspace/{WORKSPACE_SLUG}/projects/{PROJECT_STORAGE_KEY}"

#: OWASP path-traversal corpus plus the separators and control characters a
#: filename may carry (ARCH-001 §5.2).
TRAVERSAL_FILENAMES = [
    "../etc/passwd",
    "..\\windows\\system32",
    "%2e%2e%2f",
    "..%2f",
    "%252e%252e%255c",
    "%00",
    "in/voice.pdf",
    "in\\voice.pdf",
    "invoice\x00.pdf",
    "../../../../etc/hosts",
    "....//....//etc/shadow",
]


def build(filename, entity_ref=None, category="issues", version_no=1, file_id=FILE_ID):
    return build_object_key(
        WORKSPACE_SLUG,
        PROJECT_STORAGE_KEY,
        category,
        file_id,
        version_no,
        filename,
        entity_ref=entity_ref,
    )


def segments(key):
    return key.split("/")


@pytest.mark.unit
class TestBuildObjectKey:
    """The exact key shape and the rules that keep client input out of it."""

    def test_issue_linked_key_matches_the_approved_pattern(self):
        key = build("Customer Deliverables.pdf", entity_ref="CBUTR-11")

        assert key == f"{KEY_PREFIX}/issues/CBUTR-11/{FILE_ID_STR}/v1/customer-deliverables.pdf"

    def test_project_level_key_omits_the_entity_segment(self):
        key = build("customer-deliverables.pdf")

        assert key == f"{KEY_PREFIX}/issues/{FILE_ID_STR}/v1/customer-deliverables.pdf"

    def test_entity_ref_is_omitted_when_not_given(self):
        assert build("a.pdf", entity_ref=None) == build("a.pdf")

    @pytest.mark.parametrize("entity_ref", ["", "///", "Pages/0193f0a1", "CBUTR|11", "MY:PROJ-11", "a" * 121])
    def test_entity_ref_that_would_have_to_be_modified_is_rejected(self, entity_ref):
        """Only whitespace may be folded, so any other unsafe value is an error."""
        with pytest.raises(ValueError, match="entity_ref"):
            build("a.pdf", entity_ref=entity_ref)

    @pytest.mark.parametrize("entity_ref", ["MY PROJ-11", "MY  PROJ-11"])
    def test_entity_ref_whitespace_folds_to_a_dash(self, entity_ref):
        """A project identifier may contain a space, so ``MY PROJ-11`` is a real key."""
        key = build("a.pdf", entity_ref=entity_ref)

        assert segments(key)[5] == "MY-PROJ-11"
        assert key == f"{KEY_PREFIX}/issues/MY-PROJ-11/{FILE_ID_STR}/v1/a.pdf"

    def test_page_uuid_entity_ref_is_preserved(self):
        key = build("wireframe.png", entity_ref=FILE_ID_STR, category="pages")

        assert segments(key)[5] == FILE_ID_STR
        assert key == f"{KEY_PREFIX}/pages/{FILE_ID_STR}/{FILE_ID_STR}/v1/wireframe.png"

    def test_version_segment_carries_the_version_number(self):
        assert build("a.pdf", version_no=7).endswith(f"{FILE_ID_STR}/v7/a.pdf")

    @pytest.mark.parametrize("version_no", [0, -1, "3", 2.9, True, "seven", None])
    def test_version_number_must_be_a_positive_int(self, version_no):
        with pytest.raises(ValueError, match="version_no"):
            build("a.pdf", version_no=version_no)

    @pytest.mark.parametrize("file_id", [None, 5, True, "not-a-uuid", "../etc/passwd", ""])
    def test_file_id_must_be_a_uuid(self, file_id):
        with pytest.raises(ValueError, match="file_id"):
            build("a.pdf", file_id=file_id)

    def test_file_id_is_canonicalised(self):
        assert build("a.pdf", file_id=FILE_ID_STR.upper()) == build("a.pdf", file_id=FILE_ID)

    def test_nfc_and_nfd_filenames_produce_the_same_key(self):
        nfc = unicodedata.normalize("NFC", "café résumé.pdf")
        nfd = unicodedata.normalize("NFD", "café résumé.pdf")

        assert nfc != nfd
        assert build(nfc) == build(nfd)
        assert build(nfc).endswith("/caf-r-sum.pdf")
        assert build(nfc).isascii()

    def test_accented_filename_keeps_its_extension_and_stem(self):
        assert sanitize_key_segment("café.pdf") == "caf.pdf"
        assert build("café.pdf").endswith("/caf.pdf")

    def test_filename_whose_stem_sanitises_away_keeps_the_extension(self):
        """A fully non-ASCII name falls back to the file id plus its extension."""
        assert sanitize_key_segment("เอกสาร.pdf") == ""
        assert build("เอกสาร.pdf").endswith(f"/{FILE_ID_STR}.pdf")

    @pytest.mark.parametrize("filename", TRAVERSAL_FILENAMES)
    def test_traversal_corpus_cannot_escape_or_forge_a_segment(self, filename):
        key = build(filename)
        parts = segments(key)

        # The only part of the key the client's filename may influence is the
        # last segment, and it can never contain a separator or a traversal
        # fragment.
        assert parts[:-1] == segments(f"{KEY_PREFIX}/issues/{FILE_ID_STR}/v1")
        assert len(parts) == 8
        assert "." not in parts
        assert ".." not in parts
        assert ".." not in key
        assert "%" not in key
        assert "\\" not in key
        assert ":" not in key
        assert "\x00" not in key
        assert key.isascii()

    def test_known_traversal_filenames_sanitise_to_readable_segments(self):
        assert build("../etc/passwd").endswith("/etc-passwd")
        assert build("%2e%2e%2f").endswith("/2e-2e-2f")
        assert build("invoice\x00.pdf").endswith("/invoice.pdf")
        assert build("in/voice.pdf").endswith("/in-voice.pdf")

    @pytest.mark.parametrize("filename", [".", "..", "...", "///", "***", "", None])
    def test_filename_that_sanitises_to_empty_falls_back_to_the_file_id(self, filename):
        assert sanitize_key_segment(filename) == ""
        assert build(filename).endswith(f"/{FILE_ID_STR}/v1/{FILE_ID_STR}")

    def test_colon_in_filename_is_removed(self):
        key = build("C:report.pdf")

        assert ":" not in key
        assert key.endswith("/c-report.pdf")

    @pytest.mark.parametrize(
        "filename,expected_tail",
        [
            ("file.txt:evil.exe", "file.txt-evil.exe"),
            ("..%2f", "2f"),
            ("%00", "00"),
            ("a..b.pdf", "a.b.pdf"),
            ("report.final.PDF", "report.final.pdf"),
            (".gitignore", f"{FILE_ID_STR}.gitignore"),
        ],
    )
    def test_adversarial_filenames_produce_safe_segments(self, filename, expected_tail):
        key = build(filename)

        assert key.endswith(f"/{expected_tail}")
        assert ".." not in key
        assert not any(part in {".", ".."} for part in segments(key))

    def test_long_filename_is_capped_and_keeps_its_extension(self):
        key = build("y" * 300 + ".pdf")
        filename_segment = segments(key)[-1]

        assert len(filename_segment) == MAX_SEGMENT_CHARS
        assert filename_segment.endswith(".pdf")
        assert len(key.encode("utf-8")) <= MAX_OBJECT_KEY_BYTES

    def test_long_filename_without_extension_is_capped(self):
        key = build("x" * 300)

        assert segments(key)[-1] == "x" * MAX_SEGMENT_CHARS
        assert len(key.encode("utf-8")) <= MAX_OBJECT_KEY_BYTES

    def test_non_ascii_filename_still_yields_an_ascii_key(self):
        key = build("รายงานประจำเดือน.pdf")

        assert key.isascii()
        assert key.endswith(f"/{FILE_ID_STR}.pdf")

    @pytest.mark.parametrize("category", sorted(OBJECT_KEY_CATEGORIES))
    def test_every_allowed_category_is_accepted(self, category):
        key = build("a.pdf", category=category)

        assert segments(key)[4] == category
        assert key.startswith(f"workspace/{WORKSPACE_SLUG}/projects/{PROJECT_STORAGE_KEY}/{category}/")

    @pytest.mark.parametrize("category", ["unknown", "Issue", "issues/../archive", "", None, 7])
    def test_unknown_category_is_rejected(self, category):
        with pytest.raises(ValueError, match="unknown file category"):
            build("a.pdf", category=category)

    @pytest.mark.parametrize(
        "workspace_slug,project_storage_key",
        [
            ("", PROJECT_STORAGE_KEY),
            (None, PROJECT_STORAGE_KEY),
            ("acme/other", PROJECT_STORAGE_KEY),
            ("acme:evil", PROJECT_STORAGE_KEY),
            ("acme\\evil", PROJECT_STORAGE_KEY),
            ("acme evil", PROJECT_STORAGE_KEY),
            ("acmeé", PROJECT_STORAGE_KEY),
            ("acme", ""),
            ("acme", None),
            ("acme", "../escape"),
            ("acme", "CBUTR|x"),
            ("acme", "CBUTR x"),
            ("acme", "CBUTR..x"),
        ],
    )
    def test_unusable_server_segments_are_rejected(self, workspace_slug, project_storage_key):
        with pytest.raises(ValueError):
            build_object_key(workspace_slug, project_storage_key, "docs", FILE_ID, 1, "a.pdf")

    def test_server_segments_keep_the_documented_identifier_case(self):
        """DEC-001's approved example prefix is uppercase: it must stay valid."""
        key = build_object_key(WORKSPACE_SLUG, "CBUTR-smart-cbu-tracking-system", "docs", FILE_ID, 1, "a.pdf")

        assert segments(key)[3] == "CBUTR-smart-cbu-tracking-system"

    def test_key_never_carries_a_client_supplied_path_segment(self):
        key = build("../../v9/other.pdf", entity_ref="CBUTR-11")

        assert key == f"{KEY_PREFIX}/issues/CBUTR-11/{FILE_ID_STR}/v1/v9-other.pdf"

    def test_uuid_and_string_file_ids_build_the_same_key(self):
        assert build("a.pdf", file_id=FILE_ID) == build("a.pdf", file_id=FILE_ID_STR)


@pytest.mark.unit
class TestProjectStorageKey:
    """The immutable readable project prefix consumed by the key builder."""

    def test_shape_is_identifier_dash_slugified_name(self):
        assert build_project_storage_key("CBUTR", "Smart CBU Tracking System") == "CBUTR-smart-cbu-tracking-system"

    def test_slugify_collapses_non_alphanumerics(self):
        assert slugify_project_name("Smart  CBU - Tracking (System)") == "smart-cbu-tracking-system"

    def test_name_without_ascii_alphanumerics_leaves_the_identifier_alone(self):
        assert build_project_storage_key("CBUTR", "ระบบติดตาม") == "CBUTR"

    def test_empty_identifier_falls_back_to_a_literal_prefix(self):
        assert build_project_storage_key("", "Demo") == "project-demo"

    def test_long_name_is_truncated_to_the_column_length(self):
        key = build_project_storage_key("CBUTR", "a" * 200)

        assert len(key) == MAX_PROJECT_STORAGE_KEY_CHARS
        assert key.startswith("CBUTR-a")

    def test_collisions_are_suffixed_inside_the_same_workspace(self):
        assert build_project_storage_key("CBUTR", "Demo", {"CBUTR-demo"}) == "CBUTR-demo-2"
        assert build_project_storage_key("CBUTR", "Demo", {"CBUTR-demo", "CBUTR-demo-2"}) == "CBUTR-demo-3"

    def test_collision_suffix_still_fits_the_column(self):
        name = "a" * 200
        base = build_project_storage_key("CBUTR", name)

        assert len(base) == MAX_PROJECT_STORAGE_KEY_CHARS

        key = build_project_storage_key("CBUTR", name, {base})

        assert key != base
        assert key.endswith("-2")
        assert len(key) <= MAX_PROJECT_STORAGE_KEY_CHARS

    def test_derived_prefix_composes_into_a_valid_object_key(self):
        storage_key = build_project_storage_key("CBUTR", "Warehouse Inventory")
        key = build_object_key(WORKSPACE_SLUG, storage_key, "docs", FILE_ID, 1, "spec.pdf")

        assert key == f"workspace/{WORKSPACE_SLUG}/projects/CBUTR-warehouse-inventory/docs/{FILE_ID_STR}/v1/spec.pdf"
