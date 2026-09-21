# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-115 / DEFECT-011: a project-file embed survives the HTML sanitiser (unit level).

The editor's source of truth is the yjs binary, but the server keeps a
``description_html`` mirror and rewrites it with the sanitised string on every save. A
``project-file:<file id>`` reference is this application's own inert reference to a
project file - the API resolves it to a presigned URL and the browser never fetches it
itself - and it was stripped on every save, so every html-derived consumer (the page
fallback, export, search, page copy) saw an embed with no source.

The sanitiser admits the scheme and scopes it to an embed's ``src``; everything else a
sanitiser is for must still happen. The page save itself is
``tests/contract/app/test_page_embed_html_mirror.py``.
"""

# Third party imports
import pytest

# Module imports
from plane.utils.content_validator import validate_html_content

#: A reference as the editor writes it: the scheme and a file id.
REF = "project-file:8f0e0c1e-0000-4000-8000-000000000000"


def clean(html):
    """Validate one fragment and return the sanitised string."""
    is_valid, error, sanitised = validate_html_content(html)
    assert is_valid is True, error
    return sanitised


class TestProjectFileEmbedReference:
    def test_an_embed_keeps_its_project_file_source(self):
        sanitised = clean(f'<image-component src="{REF}" status="uploaded" width="320"></image-component>')

        assert f'src="{REF}"' in sanitised
        assert 'status="uploaded"' in sanitised

    def test_an_img_carrying_the_reference_is_kept_too(self):
        """The same reference appears on an ``img`` in documents written the other way."""
        sanitised = clean(f'<img src="{REF}" alt="diagram">')

        assert f'src="{REF}"' in sanitised

    def test_the_reference_scheme_is_not_honoured_outside_an_embed_source(self):
        """A link's href is not an embed: the scheme stays refused there."""
        sanitised = clean(f'<a href="{REF}">open</a>')

        assert REF not in sanitised

    def test_a_reference_on_an_unrelated_attribute_is_dropped(self):
        sanitised = clean(f'<p title="{REF}">text</p>')

        assert REF not in sanitised
        assert "<p>text</p>" in sanitised

    @pytest.mark.parametrize(
        "value",
        ["javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "vbscript:msgbox(1)"],
    )
    def test_script_capable_sources_are_still_stripped(self, value):
        sanitised = clean(f'<img src="{value}" alt="x">')

        assert value.split(":")[0] not in sanitised
        assert "src=" not in sanitised

    def test_the_ordinary_embeds_are_untouched(self):
        """A bare asset id and an https URL keep the behaviour they had."""
        sanitised = clean(
            '<img src="https://example.com/a.png" alt="a">'
            '<image-component src="8f0e0c1e-0000-4000-8000-000000000000"></image-component>'
        )

        assert 'src="https://example.com/a.png"' in sanitised
        assert 'src="8f0e0c1e-0000-4000-8000-000000000000"' in sanitised

    def test_a_script_tag_is_still_removed(self):
        sanitised = clean(f'<script>alert(1)</script><image-component src="{REF}"></image-component>')

        assert "<script" not in sanitised
        assert f'src="{REF}"' in sanitised


