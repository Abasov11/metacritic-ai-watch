"""Let's-play lookup: find the most watched playthrough, read what the blogger says.

Search runs through yt-dlp's flat extractor (one request, and it already carries view
counts). The spoken text comes from YouTube's own captions; only when there are none
do we fall back to downloading the audio and running Whisper locally.

Both of those last steps need full video extraction, which YouTube refuses from
datacenter IPs ("Sign in to confirm you're not a bot"). Point `YOUTUBE_COOKIES_FILE`
at an exported cookie jar to lift that; without it the stage records `none` and the
crawl carries on.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from app.config import settings
from app.llm import chat_json

log = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 12_000
TRANSCRIPT_LANGUAGES = ("en", "ru")

#: Titles that are clearly not a playthrough.
NOT_A_LETSPLAY = re.compile(
    r"\b(trailer|teaser|announce\w*|reveal|review|обзор|трейлер|рецензи\w+|"
    r"ost|soundtrack|music|подборка|top\s*\d+|все\s+концовки|all\s+endings)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


class YouTubeError(RuntimeError):
    pass


@dataclass
class Video:
    video_id: str
    url: str
    title: str
    channel: str | None
    view_count: int
    duration: int

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------- search


def _title_words(title: str) -> set[str]:
    """Distinctive words of a game title — short ones match everything."""
    return {w.lower() for w in _WORD_RE.findall(title) if len(w) > 2}


def is_letsplay(entry: dict, game_title: str) -> bool:
    """Long enough, about this game, and not a trailer or a review."""
    if (entry.get("duration") or 0) < settings.youtube_min_duration:
        return False
    title = entry.get("title") or ""
    if NOT_A_LETSPLAY.search(title):
        return False
    wanted = _title_words(game_title)
    if not wanted:
        return False
    haystack = _title_words(f"{title} {entry.get('description') or ''}")
    # Most of the title has to show up; sequels and subtitles get dropped otherwise.
    return len(wanted & haystack) / len(wanted) >= 0.6


def _ytsearch(query: str) -> list[dict]:
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noprogress": True,
        "socket_timeout": 20,
    }
    if settings.youtube_cookies_file:
        options["cookiefile"] = settings.youtube_cookies_file
    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(query, download=False) or {}
    return [e for e in (result.get("entries") or []) if isinstance(e, dict)]


def search_letsplay(game_title: str) -> Video | None:
    """Most watched playthrough of the game, or None if the search finds nothing."""
    query = f"ytsearch{settings.youtube_search_count}:{game_title} let's play"
    try:
        entries = _ytsearch(query)
    except Exception as exc:
        raise YouTubeError(f"search failed: {exc}") from exc

    candidates = [e for e in entries if is_letsplay(e, game_title)]
    if not candidates:
        return None
    best = max(candidates, key=lambda e: e.get("view_count") or 0)
    return Video(
        video_id=best["id"],
        url=best.get("url") or f"https://www.youtube.com/watch?v={best['id']}",
        title=best.get("title") or "",
        channel=best.get("channel") or best.get("uploader"),
        view_count=best.get("view_count") or 0,
        duration=best.get("duration") or 0,
    )


# --------------------------------------------------------------------- transcript


def _cookie_session():
    """A requests session carrying the operator's YouTube cookies, if configured."""
    import requests

    session = requests.Session()
    path = settings.youtube_cookies_file
    if path and Path(path).is_file():
        jar = MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = jar
    return session


