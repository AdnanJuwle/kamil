"""Tests for actions/web.py + the web_search/web_fetch tools (network mocked)."""
import actions.web as web
from actions import tools


def test_fetch_strips_html(monkeypatch):
    class _R:
        status_code = 200
        text = ("<html><head><style>x{}</style><script>bad()</script></head>"
                "<body><h1>Title</h1> <p>hello &amp; world</p></body></html>")
    monkeypatch.setattr(web.httpx, "get", lambda *a, **k: _R())
    r = web.fetch_url("https://example.com")
    assert r["success"] is True
    assert "Title" in r["text"] and "hello" in r["text"] and "world" in r["text"]
    assert "<h1>" not in r["text"] and "bad()" not in r["text"] and "x{}" not in r["text"]


def test_fetch_rejects_non_http():
    assert web.fetch_url("ftp://x/y")["success"] is False
    assert web.fetch_url("")["success"] is False


def test_search_empty_query_returns_nothing():
    assert web.search_web("") == []


def test_web_search_tool_no_results(monkeypatch):
    monkeypatch.setattr(web, "search_web", lambda *a, **k: [])
    r = tools.execute_tool("web_search", {"query": "x"})
    assert r.success is False


def test_web_search_tool_returns_results(monkeypatch):
    monkeypatch.setattr(web, "search_web",
                        lambda *a, **k: [{"title": "T", "url": "u", "snippet": "s"}])
    r = tools.execute_tool("web_search", {"query": "x"})
    assert r.success is True
    assert r.output["results"][0]["title"] == "T"


def test_web_tools_are_auto():
    assert tools.requires_confirmation("web_search") is False
    assert tools.requires_confirmation("web_fetch") is False
