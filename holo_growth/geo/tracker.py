"""GEO (generative engine optimisation) tracker.

Queries AI assistants with Holo-relevant prompts and records whether Holo is
mentioned, its position relative to competitors, and which competitors are named.
Mention rate is meaningful as a trend across prompts and scheduled runs, not per
answer.

Providers:
* AnthropicProvider: Claude via the Anthropic API.
* OpenRouterProvider: any OpenRouter model (e.g. openai/gpt-5, google/gemini-3-pro).
* CannedProvider: fixed answers for offline runs, labelled 'demo'.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from importlib import resources
from typing import Protocol

import httpx

from holo_growth import db

log = logging.getLogger(__name__)

ASK_SYSTEM = "Answer the user's question helpfully and concisely. If you recommend apps or tools, name them."


class Provider(Protocol):
    name: str

    def ask(self, prompt: str) -> str: ...


class AnthropicProvider:
    def __init__(self, llm) -> None:
        self._llm = llm
        self.name = f"anthropic:{getattr(llm, 'model', 'claude')}"

    def ask(self, prompt: str) -> str:
        return str(self._llm.text(ASK_SYSTEM, prompt, max_tokens=700))


class OpenRouterProvider:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        self._key, self.model = api_key, model
        self.name = f"openrouter:{model}"
        self._client = client or httpx.Client(timeout=60)

    def ask(self, prompt: str) -> str:
        resp = self._client.post(
            self.URL,
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": self.model,
                "max_tokens": 700,
                "messages": [{"role": "system", "content": ASK_SYSTEM}, {"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"])


class CannedProvider:
    """Fixed offline answers. Results are labelled 'demo' to keep them distinct from real data."""

    name = "demo"

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self._answers = answers or {}

    def ask(self, prompt: str) -> str:
        if prompt in self._answers:
            return self._answers[prompt]
        if "Sri Lanka" in prompt:
            return (
                "Popular options include Tinder and Bumble for matching. For safety, "
                "Holo analyses chats to flag when a conversation is going cold, and "
                "Wingman offers reply suggestions."
            )
        if "reply" in prompt.lower():
            return "Tools like Rizz, Keys AI and YourMove generate reply suggestions."
        return (
            "Look for short replies, no questions back and longer gaps. Apps such as "
            "Rizz can help with replies."
        )


@dataclass
class GeoConfig:
    brand: str
    brand_patterns: list[str]
    competitors: list[str]
    prompts: list[str]

    @classmethod
    def load(cls, path: str | None = None) -> GeoConfig:
        if path:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.loads(resources.files("holo_growth.data").joinpath("geo_config.json").read_text())
        return cls(**data)


@dataclass
class Mention:
    mentioned: bool
    position: int | None
    competitors: list[str] = field(default_factory=list)


def detect(answer: str, cfg: GeoConfig) -> Mention:
    hits: list[tuple[int, str]] = []
    brand_at = None
    for pattern in cfg.brand_patterns:
        m = re.search(pattern, answer)  # Case-sensitive: lowercase "holo" is usually not the brand name.
        if m and (brand_at is None or m.start() < brand_at):
            brand_at = m.start()
    if brand_at is not None:
        hits.append((brand_at, cfg.brand))
    comps = []
    for c in cfg.competitors:
        m = re.search(rf"\b{re.escape(c)}\b", answer, flags=re.IGNORECASE)
        if m:
            hits.append((m.start(), c))
            comps.append(c)
    hits.sort()
    position = next((i + 1 for i, (_, name) in enumerate(hits) if name == cfg.brand), None)
    return Mention(brand_at is not None, position, comps)


@dataclass
class GeoReport:
    run_id: int
    checks: int = 0
    mentions: int = 0
    errors: int = 0
    by_provider: dict[str, dict[str, int]] = field(default_factory=dict)
    competitor_counts: dict[str, int] = field(default_factory=dict)

    @property
    def mention_rate(self) -> float:
        return self.mentions / self.checks if self.checks else 0.0

    def summary(self) -> str:
        comps = ", ".join(
            f"{k} {v}" for k, v in sorted(self.competitor_counts.items(), key=lambda kv: -kv[1])
        )
        return (
            f"Holo mentioned in {self.mentions}/{self.checks} answers "
            f"({self.mention_rate:.0%}). Competitors named: {comps or 'none'}. "
            f"{self.errors} provider errors."
        )


def run_geo(conn: sqlite3.Connection, providers: list[Provider], cfg: GeoConfig | None = None) -> GeoReport:
    cfg = cfg or GeoConfig.load()
    run_id = db.start_run(conn, "geo", ",".join(p.name for p in providers))
    report = GeoReport(run_id)
    for provider in providers:
        stats = report.by_provider.setdefault(provider.name, {"checks": 0, "mentions": 0})
        for prompt in cfg.prompts:
            try:
                answer = provider.ask(prompt)
            except Exception as exc:  # Isolate provider failures; continue with the remaining providers.
                report.errors += 1
                log.warning(
                    "GEO provider failed", extra={"provider": provider.name, "error": type(exc).__name__}
                )
                continue
            m = detect(answer, cfg)
            report.checks += 1
            stats["checks"] += 1
            if m.mentioned:
                report.mentions += 1
                stats["mentions"] += 1
            for c in m.competitors:
                report.competitor_counts[c] = report.competitor_counts.get(c, 0) + 1
            conn.execute(
                "INSERT INTO geo_results (run_id, provider, prompt, mentioned, position, "
                "competitors, answer_excerpt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    provider.name,
                    prompt,
                    int(m.mentioned),
                    m.position,
                    json.dumps(m.competitors),
                    answer[:400],
                ),
            )
    conn.commit()
    status = "ok" if not report.errors else ("partial" if report.checks else "failed")
    db.finish_run(
        conn,
        run_id,
        status,
        json.dumps(
            {
                "checks": report.checks,
                "mentions": report.mentions,
                "errors": report.errors,
                "by_provider": report.by_provider,
                "competitors": report.competitor_counts,
            }
        ),
    )
    return report


def providers_from_env(settings, llm) -> list[Provider]:
    import os

    providers: list[Provider] = []
    if llm is not None:
        providers.append(AnthropicProvider(llm))
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    models = [m.strip() for m in os.environ.get("HOLO_GEO_OPENROUTER_MODELS", "").split(",") if m.strip()]
    if key:
        providers.extend(OpenRouterProvider(key, m) for m in models)
    return providers or [CannedProvider()]
