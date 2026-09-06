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
from app.scraper.http import PoliteClient, ScrapeError, default_client

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

#: Cache file names are `<slug>.<ext>` and nothing else — the serving route rejects
#: everything that does not match, so no request can escape the directory.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,199}\.(jpg|png|webp|gif|avif)$")


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
    except (ScrapeError, ValueError) as exc:
        log.warning("cover download failed for %s: %s", slug, exc)
        return existing

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    extension = EXTENSIONS.get(content_type)
    if extension is None:
        log.warning("cover for %s is not an image (%s)", slug, content_type or "no type")
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
