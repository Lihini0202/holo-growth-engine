"""Dependency wiring: builds live or offline implementations from settings.

Shared by the CLI and MCP server so both behave identically.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from holo_growth import db
from holo_growth.config import Settings, get_settings
from holo_growth.crm.local import crm_from_settings
from holo_growth.llm import from_settings as llm_from_settings
from holo_growth.retention.usage import SupabaseUsageSource, SyntheticUsageSource, UsageSource
from holo_growth.web.traffic import GA4Source, SyntheticTrafficSource, TrafficSource, service_account_token


@dataclass
class Services:
    settings: Settings
    conn: sqlite3.Connection
    crm: Any
    llm: Any

    def usage_source(self, synthetic: bool = False, convert_emails: list[str] | None = None) -> UsageSource:
        if self.settings.use_real_holo and not synthetic:
            return SupabaseUsageSource(self.settings.supabase_url, self.settings.supabase_key)
        return SyntheticUsageSource(convert_emails=convert_emails)

    def traffic_source(self, synthetic: bool = False) -> TrafficSource:
        if self.settings.use_real_ga4 and not synthetic:
            import httpx

            client = httpx.Client(timeout=60)
            token = service_account_token(self.settings.ga4_credentials_file, client)
            return GA4Source(self.settings.ga4_property_id, token, self.settings.ga4_signup_event, client)
        return SyntheticTrafficSource()

    def _subscription_mode(self) -> str:
        if self.crm.name == "local":
            return "local"
        if self.crm.name == "salesforce":
            return "salesforce Lead.HasOptedOutOfEmail"
        if self.settings.hubspot_subscription_id:
            return f"hubspot subscription {self.settings.hubspot_subscription_id}"
        return "property only (set HUBSPOT_SUBSCRIPTION_ID)"

    def mode(self) -> dict[str, str]:
        return {
            "llm": self.llm.model if self.llm else "rules only (no ANTHROPIC_API_KEY)",
            "crm": self.crm.name,
            "crm_subscriptions": self._subscription_mode(),
            "holo_data": "supabase" if self.settings.use_real_holo else "synthetic",
            "web_traffic": "ga4" if self.settings.use_real_ga4 else "synthetic",
            "db": str(self.settings.db_path),
        }


def build(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    return Services(settings, conn, crm_from_settings(settings, conn), llm_from_settings(settings))
