"""
core/skill_store.py — Kamil's PROCEDURAL memory (learned how-to skills).

Distinct from the domain-doc library (that's knowledge/RAG). A *skill* here is a
short playbook for a recurring TASK: when to use it, the steps, pitfalls, and how to
verify success. Kamil retrieves the relevant ones before acting, and can LEARN new
ones. Stored as data/procedures/<slug>/SKILL.md with YAML-ish frontmatter.

Design adapted from Odysseus's skills service (SKILL.md: When to Use / Procedure /
Pitfalls / Verification), pared down for Kamil's single-user, local setup — no owners,
no DB, keyword retrieval (no embedding dependency).
"""
from __future__ import annotations

import os
import re
import time

from core.config import DATA_DIR

SKILLS_DIR = os.path.join(DATA_DIR, "procedures")
_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "it",
         "how", "do", "i", "you", "my", "me", "with", "that", "this", "can", "please"}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "skill"


def _path(slug: str) -> str:
    return os.path.join(SKILLS_DIR, slug, "SKILL.md")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def save_skill(name: str, when_to_use: str, procedure: str, pitfalls: str = "",
               verification: str = "", tags=None, source: str = "taught") -> dict:
    """Create/overwrite a skill. `procedure` is the numbered steps (or free text)."""
    slug = _slug(name)
    tags = tags or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    os.makedirs(os.path.dirname(_path(slug)), exist_ok=True)
    desc = (when_to_use or "").strip().split("\n")[0][:120]
    body = [
        "---",
        f"name: {name}",
        f"description: {desc}",
        f"tags: [{', '.join(tags)}]",
        f"source: {source}",
        "uses: 0",
        f"created: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "---",
        "## When to Use",
        (when_to_use or "").strip(),
        "",
        "## Procedure",
        (procedure or "").strip(),
    ]
    if pitfalls.strip():
        body += ["", "## Pitfalls", pitfalls.strip()]
    if verification.strip():
        body += ["", "## Verification", verification.strip()]
    with open(_path(slug), "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    return {"slug": slug, "name": name}


def _parse(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    slug = os.path.basename(os.path.dirname(path))
    return {"slug": slug, "name": meta.get("name", slug),
            "description": meta.get("description", ""),
            "tags": [t.strip() for t in meta.get("tags", "").strip("[]").split(",") if t.strip()],
            "source": meta.get("source", ""), "uses": int(meta.get("uses", "0") or 0),
            "body": body.strip()}


def list_skills() -> list[dict]:
    out = []
    if os.path.isdir(SKILLS_DIR):
        for d in sorted(os.listdir(SKILLS_DIR)):
            p = _path(d)
            if os.path.isfile(p):
                s = _parse(p)
                if s:
                    out.append(s)
    return out


def get_skill(slug: str) -> dict | None:
    return _parse(_path(_slug(slug)))


def retrieve(query: str, k: int = 3) -> list[dict]:
    """Top-k skills whose name/description/tags/when-to-use overlap the query."""
    qw = _words(query)
    if not qw:
        return []
    scored = []
    for s in list_skills():
        text = " ".join([s["name"], s["description"], " ".join(s["tags"]), s["body"][:400]])
        overlap = len(qw & _words(text))
        if overlap:
            scored.append((overlap, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:k]]


def mark_used(slug: str) -> None:
    p = _path(_slug(slug))
    s = _parse(p)
    if not s:
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"(?m)^uses:\s*\d+", f"uses: {s['uses'] + 1}", text, count=1)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def delete_skill(slug: str) -> bool:
    p = _path(_slug(slug))
    try:
        if os.path.isfile(p):
            os.remove(p)
            d = os.path.dirname(p)
            if not os.listdir(d):
                os.rmdir(d)
            return True
    except Exception:
        pass
    return False
