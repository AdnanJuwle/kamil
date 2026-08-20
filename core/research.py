"""
core/research.py — deep research: iterative, LLM-in-the-loop web research.

Not one search — Kamil PLANS sub-queries, searches + reads pages for each, spots
gaps, searches again, then writes a grounded report. Adapted from Odysseus's
IterResearch-style DeepResearcher, pared to a simple round loop for Kamil.

Everything is grounded in fetched page CONTENT (search_and_read), and the final
report is written ONLY from findings — so it doesn't drift into fabrication.
"""
from __future__ import annotations

import re


def _queries_from(text: str, cap: int) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        q = re.sub(r"^\s*(\d+[.)]|[-*])\s*", "", line).strip().strip('"')
        if q and len(q) > 3 and not q.lower().startswith(("here", "sure", "these")):
            out.append(q)
    return out[:cap]


def deep_research(question: str, chat_fn, progress=None, rounds: int = 2,
                  per_round_queries: int = 3) -> dict:
    """Run the research loop. `chat_fn(messages, max_tokens)` -> str (an LLM call).
    `progress(str)` is called with human-readable step notes. Returns {report, findings, queries}."""
    from actions.web import search_and_read

    def note(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    findings: list[str] = []
    all_queries: list[str] = []
    seen_urls: set[str] = set()

    def run_queries(queries):
        for q in queries:
            note(f"searching: {q}")
            for r in search_and_read(q, n=4, read_top=2):
                url = r.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                body = (r.get("content") or r.get("snippet") or "").strip()
                if body:
                    findings.append(f"[q: {q}] {r.get('title','')}\n{body[:1400]}")

    # Round 1 — plan sub-queries from the question
    note("planning research")
    plan = chat_fn([
        {"role": "system", "content": "You are a research planner. Break the user's question into "
         f"{per_round_queries} specific, distinct web-search queries that together answer it. "
         "Output ONLY the queries, one per line, nothing else."},
        {"role": "user", "content": question},
    ], 200)
    q1 = _queries_from(plan, per_round_queries) or [question]
    all_queries += q1
    run_queries(q1)

    # Further rounds — find gaps from what we have
    for _ in range(max(0, rounds - 1)):
        if not findings:
            break
        note("looking for gaps")
        gap = chat_fn([
            {"role": "system", "content": "Given the research question and the findings so far, list up "
             f"to {per_round_queries} follow-up web-search queries for what's still MISSING or unverified. "
             "If the findings already answer it well, output the single word NONE. Queries only, one per line."},
            {"role": "user", "content": f"Question: {question}\n\nFindings so far:\n" + "\n\n".join(findings[:8])},
        ], 160)
        if "none" in gap.strip().lower()[:8]:
            break
        qn = [q for q in _queries_from(gap, per_round_queries) if q not in all_queries]
        if not qn:
            break
        all_queries += qn
        run_queries(qn)

    # Final report — grounded strictly in findings
    note("writing report")
    if not findings:
        return {"report": "I couldn't find reliable sources on that.", "findings": [], "queries": all_queries}
    report = chat_fn([
        {"role": "system", "content": "Write a clear, well-organized answer to the research question using "
         "ONLY the findings below. Be specific and cite concrete facts/figures. Note anything uncertain or "
         "conflicting. Do NOT invent details not present in the findings. Use short markdown sections."},
        {"role": "user", "content": f"Research question: {question}\n\nFindings:\n" + "\n\n".join(findings[:14])},
    ], 900)
    return {"report": (report or "").strip(), "findings": findings, "queries": all_queries}
