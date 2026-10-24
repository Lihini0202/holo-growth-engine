"""Domain models shared across the pipeline."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Persona(StrEnum):
    YOUNG_ADULT = "young_adult"
    YOUNG_PROFESSIONAL = "young_professional"
    OTHER = "other"


class Intent(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Language(StrEnum):
    EN = "en"
    SI = "si"
    TA = "ta"


class LifecycleStage(StrEnum):
    LEAD = "lead"  # signed up at an event, no Holo account yet
    NEW = "new"  # account, no analysis yet
    ACTIVATED = "activated"  # at least one analysis
    ENGAGED = "engaged"  # 3+ analyses in the last 14 days
    AT_RISK = "at_risk"  # was active, nothing for 14-29 days
    DORMANT = "dormant"  # nothing for 30+ days
    OPTED_OUT = "opted_out"  # withdrew marketing consent


class RawLead(BaseModel):
    """A signup row after column mapping, before cleaning."""

    model_config = ConfigDict(extra="ignore")

    row_number: int
    full_name: str = ""
    email: str = ""
    phone: str = ""
    organisation: str = ""
    city: str = ""
    age: str = ""
    heard_from: str = ""
    reason: str = ""
    marketing_opt_in: str = ""


class Lead(BaseModel):
    """A cleaned, validated lead."""

    row_number: int
    first_name: str
    last_name: str
    email: str
    phone: str | None = None
    organisation: str | None = None
    city: str | None = None
    age: int | None = None
    heard_from: str | None = None
    reason: str | None = None
    marketing_opt_in: bool = False
    source_event: str


class Enrichment(BaseModel):
    """Lead attributes inferred by the LLM or the rule baseline. Strictly validated."""

    persona: Persona
    intent: Intent
    language: Language
    interests: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=300)


class EnrichedLead(BaseModel):
    lead: Lead
    enrichment: Enrichment | None
    enrichment_source: str  # "llm", "rules", or "failed"
    score: int = 0
    needs_review: bool = False


class RowIssue(BaseModel):
    row_number: int
    field: str
    problem: str
    value: str = ""
