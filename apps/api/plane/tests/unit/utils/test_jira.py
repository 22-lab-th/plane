import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from plane.utils.confluence.client import ConfluenceError
from plane.utils.jira.client import JiraClient
from plane.utils.jira.content import adf_html, choose_sprint, revision

pytestmark = pytest.mark.unit
CONFIG = {"site_url": "https://team.atlassian.net", "email": "member@example.com", "api_token": "jira-secret"}


def response(body, status=200, headers=None):
    return SimpleNamespace(
        status_code=status, headers=headers or {}, iter_content=lambda size: iter([body]), close=Mock()
    )


def test_enhanced_search_follows_tokens_and_uses_scoped_gateway(monkeypatch):
    client = JiraClient({**CONFIG, "cloud_id": "00000000-0000-4000-8000-000000000000"})
    fetch = Mock(
        side_effect=[
            response(json.dumps({"issues": [{"id": "1"}], "nextPageToken": "second"}).encode()),
            response(b'{"issues":[{"id":"2"}],"isLast":true}'),
        ]
    )
    monkeypatch.setattr("plane.utils.confluence.client.pinned_fetch", fetch)
    assert list(client.issues("100", ["customfield_10020"])) == [{"id": "1"}, {"id": "2"}]
    assert all("/ex/jira/" in call.args[1] and "/search/jql?" in call.args[1] for call in fetch.call_args_list)
    assert "nextPageToken=second" in fetch.call_args_list[1].args[1]
    assert "jira-secret" not in fetch.call_args_list[0].args[1]


def test_sprint_pagination_without_total_keeps_advancing(monkeypatch):
    client = JiraClient(CONFIG)
    read = Mock(
        side_effect=[
            {"values": [{"id": 1}], "startAt": 0, "isLast": False},
            {"values": [{"id": 2}], "startAt": 1, "isLast": True},
        ]
    )
    monkeypatch.setattr(client, "json", read)
    assert list(client.pages("/rest/agile/1.0/board/1/sprint")) == [{"id": 1}, {"id": 2}]


def test_search_detects_repeated_token(monkeypatch):
    client = JiraClient(CONFIG)
    monkeypatch.setattr(client, "json", lambda *args: {"issues": [], "nextPageToken": "same"})
    with pytest.raises(ConfluenceError, match="repeated"):
        list(client.issues("100", []))


def test_search_reports_missing_cursor_instead_of_incomplete_success(monkeypatch):
    client = JiraClient(CONFIG)
    monkeypatch.setattr(client, "json", lambda *args: {"issues": [], "isLast": False})
    with pytest.raises(ConfluenceError, match="omitted"):
        list(client.issues("100", []))


def test_sprint_reports_non_advancing_page(monkeypatch):
    client = JiraClient(CONFIG)
    monkeypatch.setattr(client, "json", lambda *args: {"values": [], "isLast": False})
    with pytest.raises(ConfluenceError, match="did not advance"):
        list(client.pages("/rest/agile/1.0/board/1/sprint"))


@pytest.mark.parametrize(
    "path", ["https://attacker.example/rest/api/3/issue/1", "/rest/api/3/../admin", "/wiki/api/v2/pages"]
)
def test_refuses_unexpected_api_paths(path):
    with pytest.raises(ConfluenceError):
        JiraClient(CONFIG).api_url(path)


def test_attachment_redirect_does_not_forward_credentials(monkeypatch):
    fetch = Mock(side_effect=[response(b"", 302, {"Location": "https://cdn.example/file"}), response(b"abc")])
    monkeypatch.setattr("plane.utils.confluence.client.pinned_fetch", fetch)
    stream, size = JiraClient(CONFIG).download({"id": "123", "fileSize": 3, "content": "https://attacker.example/"}, 10)
    try:
        assert size == 3 and stream.read() == b"abc"
    finally:
        stream.close()
    assert "/rest/api/3/attachment/content/123" in fetch.call_args_list[0].args[1]
    assert "Authorization" not in fetch.call_args_list[1].kwargs["headers"]


def test_content_escapes_text_and_never_emits_script_links():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "<script>",
                        "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}, {"type": "strong"}],
                    }
                ],
            }
        ],
    }
    assert adf_html(doc, CONFIG["site_url"], "ENG-1") == "<p><strong>&lt;script&gt;</strong></p>"


def test_unresolved_adf_media_reports_a_failure_instead_of_guessing():
    with pytest.raises(ConfluenceError, match="did not render embedded media"):
        adf_html({"type": "media", "attrs": {"id": "media-uuid"}}, CONFIG["site_url"], "ENG-1")


def test_current_sprint_prefers_active_over_future_and_history():
    values = [{"id": 20, "state": "closed"}, {"id": 10, "state": "active"}, {"id": 30, "state": "future"}]
    assert choose_sprint(values)["id"] == 10
    assert choose_sprint([values[0], {"id": 19, "state": "closed"}])["id"] == 20


def test_revision_ignores_transient_rendered_urls_but_detects_mapping_changes():
    base = {"id": "1", "fields": {"updated": "2026-10-02"}}
    assert revision({**base, "renderedFields": {"description": "token=first"}}) == revision(
        {**base, "renderedFields": {"description": "token=second"}}
    )
    assert revision(base) != revision(base, {"account-id": "plane-user"})


def test_jira_configuration_request_logs_redact_token(monkeypatch):
    from django.http import HttpResponse
    from django.test import RequestFactory
    from plane.middleware.logger import APITokenLogMiddleware

    enqueue = Mock()
    monkeypatch.setattr("plane.middleware.logger.process_logs.delay", enqueue)
    request = RequestFactory().patch(
        "/api/instances/jira/",
        data={"api_token": "do-not-log-jira-token"},
        content_type="application/json",
        HTTP_X_API_KEY="key",
    )
    APITokenLogMiddleware(lambda req: HttpResponse("{}"))(request)
    assert enqueue.call_count == 1
    assert "do-not-log-jira-token" not in str(enqueue.call_args.kwargs)
    assert enqueue.call_args.kwargs["log_data"]["body"] == "[Jira configuration redacted]"
