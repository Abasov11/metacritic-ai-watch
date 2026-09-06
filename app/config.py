"""Application settings, loaded from environment / `.env`."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage ---
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"

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

    # --- LLM (used in a later step) ---
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o-mini"

    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"


settings = Settings()
