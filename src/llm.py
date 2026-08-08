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


# --------------------------------------------------------------------------- #
# Gap detection — what the contract does NOT say
# --------------------------------------------------------------------------- #
# CLAUSE_PATTERNS finds bad clauses that are present. It is blind to protections that
# are simply missing, which is how a polished, contractor-friendly template still
# leaves you exposed.
#
# Driven by the freelancer's FULL rule set, not the recalled subset. Vector search
# returns rules resembling what the contract says, and a missing protection is by
# definition absent from that text — gating gaps on recall makes them unable to fire.
#
#   (topic, matches a recalled rule, topic is addressed in the contract, severity, ask)
COVERAGE: list[tuple[str, str, str, int, str]] = [
    ("Portfolio rights", r"portfolio",
     r"portfolio|case stud|showcase|promotional material", 3,
     "Right to display non-confidential work in portfolio and case studies after launch."),
    ("Late-payment remedy", r"late payment|interest per month|overdue",
     r"interest|overdue|late payment|per month on", 3,
     "2% monthly interest on overdue sums; work pauses after 14 days overdue."),
    ("Kill fee", r"kill fee|cancels mid-project",
     r"kill fee|cancellation fee|early termination fee", 3,
     "50% kill fee on the remaining contract value if the client cancels mid-project."),
    ("Deposit", r"deposit",
     r"deposit|advance payment|upon signing|commencement", 4,
     "30% deposit before work begins."),
    ("Liability cap", r"liability",
     r"liabilit|indemnif|not exceed", 4,
     "Total liability capped at fees actually paid under this agreement."),
    ("IP timing", r"copyright|intellectual property|ip assigns",
     r"intellectual property|copyright|assign", 5,
     "IP assigns on receipt of final cleared payment, not on creation."),
    ("Revision cap", r"revision",
     r"revision|change request|amendment", 4,
     "Two revision rounds included; further rounds billed hourly."),
    ("Scope definition", r"scope",
     r"scope of services|deliverable|statement of work", 3,
     "An itemised, written scope. Anything further is a change order."),
]

# A template still full of [placeholders] is not a contract yet — "accept as written"
# would be a meaningless verdict when the terms are literally unwritten.
PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]")


def find_gaps(contract_text: str, rules: list[dict]) -> list[dict]:
    """Protections your standing rules require that this contract never mentions."""
    recalled = " ".join(r.get("text", "") for r in rules).lower()
    body = contract_text.lower()
    gaps = []
    for topic, rule_pat, covered_pat, sev, ask in COVERAGE:
        if not re.search(rule_pat, recalled):
            continue                                   # memory did not raise this topic
        if re.search(covered_pat, body):
            continue                                   # the contract addresses it
        gaps.append({
            "clause": f"Missing: {topic}",
            "severity": sev,
            "issue": f"Your standing rules cover {topic.lower()}, "
                     f"and this contract is silent on it.",
            "counter": ask,
            "kind": "gap",
        })
    return gaps


def find_placeholders(contract_text: str) -> dict | None:
    hits = PLACEHOLDER.findall(contract_text)
    if len(hits) < 3:            # a stray bracket is not an unfilled template
        return None
    return {
        "clause": "Unfilled template",
        "severity": 4,
        "issue": f"{len(hits)} placeholders are still blank "
                 f"(e.g. {', '.join(dict.fromkeys(hits[:3]))}), so key terms are undecided.",
        "counter": "Ask for the completed draft before reviewing — payment terms, notice "
                   "periods and fees are all still blank.",
        "kind": "gap",
    }


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
    contract_text: str, rules: list[dict], risks: list[dict], history: list[str],
    client_name: str, all_rules: list[dict] | None = None,
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
    present = len(findings)

    # What the contract does not say. Driven by the rules memory actually recalled.
    gaps = find_gaps(contract_text, all_rules if all_rules is not None else rules)
    blank = find_placeholders(contract_text)
    if blank:
        gaps.append(blank)
    findings.extend(gaps)

    findings.sort(key=lambda f: -f["severity"])
    score = _score(findings)
    return {
        "risk_score": score,
        "recommendation": _recommend(score),
        "findings": findings,
        "counter_offer_email": _email(client_name, findings),
        "reasoning": (
            f"Matched {present} risky clauses and {len(gaps)} missing protections against "
            f"{len(rules)} retrieved rules and {len(risks)} recorded past risks; "
            f"{len(history)} prior messages with this client were on file. "
            f"Score {score} -> {_recommend(score)}."
        ),
        "mode": "mock",
    }


# --------------------------------------------------------------------------- #
# Real analyser
# --------------------------------------------------------------------------- #
def _chat_model():
    """The configured chat model. temperature=0 because a contract review that changes
    its mind between identical runs is not reviewable."""
    if config.provider() == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=config.GEMINI_MODEL,
            google_api_key=config.GEMINI_API_KEY,
            temperature=0,
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, temperature=0)


async def real_analyze(
    contract_text: str, rules: list[dict], risks: list[dict], history: list[str], client_name: str
) -> dict:
    llm = _chat_model()
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
    contract_text: str, rules: list[dict], risks: list[dict], history: list[str],
    client_name: str, all_rules: list[dict] | None = None,
) -> dict:
    if config.MOCK_MODE:
        return mock_analyze(contract_text, rules, risks, history, client_name, all_rules)
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

    # ---- gaps: driven by recalled memory, not by the contract alone ----------
    clean_but_silent = (
        "1. FEE. Total fee USD 9,000 payable Net 30 from invoice date, 30% on signing.\n"
        "2. IP. Copyright assigns to Client upon full payment of all sums due.\n"
    )
    portfolio_rule = [{"text": "Retain the right to show non-confidential work in a portfolio."}]

    # No rule recalled -> no gap raised, even though the contract is silent.
    assert find_gaps(clean_but_silent, []) == []
    # Rule recalled and contract silent -> gap.
    gaps = find_gaps(clean_but_silent, portfolio_rule)
    assert [g["clause"] for g in gaps] == ["Missing: Portfolio rights"], gaps
    assert gaps[0]["kind"] == "gap"
    # Rule recalled and contract addresses it -> no gap.
    assert find_gaps(clean_but_silent + "Contractor may display work in a portfolio.",
                     portfolio_rule) == []

    # ---- unfilled templates are not reviewable ------------------------------
    assert find_placeholders("payable within [7/15/30] days") is None      # 1 hit, ignored
    tpl = find_placeholders("[Date] fee [Amount] within [7/15/30] days by [Client Name]")
    assert tpl and tpl["severity"] == 4 and "4 placeholders" in tpl["issue"], tpl

    # A silent-but-clean contract now scores above zero when memory says it should.
    c = mock_analyze(clean_but_silent, portfolio_rule, [], [], "Acme")
    assert c["risk_score"] > 0 and len(c["findings"]) == 1, c

    print(f"ok — clean={a['risk_score']}/{a['recommendation']}  "
          f"awful={b['risk_score']}/{b['recommendation']} ({len(b['findings'])} findings)")
