import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from plane.utils.confluence.client import (
    ConfluenceClient,
    ConfluenceError,
    category_for,
    should_import,
    validate_site_url,
)
from plane.utils.confluence.content import rewrite_page_html

CONFIG = {"site_url": "https://team.atlassian.net", "email": "member@example.com", "api_token": "test-token"}


def response(status=200, *, body=b"", headers=None):
    return SimpleNamespace(
        status_code=status, headers=headers or {}, iter_content=lambda size: iter([body]), close=Mock()
    )


@pytest.mark.unit
class TestConfluenceClient:
    @pytest.mark.parametrize(
        "url",
        [
            "http://team.atlassian.net",
            "https://attacker.example/wiki",
            "https://user:pass@team.atlassian.net",
            "https://team.atlassian.net:444",
            "https://team.atlassian.net:bad",
            "https://team.atlassian.net/other",
            "https://team.atlassian.net?x=1",
        ],
    )
    def test_refuses_untrusted_configured_hosts(self, url):
        with pytest.raises(ConfluenceError, match="Use https"):
            validate_site_url(url)

    def test_scoped_token_pagination_uses_gateway(self, monkeypatch):
        cloud_id = "00000000-0000-4000-8000-000000000000"
        client = ConfluenceClient({**CONFIG, "cloud_id": cloud_id})
        fetch = Mock(
            side_effect=[
                response(
                    body=json.dumps(
                        {
                            "results": [{"id": "1"}],
                            "_links": {"next": "https://team.atlassian.net/wiki/api/v2/spaces?cursor=second"},
                        }
                    ).encode()
                ),
                response(body=b'{"results":[{"id":"2"}]}'),
            ]
        )
        monkeypatch.setattr("plane.utils.confluence.client.pinned_fetch", fetch)
        assert list(client.paginate("/wiki/api/v2/spaces")) == [{"id": "1"}, {"id": "2"}]
        assert all(
            call.args[1].startswith(f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/")
            for call in fetch.call_args_list
        )

    def test_download_never_forwards_credentials_to_cdn(self, monkeypatch):
        fetch = Mock(
            side_effect=[response(302, headers={"Location": "https://media-cdn.example/image"}), response(body=b"png")]
        )
        monkeypatch.setattr("plane.utils.confluence.client.pinned_fetch", fetch)
        stream, size = ConfluenceClient(CONFIG).download(
            {"downloadLink": "/wiki/download/attachments/1/a.png", "fileSize": 3}, 100
        )
        assert size == 3 and stream.read() == b"png"
        stream.close()
        assert "Authorization" in fetch.call_args_list[0].kwargs["headers"]
        assert "Authorization" not in fetch.call_args_list[1].kwargs["headers"]

    def test_unsafe_redirect_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            "plane.utils.confluence.client.pinned_fetch",
            Mock(return_value=response(302, headers={"Location": "http://127.0.0.1/private"})),
        )
        with pytest.raises(ConfluenceError) as exc:
            ConfluenceClient(CONFIG).download({"downloadLink": "/wiki/download/attachments/1/a.png"}, 100)
        assert exc.value.code == "unsafe_download"

    def test_credentials_do_not_appear_in_http_failure(self, monkeypatch):
        monkeypatch.setattr(
            "plane.utils.confluence.client.pinned_fetch", Mock(return_value=response(401, body=b"test-token"))
        )
        with pytest.raises(ConfluenceError) as exc:
            ConfluenceClient(CONFIG).json("/wiki/api/v2/spaces")
        assert exc.value.code == "atlassian_http_401"
        assert "test-token" not in exc.value.message

    def test_rate_limit_retries_are_bounded(self, monkeypatch):
        fetch = Mock(return_value=response(429, headers={"Retry-After": "999"}))
        sleep = Mock()
        monkeypatch.setattr("plane.utils.confluence.client.pinned_fetch", fetch)
        monkeypatch.setattr("plane.utils.confluence.client.time.sleep", sleep)
        with pytest.raises(ConfluenceError) as exc:
            ConfluenceClient(CONFIG).json("/wiki/api/v2/spaces")
        assert exc.value.code == "atlassian_http_429" and fetch.call_count == 3
        assert [call.args[0] for call in sleep.call_args_list] == [30, 30]

    @pytest.mark.parametrize(
        "body,size,limit,code", [(b"four", 4, 3, "file_too_large"), (b"bad", 5, 100, "size_mismatch")]
    )
    def test_download_limits_and_metadata(self, monkeypatch, body, size, limit, code):
        monkeypatch.setattr("plane.utils.confluence.client.pinned_fetch", Mock(return_value=response(body=body)))
        with pytest.raises(ConfluenceError) as exc:
            ConfluenceClient(CONFIG).download({"downloadLink": "/wiki/download/a", "fileSize": size}, limit)
        assert exc.value.code == code

    def test_pagination_loop_fails(self, monkeypatch):
        monkeypatch.setattr(
            "plane.utils.confluence.client.pinned_fetch",
            Mock(return_value=response(body=b'{"results":[],"_links":{"next":"/wiki/api/v2/spaces"}}')),
        )
        with pytest.raises(ConfluenceError) as exc:
            list(ConfluenceClient(CONFIG).paginate("/wiki/api/v2/spaces"))
        assert exc.value.code == "pagination_loop"


