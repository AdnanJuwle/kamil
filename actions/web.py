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


def search_web(query: str, n: int = 5) -> list[dict]:
    """Return up to n results [{title, url, snippet}]. SearXNG if configured, else DDG."""
    query = (query or "").strip()
    if not query:
        return []
    n = max(1, min(int(n), 12))

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


def fetch_url(url: str, max_chars: int = 6000) -> dict:
    """Fetch a page and return its readable text (tags/scripts stripped)."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "url must start with http(s)://"}
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        html = r.text
    except Exception as e:
        return {"success": False, "error": str(e)}
    html = re.sub(r'(?is)<(script|style|noscript)\b[^>]*>.*?</\1>', ' ', html)
    text = re.sub(r'(?s)<[^>]+>', ' ', html)
    text = re.sub(r'&(?:nbsp|amp|lt|gt|quot|#\d+);', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return {"success": True, "url": url, "text": text[:max_chars],
            "chars": len(text), "truncated": len(text) > max_chars}
