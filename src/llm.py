"""Two analysers behind one function: `analyze()`.

MOCK_MODE=true  -> a deterministic clause-pattern engine. No API key, no network,
                   identical output every run, so `make test` is reproducible and a
                   judge can clone the repo and see a real answer in 30 seconds.
MOCK_MODE=false -> a real LLM given the same memory in the same prompt.

Both return the shape defined by prompts.DECISION_SCHEMA.
"""

import json
import re

from . import config, prompts

# --------------------------------------------------------------------------- #
# Mock analyser
# --------------------------------------------------------------------------- #
# (clause name, severity, regex, what is wrong, what to demand instead)
# These mirror sample_data/freelancer_rules.txt — the rules are what the freelancer
# believes, these patterns are how those beliefs get detected in contract prose.
CLAUSE_PATTERNS: list[tuple[str, int, str, str, str]] = [
    (
        "Unlimited revisions", 5,
        r"unlimited revisions|until .{0,30}(fully )?satisfied|sole discretion",
        "Revisions are uncapped and 'satisfaction' is undefined, so the work can never end.",
        "Two revision rounds included; further rounds billed hourly at the standard rate.",
    ),
    (
        "Payment terms", 5,
        r"net\s*(45|60|75|90)|payable .{0,40}acceptance|upon .{0,20}written acceptance",
        "Payment is deferred past Net 30 and gated on the client's own acceptance.",
        "Net 30 from invoice date, not from acceptance. Milestone invoices at 50% and on delivery.",
    ),
    (
        "IP assignment timing", 5,
        r"property of client immediately upon creation|whether or not payment|upon creation",
        "Copyright transfers before payment clears, leaving nothing to withhold if the client defaults.",
        "IP assigns on receipt of final cleared payment; a licence to review applies before then.",
    ),
    (
        "No deposit", 4,
        r"no deposit|no advance payment|without any (deposit|advance)",
        "Work starts with zero money down — every non-payment starts this way.",
        "30% non-refundable deposit before work begins.",
    ),
    (
        "Uncapped liability", 4,
        r"without limitation as to amount|any and all claims|unlimited liability|hold client harmless",
        "Indemnity is uncapped, so a small fee carries unbounded downside.",
        "Total liability capped at fees actually paid under this agreement; no consequential damages.",
    ),
    (
        "Non-compete", 4,
        r"non-compete|shall not provide .{0,60}services|(three|3|four|4|five|5)\s*\(?\d?\)?\s*years?",
        "The restraint is too long and too broad to work around.",
        "6 months, limited to the client's named direct competitors.",
    ),
    (
        "Termination for convenience", 4,
        r"terminate .{0,60}(at any time|for any reason)|no obligation to pay for work performed",
        "The client can walk away without paying for work already delivered.",
        "14 days' written notice; all work performed to the termination date is invoiced and payable.",
    ),
    (
        "Open-ended scope", 3,
        r"other duties as .{0,40}require|as .{0,20}may .{0,30}reasonably require",
        "Scope has no boundary, so the fee has no relationship to the work.",
        "Scope limited to the itemised deliverables; anything further is a written change order.",
    ),
    (
        "Portfolio restriction", 3,
        r"shall not display|not .{0,30}(reference|disclose) any work|portfolio, case study",
        "Non-confidential work cannot be shown, removing its career value.",
        "Right to display non-confidential work in portfolio and case studies after launch.",
    ),
    (
        "No late-payment remedy", 2,
        r"no interest shall accrue|no interest .{0,20}late",
        "There is no cost to the client for paying late, so late payment is free.",
        "2% monthly interest on overdue sums; work pauses after 14 days overdue.",
    ),
    (
        "Governing law", 2,
        r"exclusive jurisdiction|governed by the laws of the state",
        "Disputes must be pursued in the client's courts, costing more than the fee is worth.",
        "Governing law and venue in the freelancer's jurisdiction for contracts under $25,000.",
    ),
    (
        "Moral rights waiver", 2,
        r"waives all moral rights|waiver of moral rights",
        "Moral rights are waived with no separate consideration.",
        "Moral rights retained, or waived only in exchange for an explicit attribution clause.",
    ),
]


def _score(findings: list[dict]) -> int:
    """max severity dominates, count adds pressure.

    One catastrophic clause should already read as serious (12*5 = 60 -> "negotiate"
    edging on "reject"), while a pile of small ones still accumulates. A clean
    contract scores 0. Capped at 100.

    ponytail: hand-tuned weights, not calibrated against real outcomes. If this ever
    drives a real decision, replace with a scorecard agreed with a contracts lawyer.
    """
    if not findings:
        return 0
    return min(100, 12 * max(f["severity"] for f in findings) + 3 * len(findings))


def _recommend(score: int) -> str:
    return "reject" if score >= 70 else "negotiate" if score >= 35 else "accept"


