"""Read/write operations over the agent's memory, shared by both front doors.

`scripts/serve.py` (local) and `src/lambda_function.py` (AWS) both call these, so a
dashboard rendered locally and one rendered from Lambda are the same query, not two
implementations that drift.

Everything here opens and closes its own engine. That is deliberate: Lambda freezes the
event loop between invocations, so a pooled engine held at module scope hands back dead
connections on the next warm start.
"""

from . import config, memory
from sqlalchemy import text

_MODE_LABEL = {
    "mock": "deterministic engine · no API key",
    "gemini": "Gemini",
    "openai": "OpenAI",
}


async def memory_stats(backend: str = "in-process", auth: bool = False) -> dict:
    """Counts only — no contract text. Safe to serve before sign-in."""
    engine = memory.get_engine()
    try:
        async with engine.engine.connect() as conn:
            by_ns = dict((await conn.execute(text(
                f"SELECT namespace, count(*) FROM {config.VECTOR_TABLE} GROUP BY 1"))).all())
            reviews = (await conn.execute(text(
                f"SELECT count(*) FROM {config.AUDIT_TABLE}"))).scalar_one()
            clients = (await conn.execute(text(
                f"SELECT count(DISTINCT client_name) FROM {config.AUDIT_TABLE}"))).scalar_one()
    finally:
        await engine.aclose()
    return {
        "rules": by_ns.get(config.NS_RULES, 0),
        "risks": by_ns.get(config.NS_RISKS, 0),
        "reviews": reviews,
        "clients": clients,
        # "mock" was misleading: the analyser is a deterministic clause engine, not a stub.
        "mode": _MODE_LABEL[config.provider()],
        "backend": backend,
        "auth": auth,
    }


async def dashboard(backend: str = "in-process") -> dict:
    """Audit rollups. The top_clauses query is the one that pays for the whole project:
    a clause flagged across many clients is a missing term in your own contract."""
    engine = memory.get_engine()
    try:
        async with engine.engine.connect() as conn:
            async def rows(sql: str) -> list[dict]:
                return [dict(r._mapping) for r in (await conn.execute(text(sql))).all()]

            recent = await rows(f"""
                SELECT ts::timestamp(0)::STRING AS ts, client_name, risk_score,
                       decision->>'recommendation' AS recommendation,
                       jsonb_array_length(decision->'findings') AS findings
                FROM {config.AUDIT_TABLE} ORDER BY ts DESC LIMIT 12""")
            top = await rows(f"""
                SELECT f->>'clause' AS clause, (f->>'severity')::INT AS severity,
                       count(*) AS times_flagged, count(DISTINCT client_name) AS clients
                FROM {config.AUDIT_TABLE}, jsonb_array_elements(decision->'findings') AS f
                GROUP BY 1, 2 ORDER BY times_flagged DESC, severity DESC LIMIT 10""")
            clients = await rows(f"""
                SELECT client_name, count(*) AS reviews, max(risk_score) AS worst
                FROM {config.AUDIT_TABLE} GROUP BY 1
                ORDER BY worst DESC, reviews DESC LIMIT 10""")
    finally:
        await engine.aclose()
    return {
        "counts": await memory_stats(backend),
        "recent": recent,
        "top_clauses": top,
        "clients": clients,
    }


async def list_items(namespace: str) -> dict:
    engine = memory.get_engine()
    try:
        async with engine.engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT id::STRING AS id, content, metadata FROM {config.VECTOR_TABLE} "
                     "WHERE namespace = :ns "
                     "ORDER BY (metadata->>'severity')::INT DESC NULLS LAST, content"),
                {"ns": namespace},
            )
            items = [{"id": r.id, "text": r.content,
                      "severity": (r.metadata or {}).get("severity")} for r in result.all()]
    finally:
        await engine.aclose()
    return {"items": items}


async def add_item(namespace: str, severity: int, body: str) -> dict:
    """Writes straight into the agent's beliefs, so callers must validate first."""
    engine = memory.get_engine()
    try:
        kind = "rule" if namespace == config.NS_RULES else "risk"
        await memory.vector_store(engine, namespace).aadd_texts(
            [body], metadatas=[{"kind": kind, "severity": severity, "source": "ui",
                        "embedder": config.embedder_id()}]
        )
    finally:
        await engine.aclose()
    return {"ok": True}


async def delete_item(namespace: str, item_id: str) -> dict:
    engine = memory.get_engine()
    try:
        async with engine.engine.begin() as conn:
            # Scoped by namespace as well as id, so a rules id can never delete a risk.
            result = await conn.execute(
                text(f"DELETE FROM {config.VECTOR_TABLE} WHERE id = :id AND namespace = :ns"),
                {"id": item_id, "ns": namespace},
            )
    finally:
        await engine.aclose()
    return {"ok": True, "deleted": result.rowcount}


def validate_item(data: dict) -> tuple[str, int, str] | str:
    """Shared validation for add_item. Returns (namespace, severity, text) or an error
    string. Lives here so the local server and Lambda cannot drift on what they accept."""
    namespace = data.get("namespace")
    body = str(data.get("text") or "").strip()
    severity = data.get("severity")
    if namespace not in (config.NS_RULES, config.NS_RISKS):
        return "namespace must be 'rules' or 'risks'"
    if not body:
        return "text is required"
    if len(body) > 1000:
        return "text must be 1000 characters or fewer"
    if not isinstance(severity, int) or not 1 <= severity <= 5:
        return "severity must be an integer from 1 to 5"
    return namespace, severity, body
