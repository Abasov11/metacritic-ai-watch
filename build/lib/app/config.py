"""Application settings, loaded from environment / `.env`."""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage ---
    #: SQLite file and cached covers live here. Override in a container so the
    #: writable volume, not the read-only install directory, holds the state.
    data_dir: Path = BASE_DIR / "data"
    database_url: str = ""  # defaults to <data_dir>/app.db, see below

    # --- scraping ---
    metacritic_base_url: str = "https://www.metacritic.com"
    metacritic_api_url: str = "https://backend.metacritic.com"
    # Public key that metacritic.com itself ships in its HTML for the JSON backend.
    metacritic_api_key: str = "1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    request_timeout: float = 20.0
    request_retries: int = 3
    request_delay: float = 1.0  # minimum seconds between two outgoing requests

    # --- crawler ---
    tz: str = "Europe/Moscow"
    crawl_interval_minutes: int = 60
    crawl_batch_size: int = 20
    reviews_per_kind: int = 40

    # --- youtube let's plays ---
    youtube_enabled: bool = True
    youtube_search_count: int = 15
    youtube_min_duration: int = 180  # seconds; drops shorts and clips
    youtube_timeout: float = 120.0  # per-game budget for the whole stage
    youtube_max_age_days: int = 7
    #: Netscape cookie jar. YouTube blocks video extraction from datacenter IPs
    #: without one, so subtitles and audio are unavailable until this is set.
    youtube_cookies_file: str = ""
    youtube_whisper_enabled: bool = False
    youtube_whisper_model: str = "small"
    youtube_whisper_min_free_mb: int = 2048
    youtube_audio_seconds: int = 900  # transcribe at most the first 15 minutes

    # --- LLM (OpenRouter) ---
    openrouter_api_key: str = ""
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_model: str = "deepseek/deepseek-v4-flash"
    openrouter_fallback_model: str = "google/gemini-2.5-flash-lite"
    llm_timeout: float = 60.0
    site_url: str = "https://github.com/"  # sent as HTTP-Referer to OpenRouter
    site_title: str = "metacritic-ai-watch"

    @model_validator(mode="after")
    def _default_database_url(self) -> "Settings":
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'app.db'}"
        return self


settings = Settings()