def _email(client_name: str, findings: list[dict]) -> str:
    if not findings:
        return (
            f"Hi {client_name},\n\nThanks for sending this over — the terms look fine "
            f"to me and I'm happy to sign as drafted.\n\nBest regards"
        )
    asks = "\n".join(f"{n}. {f['clause']}: {f['counter']}" for n, f in enumerate(findings, 1))
    return (
        f"Hi {client_name},\n\n"
        f"Thanks for sending the agreement. I'd like to go ahead, and there are "
        f"{len(findings)} points I need adjusted before I can sign:\n\n"
        f"{asks}\n\n"
        f"These are my standard terms and they're what let me commit properly to the "
        f"work. Happy to jump on a call if it's easier to work through them together.\n\n"
        f"Best regards"
    )


def mock_analyze(
    contract_text: str, rules: list[dict], risks: list[dict], history: list[str], client_name: str
) -> dict:
    findings = []
    for name, sev, pattern, issue, counter in CLAUSE_PATTERNS:
        hit = re.search(pattern, contract_text, re.IGNORECASE)
        if not hit:
            continue
        findings.append(
            {
                "clause": name,
                "severity": sev,
                "issue": issue,
                "counter": counter,
                # Exact offsets of the offending words, so the UI can redline the
                # source document instead of just listing complaints beside it.
                # A real LLM cannot produce these reliably; consumers must treat
                # `span` as optional.
                "match": hit.group(0),
                "span": [hit.start(), hit.end()],
            }
        )
    findings.sort(key=lambda f: -f["severity"])
    score = _score(findings)
    return {
        "risk_score": score,
        "recommendation": _recommend(score),
        "findings": findings,
        "counter_offer_email": _email(client_name, findings),
        "reasoning": (
            f"Matched {len(findings)} clause patterns against {len(rules)} retrieved rules "
            f"and {len(risks)} recorded past risks; {len(history)} prior messages with this "
            f"client were on file. Score {score} -> {_recommend(score)}."
        ),
        "mode": "mock",
    }


# --------------------------------------------------------------------------- #
# Real analyser
# --------------------------------------------------------------------------- #
async def real_analyze(
    contract_text: str, rules: list[dict], risks: list[dict], history: list[str], client_name: str
) -> dict:
    from langchain_openai import ChatOpenAI  # optional dep, real mode only

    llm = ChatOpenAI(
        model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, temperature=0
    )
    reply = await llm.ainvoke(
        [
            ("system", prompts.SYSTEM),
            (
                "user",
                prompts.USER_TEMPLATE.format(
                    client_name=client_name,
                    rules=prompts.render_memory(rules),
                    risks=prompts.render_memory(risks),
                    history=prompts.render_history(history),
                    contract_text=contract_text,
                    schema=prompts.DECISION_SCHEMA,
                ),
            ),
        ]
    )
    decision = _parse_json(reply.content)
    decision["mode"] = "live"
    return decision


def _parse_json(raw: str) -> dict:
    """Models still fence JSON despite being told not to. Strip and retry once,
    then fail loudly — a silently-empty decision is worse than a crash."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"LLM returned no JSON object:\n{raw[:500]}")
        return json.loads(match.group())


async def analyze(
    contract_text: str, rules: list[dict], risks: list[dict], history: list[str], client_name: str
) -> dict:
    if config.MOCK_MODE:
        return mock_analyze(contract_text, rules, risks, history, client_name)
    return await real_analyze(contract_text, rules, risks, history, client_name)


# --------------------------------------------------------------------------- #
# Self-check:  python -m src.llm
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    clean = "The parties agree to a fixed fee of $5,000, payable Net 30 from invoice date."
    awful = (
        "Client is entitled to unlimited revisions. Total fee payable Net 60. All work "
        "product shall be property of Client immediately upon creation. No deposit shall "
        "be made. Contractor shall indemnify Client from any and all claims."
    )

    a = mock_analyze(clean, [], [], [], "Acme")
    assert a["risk_score"] == 0, a["risk_score"]
    assert a["recommendation"] == "accept", a
    assert a["findings"] == []

    b = mock_analyze(awful, [], [], [], "Acme")
    assert b["risk_score"] >= 70, b["risk_score"]
    assert b["recommendation"] == "reject", b
    assert {f["clause"] for f in b["findings"]} >= {
        "Unlimited revisions", "Payment terms", "IP assignment timing", "No deposit",
    }, b["findings"]
    assert b["findings"] == sorted(b["findings"], key=lambda f: -f["severity"])
    assert "Acme" in b["counter_offer_email"]

    # spans must actually index the offending text, or the UI redlines the wrong words
    for f in b["findings"]:
        start, end = f["span"]
        assert awful[start:end] == f["match"], (f["clause"], awful[start:end], f["match"])

    # deterministic: same input, byte-identical output
    assert mock_analyze(awful, [], [], [], "Acme") == b

    print(f"ok — clean={a['risk_score']}/{a['recommendation']}  "
          f"awful={b['risk_score']}/{b['recommendation']} ({len(b['findings'])} findings)")