def fetch_subtitles(video_id: str) -> str:
    """Caption text, auto-generated included. Empty string when there are none."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi(http_client=_cookie_session())
    fetched = api.fetch(video_id, languages=list(TRANSCRIPT_LANGUAGES))
    return " ".join(snippet.text.strip() for snippet in fetched if snippet.text.strip())


def whisper_is_affordable() -> tuple[bool, str]:
    """Whether transcribing locally is worth attempting right now."""
    if not settings.youtube_whisper_enabled:
        return False, "whisper disabled"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "faster-whisper is not installed"
    try:
        fields = dict(
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
        )
        free_mb = int(fields["MemAvailable"].split()[0]) // 1024
    except Exception:
        return False, "cannot read available memory"
    if free_mb < settings.youtube_whisper_min_free_mb:
        return False, f"only {free_mb} MB free, need {settings.youtube_whisper_min_free_mb}"
    return True, f"{free_mb} MB free"


def transcribe_audio(video_id: str, seconds_left: float) -> str:
    """Download the opening minutes of audio and run Whisper over it."""
    import tempfile

    import yt_dlp
    from faster_whisper import WhisperModel

    span = min(settings.youtube_audio_seconds, max(int(seconds_left), 0))
    if span < 60:
        raise YouTubeError("not enough time left for transcription")

    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir) / "audio.%(ext)s"
        options = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "format": "bestaudio/best",
            "outtmpl": str(target),
            "socket_timeout": 30,
            # Only the opening stretch: enough to hear the verdict, cheap to fetch.
            "download_ranges": yt_dlp.utils.download_range_func(None, [(0, span)]),
            "force_keyframes_at_cuts": False,
        }
        if settings.youtube_cookies_file:
            options["cookiefile"] = settings.youtube_cookies_file
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        files = [p for p in Path(workdir).iterdir() if p.is_file() and p.stat().st_size]
        if not files:
            raise YouTubeError("no audio downloaded")

        model = WhisperModel(settings.youtube_whisper_model, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(files[0]), vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


def get_transcript(video_id: str, seconds_left: float) -> tuple[str, str, str | None]:
    """Returns (text, source, error). `source` is subtitles | whisper | none."""
    try:
        text = fetch_subtitles(video_id)
        if text:
            return text, "subtitles", None
        subtitle_error = "no captions for this video"
    except Exception as exc:
        subtitle_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        log.info("no subtitles for %s (%s)", video_id, subtitle_error)

    affordable, why = whisper_is_affordable()
    if not affordable:
        return "", "none", f"{subtitle_error}; whisper skipped ({why})"
    try:
        text = transcribe_audio(video_id, seconds_left)
    except Exception as exc:
        return "", "none", f"{subtitle_error}; whisper failed: {str(exc).splitlines()[0][:200]}"
    if not text:
        return "", "none", f"{subtitle_error}; whisper produced nothing"
    return text, "whisper", None


def trim(text: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Keep the opening and a chunk from the middle — that is where the verdict lives."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    middle = (len(text) - tail) // 2
    return f"{text[:head]}\n[…]\n{text[middle : middle + tail]}"


# ---------------------------------------------------------------------- the verdict


VERDICT_SCHEMA = '{"verdict": "строка", "highlights": ["строка"]}'


def summarize_letsplay(game_title: str, video: Video, text: str, game_id: int | None) -> dict:
    prompt = (
        f"Игра: {game_title}\n"
        f"Летсплей: «{video.title}» — канал {video.channel or 'неизвестен'}, "
        f"{video.view_count} просмотров.\n\n"
        "Ниже расшифровка речи блогера из этого ролика.\n\n"
        f"{trim(text)}\n\n"
        "Сделай заключение на русском языке. В verdict — 2-4 предложения: общее "
        "впечатление блогера от игры, что он хвалит и что ругает. В highlights — 3-5 "
        "коротких пунктов с конкретными моментами из ролика. Опирайся только на "
        "расшифровку; если блогер о чём-то не говорит, не выдумывай."
    )
    data = chat_json(prompt, VERDICT_SCHEMA, purpose="letsplay", game_id=game_id)
    return {
        "verdict": str(data.get("verdict") or ""),
        "highlights": [str(x) for x in data.get("highlights") or []],
        "model": data.get("_model"),
    }


# ------------------------------------------------------------------------ the stage


def build_letsplay(game_title: str, game_id: int | None, budget: float | None = None) -> dict:
    """Everything for one game, as a dict of `LetsPlay` column values.

    Never raises: a missing let's play is a missing feature, not a failed crawl.
    """
    deadline = time.monotonic() + (budget or settings.youtube_timeout)
    record: dict = {
        "video_id": None, "url": None, "title": None, "channel": None,
        "view_count": None, "transcript_source": "none", "transcript_chars": 0,
        "verdict": {}, "model": None, "error": None,
    }
    try:
        video = search_letsplay(game_title)
    except YouTubeError as exc:
        record["error"] = str(exc)[:500]
        return record
    if video is None:
        record["error"] = "подходящий летсплей не найден"
        return record

    record |= {
        "video_id": video.video_id, "url": video.url, "title": video.title,
        "channel": video.channel, "view_count": video.view_count,
    }

    text, source, error = get_transcript(video.video_id, deadline - time.monotonic())
    record["transcript_source"] = source
    record["transcript_chars"] = len(text)
    if not text:
        record["error"] = (error or "нет расшифровки")[:500]
        return record

    if time.monotonic() >= deadline:
        record["error"] = "не уложились в отведённое время до вызова модели"
        return record
    try:
        result = summarize_letsplay(game_title, video, text, game_id)
    except Exception as exc:
        record["error"] = f"LLM: {str(exc)[:400]}"
        return record

    record["model"] = result.pop("model")
    record["verdict"] = result
    return record
