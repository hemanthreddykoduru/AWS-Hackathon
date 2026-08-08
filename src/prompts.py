"""Prompts and the decision contract.

Mock mode and real mode must return the *same* JSON shape, or the graph downstream
of the LLM would need two code paths. DECISION_SCHEMA is the single source of truth
for both; it is embedded verbatim in the real-LLM prompt.
"""

DECISION_SCHEMA = """{
  "risk_score": <int 0-100>,
  "recommendation": "accept" | "negotiate" | "reject",
  "findings": [
    {
      "clause": "<short name of the offending clause>",
      "severity": <int 1-5>,
      "issue": "<what is wrong with it, one sentence>",
      "counter": "<the specific replacement term to demand>"
    }
  ],
  "counter_offer_email": "<a complete, polite, send-ready email to the client>",
  "reasoning": "<two sentences on how the retrieved memory drove this decision>"
}"""

SYSTEM = """You are Freelance Guardian, a contract negotiation agent working for an \
independent freelancer. You protect the freelancer's interests. You are not a lawyer \
and you do not give legal advice; you flag commercial risk and draft counter-offers.

You have three sources of memory and you must use them:
  1. RULES  - the freelancer's own standing terms. These are non-negotiable positions.
  2. RISKS  - clause patterns that have actually harmed this freelancer before.
  3. HISTORY - what has already been said to this specific client.

Never contradict a RULE. Never repeat a demand already conceded in HISTORY.
Ground every finding in a retrieved RULE or RISK - do not invent policy.

Respond with JSON only. No markdown fences, no prose outside the JSON."""

USER_TEMPLATE = """CLIENT: {client_name}

--- RETRIEVED RULES (the freelancer's standing terms) ---
{rules}

--- RETRIEVED RISKS (clauses that burned this freelancer before) ---
{risks}

--- NEGOTIATION HISTORY WITH THIS CLIENT ---
{history}

--- CONTRACT UNDER REVIEW ---
{contract_text}

--- TASK ---
Score the commercial risk to the freelancer and draft a counter-offer.
Scoring: 0-34 acceptable, 35-69 negotiate, 70-100 reject as written.

Return exactly this JSON structure:
{schema}"""


def render_memory(items: list[dict]) -> str:
    """Retrieved documents -> prompt lines. Empty memory says so out loud, rather
    than sending a blank section the model will quietly hallucinate around."""
    if not items:
        return "(none retrieved - semantic memory is empty; run scripts/seed_data.py)"
    return "\n".join(f"- [severity {i.get('severity', '?')}] {i['text']}" for i in items)


def render_history(messages: list[str]) -> str:
    return "\n".join(messages) if messages else "(no prior contact with this client)"
