"""Scraper for metacritic.com game pages.

Metacritic serves Nuxt-rendered HTML: every page carries the data its components were
rendered from in a `__NUXT_DATA__` devalue payload. We decode that payload rather than
scraping the DOM, because the markup is generated Tailwind soup while the payload is
the site's own typed model.

Reviews additionally have a JSON backend (the same one the page calls for pagination);
we use it when it answers and fall back to the SSR page otherwise.

Parsing is kept separate from fetching (`parse_*` vs `fetch_*`) so the tests can run
against saved fixtures without touching the network.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from selectolax.parser import HTMLParser

from app.config import settings
from app.scraper.devalue import extract_nuxt_payload
from app.scraper.http import PoliteClient, ScrapeError, default_client

log = logging.getLogger(__name__)

ReviewKind = Literal["critic", "user"]

NEW_RELEASES_COUNT = 20

_JSON_LD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)
_SLUG_RE = re.compile(r"^/game/([a-z0-9\-]+)/?$")


# --------------------------------------------------------------------------- models


@dataclass
class PlatformScore:
    """One platform release of a game. Either score may be missing."""

    name: str
    slug: str | None = None
    metascore: int | None = None
    userscore: float | None = None


@dataclass
class GameData:
    title: str
    slug: str
    metacritic_url: str
    cover_url: str | None = None
    description: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release_date: str | None = None
    genres: list[str] = field(default_factory=list)
    platforms: list[PlatformScore] = field(default_factory=list)
    video_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Review:
    text: str
    score: float | None
    author: str | None
    date: str | None
    kind: ReviewKind = "critic"
    platform: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------- payload helpers


def _page_components(html: str) -> list[dict[str, Any]]:
    """Components of the page's own `loadPage:...` entry (skipping the site skeleton)."""
    payload = extract_nuxt_payload(html)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ScrapeError("nuxt payload has no `data` section")
    for key, node in data.items():
        if not key.startswith("loadPage:") or key.startswith("loadPage:skeleton"):
            continue
        if isinstance(node, dict) and isinstance(node.get("components"), list):
            return node["components"]
    raise ScrapeError("no page components found in nuxt payload")


