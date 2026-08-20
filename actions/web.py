"""
actions/web.py — web search + page fetch for Kamil.

Privacy: this is the one place Kamil reaches the internet, and only the search
query / URL leaves — never Adnan's data, no account, no API key. Backend priority:
  1. SearXNG (KAMIL_SEARXNG_URL) — a self-hosted metasearch proxy, the most private
     option (your instance queries the engines, nothing tracks you).
  2. DuckDuckGo (default) — no key, no account, privacy-respecting.
"""

import os
import re

import httpx

SEARXNG_URL = os.getenv("KAMIL_SEARXNG_URL")   # e.g. http://localhost:8888
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _int(v, default: int) -> int:
    """Coerce a tool-supplied numeric arg to int. The model often passes numbers as
    strings ('6000') or junk ('full'); a raw string in text[:max_chars] crashes with
    'slice indices must be integers'. Fall back to the default instead."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def search_web(query: str, n: int = 5) -> list[dict]:
    """Return up to n results [{title, url, snippet}]. SearXNG if configured, else DDG."""
    query = (query or "").strip()
    if not query:
        return []
    n = max(1, min(_int(n, 5), 12))

    if SEARXNG_URL:
        try:
            r = httpx.get(f"{SEARXNG_URL.rstrip('/')}/search",
                          params={"q": query, "format": "json"},
                          headers={"User-Agent": _UA}, timeout=15)
            data = r.json()
            out = [{"title": x.get("title", ""), "url": x.get("url", ""),
                    "snippet": x.get("content", "")} for x in data.get("results", [])[:n]]
            if out:
                return out
        except Exception as e:
            print(f"[web] searxng failed ({e}); falling back to DuckDuckGo")

    try:
        from ddgs import DDGS
        out = []
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=n):
                out.append({"title": r.get("title", ""),
                            "url": r.get("href") or r.get("url", ""),
                            "snippet": r.get("body", "")})
        return out
    except Exception as e:
        print(f"[web] search failed: {e}")
        return []


def search_and_read(query: str, n: int = 5, read_top: int = 2, per_page_chars: int = 2200) -> list[dict]:
    """Search, then FETCH the actual page content for the top results (not just the
    snippet) so an answer can be grounded in real text. (Ported idea from Odysseus's
    search service, which fetches + ranks content; Kamil only had snippets.) Each of
    the top `read_top` results gets a 'content' field with readable page text."""
    read_top = _int(read_top, 2)
    per_page_chars = _int(per_page_chars, 2200)
    results = search_web(query, n)
    for r in results[:read_top]:
        url = r.get("url", "")
        if not url:
            continue
        try:
            page = fetch_url(url, max_chars=per_page_chars)
            if page.get("success") and page.get("text"):
                r["content"] = page["text"]
        except Exception:
            pass
    return results


def fetch_url(url: str, max_chars: int = 6000) -> dict:
    """Fetch a page and return its readable text (tags/scripts stripped)."""
    url = (url or "").strip()
    max_chars = _int(max_chars, 6000)
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "url must start with http(s)://"}
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
    except Exception as e:
        return {"success": False, "error": str(e)}
    # An HTTP error page (404/403/...) still has a body; without this the model reads a
    # GitHub "404" page as if it were real content and keeps guessing more URLs.
    if r.status_code >= 400:
        return {"success": False, "url": url, "status": r.status_code,
                "error": f"HTTP {r.status_code} for {url} — the page does not exist or is not accessible"}
    html = r.text
    html = re.sub(r'(?is)<(script|style|noscript)\b[^>]*>.*?</\1>', ' ', html)
    text = re.sub(r'(?s)<[^>]+>', ' ', html)
    text = re.sub(r'&(?:nbsp|amp|lt|gt|quot|#\d+);', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return {"success": True, "url": url, "text": text[:max_chars],
            "chars": len(text), "truncated": len(text) > max_chars}
