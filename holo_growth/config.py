"""Runtime configuration from environment variables.

Secrets have no defaults. Each external integration falls back to an offline
implementation when its credentials are not set.
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
    # HubSpot subscription type for marketing-email consent (see `holo-growth consent types`).
    # When unset, consent is written only to the holo_marketing_consent contact property.
    hubspot_subscription_id: str = field(default_factory=lambda: _env("HUBSPOT_SUBSCRIPTION_ID"))
    # hubspot | salesforce. When unset, the CRM whose credentials are present is used.
    crm_provider: str = field(default_factory=lambda: _env("CRM_PROVIDER").lower())
    # Salesforce External Client App (OAuth client credentials flow).
    salesforce_domain: str = field(default_factory=lambda: _env("SALESFORCE_DOMAIN"))
    salesforce_client_id: str = field(default_factory=lambda: _env("SALESFORCE_CLIENT_ID"))
    salesforce_client_secret: str = field(default_factory=lambda: _env("SALESFORCE_CLIENT_SECRET"))
    salesforce_api_version: str = field(default_factory=lambda: _env("SALESFORCE_API_VERSION", "v62.0"))

    # GA4 Data API (read-only, aggregate traffic by source)
    ga4_property_id: str = field(default_factory=lambda: _env("GA4_PROPERTY_ID"))
    ga4_credentials_file: str = field(default_factory=lambda: _env("GA4_CREDENTIALS_FILE"))
    ga4_signup_event: str = field(default_factory=lambda: _env("GA4_SIGNUP_EVENT", "sign_up"))

    # Holo product data (read-only; lifecycle stages)
    supabase_url: str = field(default_factory=lambda: _env("HOLO_SUPABASE_URL"))
    supabase_key: str = field(default_factory=lambda: _env("HOLO_SUPABASE_SERVICE_KEY"))

    # Enrichments below this confidence are routed to the review queue.
    review_threshold: float = field(
        default_factory=lambda: float(_env("HOLO_GROWTH_REVIEW_THRESHOLD", "0.7"))
    )

    # Domains permitted in campaign copy links.
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
    def has_salesforce(self) -> bool:
        return bool(self.salesforce_domain and self.salesforce_client_id and self.salesforce_client_secret)

    @property
    def crm(self) -> str:
        """The CRM in use: the configured provider, otherwise the one with credentials, otherwise local."""
        if self.crm_provider == "salesforce" and self.has_salesforce:
            return "salesforce"
        if self.crm_provider == "hubspot" and self.hubspot_token:
            return "hubspot"
        if self.hubspot_token:
            return "hubspot"
        return "salesforce" if self.has_salesforce else "local"

    @property
    def use_real_crm(self) -> bool:
        return self.crm != "local"

    @property
    def use_real_ga4(self) -> bool:
        return bool(self.ga4_property_id and self.ga4_credentials_file)

    @property
    def use_real_holo(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE pairs from a .env file. Existing environment variables take precedence."""
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