def _component(components: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for component in components:
        meta = component.get("meta") or {}
        if meta.get("componentName") == name:
            return component.get("data") or {}
    return None


def _json_ld(html: str) -> dict[str, Any]:
    """First JSON-LD block on the page, or `{}`."""
    for match in _JSON_LD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _slugs_from_links(nodes) -> list[str]:
    """Slugs of `/game/<slug>/` links, de-duplicated, in document order."""
    slugs: list[str] = []
    for node in nodes:
        match = _SLUG_RE.match(node.attributes.get("href", "") or "")
        if match and match.group(1) not in slugs:
            slugs.append(match.group(1))
    return slugs


# ------------------------------------------------------------------------- listings


def parse_new_releases(html: str, limit: int = NEW_RELEASES_COUNT) -> list[str]:
    """Slugs of the "New Releases" carousel on https://www.metacritic.com/game/."""
    try:
        data = _component(_page_components(html), "new-releases-carousel")
    except ScrapeError:
        data = None
    if data and isinstance(data.get("items"), list):
        slugs = [item["slug"] for item in data["items"] if item.get("slug")]
        if slugs:
            return slugs[:limit]

    log.warning("new-releases-carousel missing from payload, falling back to markup")
    carousel = HTMLParser(html).css_first('[data-testid="new-game-release-carousel"]')
    if carousel is None:
        raise ScrapeError("New Releases section not found on the games front door")
    return _slugs_from_links(carousel.css('a[href^="/game/"]'))[:limit]


def parse_browse_page(html: str) -> list[str]:
    """Slugs of the result cards on a /browse/game/... page."""
    tree = HTMLParser(html)
    slugs: list[str] = []
    for card in tree.css('[data-testid="filter-results"]'):
        slugs.extend(s for s in _slugs_from_links(card.css('a[href^="/game/"]')) if s not in slugs)
    if not slugs:
        raise ScrapeError("no result cards found on browse page")
    return slugs


def fetch_new_releases(
    limit: int = NEW_RELEASES_COUNT, client: PoliteClient | None = None
) -> list[str]:
    """First `limit` slugs from the New Releases section of the Games front door."""
    client = client or default_client()
    html = client.get_text(f"{settings.metacritic_base_url}/game/")
    return parse_new_releases(html, limit=limit)


def fetch_browse_new(page: int = 1, client: PoliteClient | None = None) -> list[str]:
    """Slugs from page `page` of "SEE ALL / New" (24 games per page)."""
    if page < 1:
        raise ValueError("page is 1-based")
    client = client or default_client()
    url = f"{settings.metacritic_base_url}/browse/game/all/all/all-time/new/"
    html = client.get_text(url, params={"page": page})
    return parse_browse_page(html)


# ---------------------------------------------------------------------- game details


def _cover_url(html: str, images: list[dict[str, Any]], json_ld: dict[str, Any]) -> str | None:
    """Best cover image: the portrait card art if the page has one, else the hero shot.

    Metacritic's `<img>` tags point at signed, resized derivatives; we reuse those when
    we can find the matching one, and otherwise fall back to the original in the asset
    bucket (which is served unsigned).
    """
    by_type = {img.get("typeName"): img for img in images if isinstance(img, dict)}
    image = by_type.get("cardImage") or by_type.get("mainImage") or (images[0] if images else None)
    if not isinstance(image, dict):
        return json_ld.get("image") or None

    filename = image.get("filename")
    if filename:
        signed = re.search(
            r"https://www\.metacritic\.com/a/img/resize/[0-9a-f]+/[^\"'\s]*?"
            + re.escape(filename),
            html,
        )
        if signed:
            return signed.group(0)

    bucket_type, bucket_path = image.get("bucketType"), image.get("bucketPath")
    if bucket_type and bucket_path:
        return f"{settings.metacritic_base_url}/a/img/{bucket_type}{bucket_path}"
    return json_ld.get("image") or None


def _company(companies: list[dict[str, Any]], type_name: str) -> str | None:
    for company in companies:
        if isinstance(company, dict) and company.get("typeName") == type_name:
            return company.get("name")
    return None


def _video_url(item: dict[str, Any], json_ld: dict[str, Any]) -> str | None:
    video = item.get("video")
    if isinstance(video, dict):
        for key in ("embedUrl", "url", "manifestUrl"):
            if video.get(key):
                return video[key]
    trailer = json_ld.get("trailer")
    if isinstance(trailer, dict):
        for key in ("embedUrl", "contentUrl", "url"):
            if trailer.get(key):
                return trailer[key]
    return None


def _score(summary: Any) -> Any:
    return summary.get("score") if isinstance(summary, dict) else None


def parse_game(html: str, slug: str) -> GameData:
    """Build :class:`GameData` from a saved `/game/<slug>/` page.

    Per-platform *user* scores are not on this page — see
    :func:`fetch_platform_user_score`; here they stay `None` except for the platform
    the page itself is about.
    """
    components = _page_components(html)
    data = _component(components, "product")
    if not data or not isinstance(data.get("item"), dict):
        raise ScrapeError(f"no product component on the page for {slug!r}")
    item = data["item"]
    json_ld = _json_ld(html)

    production = item.get("production") or {}
    companies = production.get("companies") or []

    platforms = [
        PlatformScore(
            name=platform.get("name") or "",
            slug=platform.get("slug"),
            metascore=_score(platform.get("criticScoreSummary")),
        )
        for platform in item.get("platforms") or []
        if isinstance(platform, dict) and platform.get("name")
    ]

    # The page is rendered for one lead platform; its user score is in the payload.
    lead_summary = _component(components, "user-score-summary") or {}
    lead_user_score = _score(lead_summary.get("item"))
    lead_name = item.get("platform")
    for platform in platforms:
        if platform.name == lead_name:
            platform.userscore = lead_user_score

    return GameData(
        title=item.get("title") or "",
        slug=item.get("slug") or slug,
        metacritic_url=f"{settings.metacritic_base_url}/game/{item.get('slug') or slug}/",
        cover_url=_cover_url(html, item.get("images") or [], json_ld),
        description=item.get("description") or json_ld.get("description") or None,
        developer=_company(companies, "Developer"),
        publisher=_company(companies, "Publisher"),
        release_date=item.get("releaseDate") or item.get("releaseDateText"),
        genres=[
            g["name"] for g in item.get("genres") or [] if isinstance(g, dict) and g.get("name")
        ],
        platforms=platforms,
        video_url=_video_url(item, json_ld),
    )


def parse_platform_user_score(source: str | dict[str, Any]) -> float | None:
    """User score out of a `/user-reviews/?platform=...` page or its API response."""
    if isinstance(source, str):
        components = _page_components(source)
    else:
        components = source.get("components") or []
    return _score((_component(components, "user-score-summary") or {}).get("item"))


def fetch_platform_user_score(
    slug: str, platform_slug: str, client: PoliteClient | None = None
) -> float | None:
    """User score for one platform release, via the JSON backend or the SSR page."""
    client = client or default_client()
    api_url = (
        f"{settings.metacritic_api_url}/composer/metacritic/pages/"
        f"games-user-reviews/{slug}/platform/{platform_slug}/web"
    )
    try:
        return parse_platform_user_score(
            client.get_json(api_url, params={"apiKey": settings.metacritic_api_key})
        )
    except (ScrapeError, ValueError) as exc:
        log.warning("user-score API failed for %s/%s: %s", slug, platform_slug, exc)

    html = client.get_text(
        f"{settings.metacritic_base_url}/game/{slug}/user-reviews/",
        params={"platform": platform_slug},
    )
    return parse_platform_user_score(html)


def fetch_game(
    slug: str,
    client: PoliteClient | None = None,
    *,
    platform_user_scores: bool = True,
) -> GameData:
    """Full card for one game.

    With `platform_user_scores` (the default) each platform costs one extra request,
    because Metacritic only renders the user score of the platform being viewed.
    """
    client = client or default_client()
    html = client.get_text(f"{settings.metacritic_base_url}/game/{slug}/")
    game = parse_game(html, slug)

    if platform_user_scores:
        for platform in game.platforms:
            if platform.userscore is not None or not platform.slug:
                continue
            try:
                platform.userscore = fetch_platform_user_score(slug, platform.slug, client)
            except (ScrapeError, ValueError) as exc:
                log.warning("no user score for %s on %s: %s", slug, platform.slug, exc)
    return game


# --------------------------------------------------------------------------- reviews


def _review_from_item(item: dict[str, Any], kind: ReviewKind) -> Review | None:
    text = (item.get("quote") or "").strip()
    if not text:
        return None
    if kind == "critic":
        author = item.get("publicationName") or item.get("author") or None
    else:
        author = item.get("author") or None
    return Review(
        text=text,
        score=item.get("score"),
        author=author,
        date=item.get("date"),
        kind=kind,
        platform=item.get("platform"),
        url=item.get("url"),
    )


def _reviews_from_items(items: Any, kind: ReviewKind, limit: int) -> list[Review]:
    if not isinstance(items, list):
        return []
    reviews = (_review_from_item(i, kind) for i in items if isinstance(i, dict))
    return [r for r in reviews if r is not None][:limit]


def parse_reviews_page(html: str, kind: ReviewKind, limit: int = 40) -> list[Review]:
    """Reviews from a saved `/critic-reviews/` or `/user-reviews/` page."""
    data = _component(_page_components(html), f"{kind}-reviews")
    if data is None:
        raise ScrapeError(f"no {kind}-reviews component on the page")
    return _reviews_from_items(data.get("items"), kind, limit)


def parse_reviews_api(payload: dict[str, Any], kind: ReviewKind, limit: int = 40) -> list[Review]:
    """Reviews from a backend.metacritic.com reviews response."""
    return _reviews_from_items((payload.get("data") or {}).get("items"), kind, limit)


def fetch_reviews(
    slug: str,
    kind: ReviewKind = "critic",
    limit: int = 40,
    client: PoliteClient | None = None,
) -> list[Review]:
    """Up to `limit` reviews of one kind.

    Prefers the JSON backend (the SSR page only ships the first batch) and falls back
    to the page itself.
    """
    if kind not in ("critic", "user"):
        raise ValueError(f"kind must be 'critic' or 'user', got {kind!r}")
    client = client or default_client()

    api_url = f"{settings.metacritic_api_url}/reviews/metacritic/{kind}/games/{slug}/web"
    try:
        payload = client.get_json(
            api_url,
            params={
                "apiKey": settings.metacritic_api_key,
                "offset": 0,
                "limit": limit,
                "filterBySentiment": "all",
                "sort": "score" if kind == "critic" else "date",
            },
        )
        reviews = parse_reviews_api(payload, kind, limit)
        if reviews:
            return reviews
        log.warning("reviews API returned nothing for %s/%s, falling back to HTML", slug, kind)
    except (ScrapeError, ValueError) as exc:
        log.warning("reviews API failed for %s/%s: %s", slug, kind, exc)

    html = client.get_text(f"{settings.metacritic_base_url}/game/{slug}/{kind}-reviews/")
    return parse_reviews_page(html, kind, limit)


# ------------------------------------------------------------------------------ cli


def _main(argv: list[str]) -> int:
    """`python -m app.scraper.metacritic [slug ...]` — live check against the site."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    client = default_client()

    slugs = argv[1:]
    if not slugs:
        slugs = fetch_new_releases(client=client)[:3]
        print(f"# New Releases (first 3 of {NEW_RELEASES_COUNT}): {', '.join(slugs)}\n")

    for slug in slugs:
        game = fetch_game(slug, client=client)
        out = game.to_dict()
        out["critic_reviews"] = [r.to_dict() for r in fetch_reviews(slug, "critic", 3, client)]
        out["user_reviews"] = [r.to_dict() for r in fetch_reviews(slug, "user", 3, client)]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv))
