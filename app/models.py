"""SQLAlchemy models. One SQLite file, no migrations — schema is created on startup."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    cover_url: Mapped[str | None] = mapped_column(String(500))
    # File name inside data/covers/, set once the crawl has copied the image locally.
    cover_path: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    developer: Mapped[str | None] = mapped_column(String(200))
    publisher: Mapped[str | None] = mapped_column(String(200))
    release_date: Mapped[str | None] = mapped_column(String(20))
    genres: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: Closed-vocabulary tags from the LLM, see `app.llm.TAG_VOCABULARY`.
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Hash of the text the tags were derived from; they are rebuilt when it changes.
    tags_hash: Mapped[str | None] = mapped_column(String(32))
    video_url: Mapped[str | None] = mapped_column(String(500))
    metacritic_url: Mapped[str | None] = mapped_column(String(500))

    # Denormalised best-of across platforms, so the web list can sort without a join.
    best_metascore: Mapped[int | None] = mapped_column(Integer, index=True)
    best_userscore: Mapped[float | None] = mapped_column(Float, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    platforms: Mapped[list[Platform]] = relationship(
        back_populates="game", cascade="all, delete-orphan", lazy="selectin"
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    summaries: Mapped[list[Summary]] = relationship(
        back_populates="game", cascade="all, delete-orphan", lazy="selectin"
    )
    letsplay: Mapped[LetsPlay | None] = relationship(
        back_populates="game", cascade="all, delete-orphan", lazy="selectin", uselist=False
    )


class Platform(Base):
    __tablename__ = "platforms"
    __table_args__ = (UniqueConstraint("game_id", "name", name="uq_platform_game_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    metascore: Mapped[int | None] = mapped_column(Integer)
    userscore: Mapped[float | None] = mapped_column(Float)

    game: Mapped[Game] = relationship(back_populates="platforms")


def text_hash(text: str) -> str:
    """Identity of a review body, so re-crawls update instead of duplicating."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("game_id", "kind", "text_hash", name="uq_review_game_kind_text"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10), index=True)  # critic | user
    author: Mapped[str | None] = mapped_column(String(200))
    score: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(32))
    date: Mapped[str | None] = mapped_column(String(20))

    game: Mapped[Game] = relationship(back_populates="reviews")


class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (UniqueConstraint("game_id", "kind", name="uq_summary_game_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # critic | user
    likes: Mapped[list[str]] = mapped_column(JSON, default=list)
    dislikes: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(100))
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    game: Mapped[Game] = relationship(back_populates="summaries")


class LetsPlay(Base):
    """The most watched playthrough of a game and what its author thinks of it."""

    __tablename__ = "letsplays"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), unique=True, index=True
    )
    video_id: Mapped[str | None] = mapped_column(String(20))
    url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(300))
    channel: Mapped[str | None] = mapped_column(String(200))
    view_count: Mapped[int | None] = mapped_column(Integer)
    transcript_source: Mapped[str] = mapped_column(String(20), default="none")
    transcript_chars: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    error: Mapped[str | None] = mapped_column(Text)

    game: Mapped[Game] = relationship(back_populates="letsplay")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(50), default="scheduled")
    source: Mapped[str] = mapped_column(String(50))  # new_releases | browse:N
    planned: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[CrawlItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class CrawlItem(Base):
    __tablename__ = "crawl_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))  # ok | partial | failed
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[CrawlRun] = relationship(back_populates="items")


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    game_id: Mapped[int | None] = mapped_column(Integer, index=True)
    purpose: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)
    ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str | None] = mapped_column(Text)
