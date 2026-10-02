from unittest.mock import Mock

import pytest
from django.core.cache.backends.locmem import LocMemCache

from plane.utils.confluence.client import ConfluenceClient, ConfluenceError
from plane.utils.confluence.spaces import search_spaces

pytestmark = pytest.mark.unit
CONFIG = {"site_url": "https://team.atlassian.net", "email": "member@example.com", "api_token": "test-token"}


@pytest.fixture
def directory_cache(monkeypatch):
    local = LocMemCache("confluence-space-search-tests", {})
    local.clear()
    monkeypatch.setattr("plane.utils.confluence.spaces.cache", local)
    return local


def test_search_finds_names_and_keys_on_later_pages_and_reuses_directory(directory_cache):
    client = ConfluenceClient(CONFIG)
    client.json = Mock(
        side_effect=[
            {
                "results": [{"id": "1", "name": "First space", "key": "FIRST"}],
                "_links": {"next": "/wiki/api/v2/spaces?cursor=next"},
            },
            {
                "results": [
                    {"id": "2", "name": "Engineering Team", "key": "ENG"},
                    {"id": "3", "name": "พื้นที่ทีม", "key": "THAI"},
                ]
            },
        ]
    )
    result = search_spaces(client, "engineering")
    assert [row["id"] for row in result["results"]] == ["2"]
    assert result["count"] == 1 and result["next_cursor"] is None
    assert client.json.call_count == 2
    assert search_spaces(client, "eng")["results"][0]["id"] == "2"
    assert search_spaces(client, "ทีม")["results"][0]["id"] == "3"
    assert search_spaces(client, "not-found")["count"] == 0
    assert client.json.call_count == 2


def test_search_pagination_prioritizes_exact_keys_and_deduplicates(directory_cache):
    client = ConfluenceClient(CONFIG)
    rows = [{"id": str(i), "name": f"Engineering {i:03d}", "key": f"ENG{i}"} for i in range(101)]
    rows.append({"id": "500", "name": "Special exact key", "key": "ENG"})
    client.paginate = Mock(return_value=iter(rows + [rows[0]]))
    first = search_spaces(client, "eng")
    assert first["count"] == 102 and first["next_cursor"] == "100"
    assert first["results"][0]["id"] == "500"
    second = search_spaces(client, "eng", first["next_cursor"])
    assert len(second["results"]) == 2 and second["next_cursor"] is None
    assert not {row["id"] for row in first["results"]} & {row["id"] for row in second["results"]}
    assert client.paginate.call_count == 1


def test_failed_directory_read_is_not_cached_as_incomplete_results(directory_cache):
    client = ConfluenceClient(CONFIG)

    def broken_pages(*args):
        yield {"id": "1", "name": "Engineering", "key": "ENG"}
        raise ConfluenceError("atlassian_http_403", "Permission denied.")

    client.paginate = Mock(side_effect=broken_pages)
    for _ in range(2):
        with pytest.raises(ConfluenceError, match="Permission denied"):
            search_spaces(client, "eng")
    assert client.paginate.call_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"api_token": "rotated-token"},
        {"email": "other@example.com"},
        {"site_url": "https://other.atlassian.net"},
        {"cloud_id": "00000000-0000-4000-8000-000000000000"},
    ],
)
def test_directory_cache_is_separate_for_each_connection(directory_cache, change):
    first = ConfluenceClient(CONFIG)
    first.paginate = Mock(return_value=iter([{"id": "1", "name": "Engineering", "key": "ENG"}]))
    assert search_spaces(first, "eng")["count"] == 1
    other = ConfluenceClient({**CONFIG, **change})
    other.paginate = Mock(return_value=iter([]))
    assert search_spaces(other, "eng")["count"] == 0
    other.paginate.assert_called_once()


@pytest.mark.parametrize("cursor", ["bad", "-1", "1000000000"])
def test_invalid_search_cursor_does_not_read_atlassian(directory_cache, cursor):
    client = ConfluenceClient(CONFIG)
    client.paginate = Mock()
    with pytest.raises(ConfluenceError, match="Invalid search cursor"):
        search_spaces(client, "eng", cursor)
    client.paginate.assert_not_called()
