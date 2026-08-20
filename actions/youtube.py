"""
actions/youtube.py — pull a YouTube transcript so Kamil can summarize/discuss a video.

Uses youtube-transcript-api (lightweight, no yt-dlp). Only the video id leaves the
machine (the caption request); nothing of Adnan's. Adapted from Odysseus's youtube
handler. The agent calls the `youtube` tool, then answers from the transcript.
"""
import re

_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str | None:
    """Extract the 11-char video id from any YouTube URL (or a bare id)."""
    u = (url or "").strip()
    m = _ID_RE.search(u)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", u):
        return u
    return None


def get_transcript(url: str, max_chars: int = 9000) -> dict:
    """Return {success, video_id, text, chars} for a video's transcript."""
    vid = video_id(url)
    if not vid:
        return {"success": False, "error": "that doesn't look like a YouTube link"}
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        fetched = YouTubeTranscriptApi().fetch(vid)   # iterable of snippets (.text)
        text = " ".join(getattr(s, "text", "") for s in fetched).strip()
        text = re.sub(r"\s+", " ", text)
    except Exception as e:
        return {"success": False, "error": f"no transcript available ({str(e)[:80]})"}
    if not text:
        return {"success": False, "error": "no transcript available for this video"}
    return {"success": True, "video_id": vid, "text": text[:max_chars],
            "chars": len(text), "truncated": len(text) > max_chars}
