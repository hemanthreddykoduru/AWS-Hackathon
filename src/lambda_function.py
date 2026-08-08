"""AWS Lambda entry point. scripts/serve.py routes /api/review here — either in-process or
by signed invoke against the deployed function, depending on LAMBDA_FUNCTION.

    POST  { "contract_text": "...", "client_name": "Apex Dynamics LLC" }
    ->    { "risk_score": 93, "recommendation": "reject", "findings": [...], ... }

Also runs locally with no AWS account at all:
    python -m src.lambda_function            # sample contract
    python -m src.lambda_function file.txt "Client Name"
"""

import asyncio
import json
import logging
from pathlib import Path

from . import api, graph, s3

# Shipped inside the deployment package so the public demo can offer a sample contract
# without a round trip to anything else.
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "risky_contract.txt"

MAX_CONTRACT_CHARS = 200_000  # ~50k tokens; a Lambda has 15 min and a body limit
MAX_PDF_BYTES = 5_000_000     # API Gateway caps the request body at 10MB; base64 adds ~33%

CORS = {
    "Content-Type": "application/json",
    # Function URLs are public by design here so a judge can try the demo. Nothing
    # secret is served and nothing is read from the caller beyond the contract body.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}


def _reply(status: int, payload: dict) -> dict:
    return {"statusCode": status, "headers": CORS, "body": json.dumps(payload)}


def _parse(event: dict) -> tuple[str, str]:
    """Pull the contract out of an API Gateway / Function URL event.

    Validated here rather than deeper in: this is the trust boundary, and everything
    past it assumes a non-empty contract and client name.
    """
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode()

    try:
        data = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError as exc:
        raise ValueError(f"body is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")

    client_name = (data.get("client_name") or "").strip()

    # A PDF arrives base64-encoded in the same request; .txt and .md are read in the
    # browser and arrive as plain contract_text.
    pdf_b64 = data.get("contract_pdf_b64")
    if pdf_b64:
        import base64

        try:
            raw = base64.b64decode(pdf_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"contract_pdf_b64 is not valid base64: {exc}") from exc
        if len(raw) > MAX_PDF_BYTES:
            raise ValueError(f"PDF exceeds {MAX_PDF_BYTES // 1_000_000}MB")
        contract_text = s3.pdf_to_text(raw)
    else:
        contract_text = (data.get("contract_text") or "").strip()

    if not contract_text:
        raise ValueError("contract_text is required")
    if not client_name:
        raise ValueError("client_name is required")
    if len(contract_text) > MAX_CONTRACT_CHARS:
        raise ValueError(f"contract_text exceeds {MAX_CONTRACT_CHARS} characters")
    if len(client_name) > 200:
        raise ValueError("client_name exceeds 200 characters")

    return contract_text, client_name


def handler(event, context=None):
    event = event or {}
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    # API Gateway sends rawPath; a direct invoke sends nothing and means "review".
    path = (event.get("rawPath") or event.get("path") or "/api/review").rstrip("/")

    if method == "OPTIONS":
        return _reply(204, {})

    # Read-only endpoints. The public demo is read + review; Settings writes stay on the
    # local server, because a public write endpoint lets anyone edit the agent's beliefs.
    try:
        if path.endswith("/api/memory"):
            return _reply(200, asyncio.run(api.memory_stats(backend="AWS Lambda")))
        if path.endswith("/api/dashboard"):
            return _reply(200, asyncio.run(api.dashboard(backend="AWS Lambda")))
        if path.endswith("/api/sample"):
            return _reply(200, {"contract_text": SAMPLE.read_text(),
                                "client_name": "Apex Dynamics LLC"})
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the caller
        logging.exception("api call failed")
        return _reply(503, {"error": f"{type(exc).__name__}: {exc}"})

    pdf_used = bool((event.get("body") or "").find("contract_pdf_b64") >= 0)
    try:
        contract_text, client_name = _parse(event)
    except ValueError as exc:
        return _reply(400, {"error": str(exc)})

    try:
        decision = asyncio.run(graph.run(contract_text, client_name))
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the caller
        logging.exception("agent run failed")
        return _reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    # When the text came from a PDF the browser never saw it, so hand it back —
    # the redline needs the exact string the findings' spans index into.
    if pdf_used:
        decision["contract_text"] = contract_text
    return _reply(200, decision)


# --------------------------------------------------------------------------- #
# Local runner:  python -m src.lambda_function [contract.txt] [client name]
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "sample_data" / "risky_contract.txt"
    client = sys.argv[2] if len(sys.argv) > 2 else "Apex Dynamics LLC"

    # Validation must reject bad input before we touch the database.
    for bad, why in [
        ({"body": "{}"}, "missing fields"),
        ({"body": '{"contract_text":"x"}'}, "missing client_name"),
        ({"body": "not json"}, "malformed JSON"),
        ({"body": '{"contract_text":"   ","client_name":"A"}'}, "blank contract"),
    ]:
        assert handler(bad)["statusCode"] == 400, f"should have rejected: {why}"

    response = handler(
        {"body": json.dumps({"contract_text": path.read_text(), "client_name": client})}
    )
    print(f"HTTP {response['statusCode']}")
    print(json.dumps(json.loads(response["body"]), indent=2)[:1200])
