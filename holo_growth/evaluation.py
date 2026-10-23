"""Measure enrichment quality against a labelled set.

Reports accuracy per field, how much gets routed to review, and accuracy on
the auto-accepted part only. That last number is the one that matters: it is
the error rate of what reaches the CRM without a human looking at it.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from holo_growth.leads.clean import clean_row
from holo_growth.leads.enrich import Enricher
from holo_growth.leads.ingest import map_headers
from holo_growth.models import RawLead


@dataclass
class EvalResult:
    enricher: str
    n: int = 0
    correct: dict[str, int] = field(default_factory=lambda: Counter())
    routed_to_review: int = 0
    accepted_correct: int = 0
    accepted: int = 0
    confusion_intent: dict[tuple[str, str], int] = field(default_factory=lambda: Counter())

    def accuracy(self, fld: str) -> float:
        return self.correct[fld] / self.n if self.n else 0.0

    @property
    def accepted_accuracy(self) -> float:
        return self.accepted_correct / self.accepted if self.accepted else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "enricher": self.enricher,
            "n": self.n,
            "persona_accuracy": round(self.accuracy("persona"), 3),
            "intent_accuracy": round(self.accuracy("intent"), 3),
            "language_accuracy": round(self.accuracy("language"), 3),
            "all_three_correct": round(self.accuracy("all"), 3),
            "review_rate": round(self.routed_to_review / self.n, 3) if self.n else 0.0,
            "auto_accepted_all_correct": round(self.accepted_accuracy, 3),
        }


def evaluate(path: Path | str, enricher: Enricher, review_threshold: float = 0.7) -> EvalResult:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    mapping, _ = map_headers([h for h in rows[0] if not h.startswith("true_")])
    result = EvalResult(enricher=getattr(enricher, "source", type(enricher).__name__))
    for i, row in enumerate(rows, start=2):
        values = {fld: row[orig] for orig, fld in mapping.items()}
        lead, _ = clean_row(RawLead(row_number=i, **values), "eval")
        if lead is None:
            continue
        e = enricher.enrich(lead)
        result.n += 1
        ok = {
            "persona": e.persona.value == row["true_persona"],
            "intent": e.intent.value == row["true_intent"],
            "language": e.language.value == row["true_language"],
        }
        for k, v in ok.items():
            result.correct[k] += int(v)
        all_ok = all(ok.values())
        result.correct["all"] += int(all_ok)
        result.confusion_intent[(row["true_intent"], e.intent.value)] += 1
        if e.confidence < review_threshold:
            result.routed_to_review += 1
        else:
            result.accepted += 1
            result.accepted_correct += int(all_ok)
    return result
