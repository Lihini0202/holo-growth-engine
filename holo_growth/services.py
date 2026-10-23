"""Wiring: build real or offline dependencies from settings.

The CLI and the MCP server both go through here, so they always behave the same.
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

    def mode(self) -> dict[str, str]:
        return {
            "llm": self.llm.model if self.llm else "rules only (no ANTHROPIC_API_KEY)",
            "crm": self.crm.name,
            "holo_data": "supabase" if self.settings.use_real_holo else "synthetic",
            "db": str(self.settings.db_path),
        }


def build(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    return Services(settings, conn, crm_from_settings(settings, conn), llm_from_settings(settings))