@pytest.mark.unit
class TestImportSelection:
    def test_changes_retry_failures_but_skip_same_version(self):
        values = {"imported_version": 2, "remote_version": 2}
        assert not should_import("changed", **values)
        assert should_import("changed", **values, failed=True)
        assert should_import("changed", imported_version=2, remote_version=3)
        assert should_import("changed", **values, destination_exists=False)
        assert should_import("all", **values)
        assert not should_import("failed", **values)
        assert should_import("failed", **values, failed=True)
        assert should_import("failed", imported_version=0, remote_version=1)
        assert should_import("selected", **values, selected=True)
        assert not should_import("selected", **values, failed=True)

    def test_document_types_remain_separate(self):
        assert category_for("attachment", "image/png") == "image"
        assert category_for("attachment", "video/mp4") == "video"
        assert category_for("attachment", "application/pdf") == "application/pdf"
        assert category_for("page", None) == "page"


def attachment(remote_id, title, mime, file_id):
    return SimpleNamespace(
        remote_id=remote_id,
        title=title,
        remote={"downloadLink": f"/wiki/download/attachments/1/{title}"},
        file=SimpleNamespace(id=file_id, mime_type=mime, deleted_at=None, status="active"),
    )


@pytest.mark.unit
class TestConfluenceHTML:
    def test_preserves_inline_media_positions_and_internal_page_links(self):
        picture = attachment("att2", "picture.png", "image/png", "00000000-0000-4000-8000-000000000002")
        video = attachment("att3", "demo.mp4", "video/mp4", "00000000-0000-4000-8000-000000000003")
        html = rewrite_page_html(
            '<p>Before</p><img src="/wiki/download/thumbnails/1/picture.png?version=1" width="320" onerror="bad()">'
            '<p>Middle</p><video><source src="/wiki/download/attachments/1/demo.mp4"></video><p>After</p>'
            '<a href="/wiki/spaces/SPACE/pages/9/title">Next</a>',
            site_url=CONFIG["site_url"],
            page_urls={"9": "/plane/pages/nine"},
            attachments=[picture, video],
            files_url="/plane/files",
        )
        assert (
            html.index("Before")
            < html.index("image-component")
            < html.index("Middle")
            < html.index("video-component")
            < html.index("After")
        )
        assert f"project-file:{picture.file.id}" in html and f"project-file:{video.file.id}" in html
        assert 'href="/plane/pages/nine"' in html and "onerror" not in html

    def test_missing_media_keeps_the_destination_untouched(self):
        with pytest.raises(ConfluenceError) as exc:
            rewrite_page_html(
                '<p>Content</p><img src="/missing.png">',
                site_url=CONFIG["site_url"],
                page_urls={},
                attachments=[],
                files_url="/files",
            )
        assert exc.value.code == "media_dependency_failed" and "/missing.png" in exc.value.message

    def test_unsupported_video_and_audio_have_file_links(self):
        movie = attachment("att2", "movie.mov", "video/quicktime", "00000000-0000-4000-8000-000000000002")
        audio = attachment("att3", "audio.mp3", "audio/mpeg", "00000000-0000-4000-8000-000000000003")
        html = rewrite_page_html(
            '<video src="/wiki/download/attachments/1/movie.mov"></video>'
            '<audio src="/wiki/download/attachments/1/audio.mp3"></audio>',
            site_url=CONFIG["site_url"],
            page_urls={},
            attachments=[movie, audio],
            files_url="/files",
        )
        assert f'href="/files?file={movie.file.id}"' in html and f'href="/files?file={audio.file.id}"' in html


@pytest.mark.unit
def test_config_request_logs_omit_tokens(monkeypatch):
    from django.http import HttpResponse
    from django.test import RequestFactory
    from plane.middleware.logger import APITokenLogMiddleware

    enqueue = Mock()
    monkeypatch.setattr("plane.middleware.logger.process_logs.delay", enqueue)
    request = RequestFactory().patch(
        "/api/instances/confluence/",
        data={"api_token": "never-log-this-token"},
        content_type="application/json",
        HTTP_X_API_KEY="key",
    )
    APITokenLogMiddleware(lambda req: HttpResponse("{}"))(request)
    assert enqueue.call_count == 1
    assert "never-log-this-token" not in str(enqueue.call_args.kwargs)
    assert enqueue.call_args.kwargs["log_data"]["body"] == "[Confluence configuration redacted]"
