"""The agent's brain. Three kinds of memory, all in one CockroachDB cluster.

  semantic   AsyncCockroachDBVectorStore    guardian_memory        what I believe
  episodic   CockroachDBChatMessageHistory  guardian_chat_history  what happened with this client
  procedural AsyncCockroachDBSaver          guardian_checkpoints   where the workflow got to

Plus an append-only audit log (agent_audit_log) so every decision is inspectable
over the MCP server — see skills/audit-memory.md.
"""

import hashlib
import json
import math
import re
from contextlib import asynccontextmanager

from langchain_cockroachdb import (
    AsyncCockroachDBSaver,
    AsyncCockroachDBVectorStore,
    CockroachDBChatMessageHistory,
    CockroachDBEngine,
)
from langchain_core.embeddings import Embeddings
from sqlalchemy import text

from . import config


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
class HashEmbeddings(Embeddings):
    """Deterministic bag-of-words hash embedder for MOCK_MODE.

    langchain-core ships DeterministicFakeEmbedding, but it maps each string to a
    *random* vector — similar sentences land nowhere near each other, so mock-mode
    retrieval returns nonsense. This hashes tokens into buckets and L2-normalises,
    so word overlap becomes cosine similarity. Deterministic, offline, zero deps.

    ponytail: lexical overlap only — no synonyms, no semantics. Set MOCK_MODE=false
    for real embeddings; the vector width is identical so the table needs no rebuild.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            vec[int.from_bytes(digest, "big") % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def get_embeddings() -> Embeddings:
    provider = config.provider()
    if provider == "mock":
        return HashEmbeddings(config.EMBED_DIM)

    if provider == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        # output_dimensionality truncates gemini-embedding-001 from its native 3072
        # down to our column width, so switching providers needs no schema change.
        return GoogleGenerativeAIEmbeddings(
            model=config.GEMINI_EMBED_MODEL,
            google_api_key=config.GEMINI_API_KEY,
            output_dimensionality=config.EMBED_DIM,
        )

    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=config.OPENAI_EMBED_MODEL, api_key=config.OPENAI_API_KEY
    )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def get_engine() -> CockroachDBEngine:
    """Pooled async engine. Caller owns it and must `await engine.aclose()`."""
    return CockroachDBEngine.from_connection_string(config.sqlalchemy_url())


# --------------------------------------------------------------------------- #
# Semantic memory
# --------------------------------------------------------------------------- #
def vector_store(engine: CockroachDBEngine, namespace: str) -> AsyncCockroachDBVectorStore:
    """One store per namespace — 'rules' and 'risks' share a table but never each other's hits."""
    return AsyncCockroachDBVectorStore(
        engine=engine,
        embeddings=get_embeddings(),
        collection_name=config.VECTOR_TABLE,
        namespace=namespace,
    )


def rules_store(engine: CockroachDBEngine) -> AsyncCockroachDBVectorStore:
    return vector_store(engine, config.NS_RULES)


def risks_store(engine: CockroachDBEngine) -> AsyncCockroachDBVectorStore:
    return vector_store(engine, config.NS_RISKS)


# --------------------------------------------------------------------------- #
# Episodic memory
# --------------------------------------------------------------------------- #
def chat_history(engine: CockroachDBEngine, client_name: str) -> CockroachDBChatMessageHistory:
    """One durable thread per client. Reopening it months later replays the whole negotiation."""
    return CockroachDBChatMessageHistory(
        session_id=session_id_for(client_name),
        engine=engine.engine,  # the underlying SQLAlchemy AsyncEngine
        table_name=config.CHAT_TABLE,
    )


def session_id_for(client_name: str) -> str:
    """Stable, collision-resistant thread id. Also the LangGraph checkpoint thread_id,
    so episodic and procedural memory line up for the same client."""
    slug = re.sub(r"[^a-z0-9]+", "-", client_name.strip().lower()).strip("-")
    return slug or "unknown-client"


# --------------------------------------------------------------------------- #
# Procedural memory
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def checkpointer():
    """LangGraph checkpointer. Kill the process mid-graph; the next run resumes."""
    async with AsyncCockroachDBSaver.from_conn_string(config.psycopg_url()) as saver:
        yield saver


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
AUDIT_DDL = f"""
CREATE TABLE IF NOT EXISTS {config.AUDIT_TABLE} (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    thread_id       STRING      NOT NULL,
    client_name     STRING      NOT NULL,
    contract_uri    STRING,
    risk_score      INT         NOT NULL,
    decision        JSONB       NOT NULL,
    INDEX audit_by_client (client_name, ts DESC),
    INDEX audit_by_risk (risk_score DESC, ts DESC)
)
"""


async def all_rules(engine: CockroachDBEngine) -> list[dict]:
    """Every standing rule, not a similarity search.

    Gap detection needs the full set: vector search returns rules that resemble the
    contract's contents, but a missing protection is by definition absent from those
    contents, so it can never be retrieved that way. The corpus is small enough
    (tens of rows) that reading it whole is cheaper than being clever.
    """
    async with engine.engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT content, metadata FROM {config.VECTOR_TABLE} WHERE namespace = :ns"),
            {"ns": config.NS_RULES},
        )
        return [{"text": r.content, "severity": (r.metadata or {}).get("severity")}
                for r in result.all()]


async def write_audit(
    engine: CockroachDBEngine,
    *,
    thread_id: str,
    client_name: str,
    contract_uri: str | None,
    risk_score: int,
    decision: dict,
) -> None:
    """Append-only. Never updated, never deleted — that is what makes it an audit trail."""
    async with engine.engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {config.AUDIT_TABLE} "
                "(thread_id, client_name, contract_uri, risk_score, decision) "
                "VALUES (:thread_id, :client_name, :contract_uri, :risk_score, :decision)"
            ),
            {
                "thread_id": thread_id,
                "client_name": client_name,
                "contract_uri": contract_uri,
                "risk_score": risk_score,
                "decision": json.dumps(decision),
            },
        )
