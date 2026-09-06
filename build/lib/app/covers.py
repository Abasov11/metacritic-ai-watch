"""Local cache of game cover art.

Metacritic serves the originals fine, but hotlinking them from our own pages is both
rude and fragile, so a crawl copies each cover into `data/covers/<slug>.<ext>` and the
site serves it from there.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.scraper.http import PoliteClient, default_client

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_AGE = timedelta(days=30)

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}

#: Leading bytes -> extension. Metacritic mislabels some covers (a PNG served as
#: image/jpeg), so the file itself decides, not the header.
MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

#: Legacy rows hold a signed `/a/img/resize/<hash>/…` URL that now answers 403.
_SIGNED_RE = re.compile(r"(/a/img/)resize/[0-9a-f]+/")

#: Cache file names are `<slug>.<ext>` and nothing else — the serving route rejects
#: everything that does not match, so no request can escape the directory.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,199}\.(jpg|png|webp|gif|avif)$")


def sniff_extension(body: bytes, content_type: str) -> str | None:
    """Extension for the image, from its magic bytes; the header is only a fallback."""
    for prefix, extension in MAGIC:
        if body.startswith(prefix):
            return extension
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    if body[4:12] == b"ftypavif":
        return ".avif"
    return EXTENSIONS.get(content_type)


def unsigned_url(url: str | None) -> str | None:
    """Rewrite a signed Metacritic image URL to the unsigned original, and drop the
    resize query string that went with it."""
    if not url:
        return url
    return _SIGNED_RE.sub(r"\1", url.split("?", 1)[0])


def covers_dir() -> Path:
    return settings.data_dir / "covers"


def stored_name(slug: str) -> str | None:
    """File name already cached for this slug, if any."""
    for extension in dict.fromkeys(EXTENSIONS.values()):
        if (covers_dir() / f"{slug}{extension}").exists():
            return f"{slug}{extension}"
    return None


def path_for(name: str) -> Path | None:
    """Validated path for a request to `/covers/<name>`, or None if it is not ours."""
    if not NAME_RE.match(name):
        return None
    path = covers_dir() / name
    return path if path.is_file() else None


def is_fresh(name: str) -> bool:
    path = covers_dir() / name
    if not path.is_file():
        return False
    age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return age < MAX_AGE


def cache_cover(
    slug: str, url: str | None, client: PoliteClient | None = None, force: bool = False
) -> str | None:
    """Download the cover unless a fresh copy is on disk. Returns the file name."""
    existing = stored_name(slug)
    if existing and not force and is_fresh(existing):
        return existing
    if not url:
        return existing

    try:
        response = (client or default_client()).get(url)
    except Exception as exc:
        # Any failure here — 403, timeout, DNS — is cosmetic. It must never take a
        # crawl down with it, so nothing escapes.
        log.warning("cover download failed for %s: %s", slug, exc)
        return existing

    # Trust the advertised length to bail out early, then check what actually arrived.
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        log.warning("cover for %s is %s bytes, over the limit", slug, declared)
        return existing
    body = response.content
    if len(body) > MAX_BYTES:
        log.warning("cover for %s is %d bytes, over the limit", slug, len(body))
        return existing

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    extension = sniff_extension(body, content_type)
    if extension is None:
        log.warning("cover for %s is not an image (%s)", slug, content_type or "no type")
        return existing

    directory = covers_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{slug}{extension}"
    # Write beside the target and rename, so a half-written file is never served.
    temporary = directory / f".{name}.part"
    temporary.write_bytes(body)
    temporary.replace(directory / name)
    if existing and existing != name:
        (directory / existing).unlink(missing_ok=True)
    log.info("cached cover %s (%d bytes)", name, len(body))
    return name
