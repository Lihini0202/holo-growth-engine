"""Human review queue: approve or correct low-confidence enrichments, then push to the CRM."""

from __future__ import annotations

import json
import sqlite3

from holo_growth.crm.base import CRM
from holo_growth.leads.scoring import score
from holo_growth.models import Enrichment, Intent, Lead, Persona


class ReviewError(ValueError):
    pass


def resolve_enrichment(
    conn: sqlite3.Connection,
    crm: CRM,
    review_id: int,
    persona: Persona | None = None,
    intent: Intent | None = None,
    reject: bool = False,
) -> dict[str, object]:
    item = conn.execute(
        "SELECT * FROM review_queue WHERE id = ? AND kind = 'enrichment'", (review_id,)
    ).fetchone()
    if item is None:
        raise ReviewError(f"no enrichment review with id {review_id}")
    if item["status"] != "open":
        raise ReviewError(f"review {review_id} is already {item['status']}")
    if reject:
        conn.execute("UPDATE review_queue SET status = 'rejected' WHERE id = ?", (review_id,))
        conn.commit()
        return {"id": review_id, "status": "rejected"}

    row = conn.execute("SELECT * FROM leads WHERE email = ?", (item["ref"],)).fetchone()
    if row is None:
        raise ReviewError(f"lead {item['ref']} no longer exists")
    proposed = Enrichment.model_validate(json.loads(item["payload"]))
    final = proposed.model_copy(
        update={
            "persona": Persona(persona) if persona else proposed.persona,
            "intent": Intent(intent) if intent else proposed.intent,
            "confidence": 1.0,
            "rationale": "human reviewed",
        }
    )
    lead = Lead(
        row_number=0,
        first_name=row["first_name"] or "",
        last_name=row["last_name"] or "",
        email=row["email"],
        phone=row["phone"],
        organisation=row["organisation"],
        city=row["city"],
        age=row["age"],
        marketing_opt_in=True,
        source_event=row["source_event"],
    )
    points, _ = score(lead, final)
    crm.upsert_contacts(
        [
            {
                "email": lead.email,
                "holo_persona": final.persona.value,
                "holo_intent": final.intent.value,
                "holo_language": final.language.value,
                "holo_lead_score": str(points),
            }
        ]
    )
    conn.execute(
        "UPDATE leads SET persona = ?, intent = ?, confidence = 1.0, score = ?, "
        "needs_review = 0, enrichment_source = 'human', updated_at = datetime('now') "
        "WHERE email = ?",
        (final.persona.value, final.intent.value, points, lead.email),
    )
    conn.execute("UPDATE review_queue SET status = 'approved' WHERE id = ?", (review_id,))
    conn.commit()
    return {
        "id": review_id,
        "status": "approved",
        "email": lead.email,
        "persona": final.persona.value,
        "intent": final.intent.value,
        "score": points,
    }
