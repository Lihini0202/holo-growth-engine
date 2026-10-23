"""Runtime configuration, read from environment variables.

Nothing secret has a default. Every external integration has an offline
fallback, so the whole system runs (and is tested) without any API keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("HOLO_GROWTH_DB", "holo_growth.db")))

    # LLM
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("HOLO_GROWTH_MODEL", "claude-sonnet-5-5"))

    # CRM
    hubspot_token: str = field(default_factory=lambda: _env("HUBSPOT_PRIVATE_APP_TOKEN"))
    hubspot_base_url: str = field(default_factory=lambda: _env("HUBSPOT_BASE_URL", "https://api.hubapi.com"))

    # Holo product data (read-only, used for lifecycle stages)
    supabase_url: str = field(default_factory=lambda: _env("HOLO_SUPABASE_URL"))
    supabase_key: str = field(default_factory=lambda: _env("HOLO_SUPABASE_SERVICE_KEY"))

    # Enrichment results below this confidence go to the human review queue.
    review_threshold: float = field(
        default_factory=lambda: float(_env("HOLO_GROWTH_REVIEW_THRESHOLD", "0.7"))
    )

    # Only links to these domains may appear in campaign copy.
    link_domains: frozenset[str] = field(
        default_factory=lambda: frozenset(
            d.strip().lower()
            for d in _env("HOLO_LINK_DOMAINS", "holo-tan.vercel.app").split(",")
            if d.strip()
        )
    )

    @property
    def use_real_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def use_real_crm(self) -> bool:
        return bool(self.hubspot_token)

    @property
    def use_real_holo(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader. Real environment variables always win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_settings() -> Settings:
    load_dotenv()
    return Settings()
