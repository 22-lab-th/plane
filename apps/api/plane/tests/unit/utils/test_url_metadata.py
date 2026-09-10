# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import MagicMock, patch

import pytest

from plane.utils.url_metadata import MAX_HTML_BYTES, URLMetadataError, fetch_url_metadata


def _response(body, content_type="text/html; charset=utf-8"):
    response = MagicMock()
    response.headers = {"content-type": content_type, "content-length": str(len(body))}
    response.iter_content.return_value = [body]
    response.raise_for_status.return_value = None
    return response


@pytest.mark.unit
class TestFetchURLMetadata:
    @patch("plane.utils.url_metadata.pinned_fetch_following_redirects")
    def test_extracts_open_graph_title_and_description(self, fetch):
        body = b"""
            <html><head>
              <title>Fallback title</title>
              <meta property="og:title" content="Product documentation">
              <meta name="description" content="  Guides and   references. ">
            </head></html>
        """
        response = _response(body)
        fetch.return_value = (response, "https://example.com/docs")

        result = fetch_url_metadata("example.com/docs")

        assert result == {
            "url": "https://example.com/docs",
            "title": "Product documentation",
            "description": "Guides and references.",
        }
        assert fetch.call_args.args[:2] == ("GET", "https://example.com/docs")
        assert fetch.call_args.kwargs["stream"] is True
        response.close.assert_called_once()

    @patch("plane.utils.url_metadata.pinned_fetch_following_redirects")
    def test_rejects_non_html_response(self, fetch):
        response = _response(b"binary", "application/octet-stream")
        fetch.return_value = (response, "https://example.com/file")

        with pytest.raises(URLMetadataError, match="HTML"):
            fetch_url_metadata("https://example.com/file")
        response.close.assert_called_once()

    @patch("plane.utils.url_metadata.pinned_fetch_following_redirects")
    def test_rejects_oversized_stream(self, fetch):
        response = _response(b"")
        response.headers.pop("content-length")
        response.iter_content.return_value = [b"x" * (MAX_HTML_BYTES + 1)]
        fetch.return_value = (response, "https://example.com/large")

        with pytest.raises(URLMetadataError, match="too large"):
            fetch_url_metadata("https://example.com/large")
        response.close.assert_called_once()
