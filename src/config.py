"""Central config. Everything comes from .env — nothing is hardcoded.

One connection string in, two flavours out, because the sponsor libraries disagree:

  * CockroachDBEngine (SQLAlchemy)  wants  cockroachdb://   -> it rewrites to cockroachdb+psycopg://
  * CockroachDBSaver  (raw psycopg) wants  postgresql://

Rather than make you keep two URLs in sync, we normalise whichever you paste.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Mode --------------------------------------------------------------------
# Default True: a judge who clones this repo with no API keys still gets a working demo.
MOCK_MODE = _bool("MOCK_MODE", True)

# --- CockroachDB -------------------------------------------------------------
_RAW_DB_URL = (os.getenv("COCKROACH_DB_URL") or "").strip()

VECTOR_TABLE = os.getenv("VECTOR_TABLE", "guardian_memory")
CHAT_TABLE = os.getenv("CHAT_TABLE", "guardian_chat_history")
AUDIT_TABLE = "agent_audit_log"  # fixed name: the Agent Skill in skills/ references it
# LangGraph's saver hardcodes its own table names — not configurable, listed for docs only.
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))

# Namespaces partition the single vector table into two memories.
NS_RULES = "rules"  # what I believe (my standing terms)
NS_RISKS = "risks"  # what has burned me before (clause patterns)

# --- LLM ---------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# gemini-embedding-001 defaults to 3072 dimensions but supports Matryoshka truncation,
# so it can emit 1536 and reuse the existing table. (text-embedding-004 was shut down
# in January 2026.)
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "models/gemini-embedding-001")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")


def provider() -> str:
    """Which brain is answering: 'mock', 'gemini' or 'openai'.

    MOCK_MODE wins outright, so a stray key in the environment can never turn a
    reproducible demo into a billed API call by accident.
    """
    if MOCK_MODE:
        return "mock"
    if GEMINI_API_KEY:
        return "gemini"
    if OPENAI_API_KEY:
        return "openai"
    raise RuntimeError(
        "MOCK_MODE=false but no API key is set. Add GEMINI_API_KEY (free tier) or "
        "OPENAI_API_KEY to .env, or set MOCK_MODE=true to use the deterministic engine."
    )


def embedder_id() -> str:
    """Identifies which embedder produced a stored vector.

    Vectors from different embedders are not comparable — mixing them silently
    returns nonsense from similarity search rather than failing. Recorded on every
    row so a mismatch is visible instead of mysterious.
    """
    return {
        "mock": f"hash-{EMBED_DIM}",
        "gemini": f"{GEMINI_EMBED_MODEL}-{EMBED_DIM}",
        "openai": f"{OPENAI_EMBED_MODEL}-{EMBED_DIM}",
    }[provider()]

# --- AWS ---------------------------------------------------------------------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")


def _scheme(url: str, scheme: str) -> str:
    """Swap the URL scheme, leaving credentials/host/params untouched."""
    return scheme + url.split("://", 1)[1] if "://" in url else url


def sqlalchemy_url() -> str:
    """For CockroachDBEngine.from_connection_string()."""
    return _scheme(_require_db_url(), "cockroachdb://")


def psycopg_url() -> str:
    """For CockroachDBSaver / AsyncCockroachDBSaver.from_conn_string()."""
    return _scheme(_require_db_url(), "postgresql://")


def _require_db_url() -> str:
    if not _RAW_DB_URL:
        raise RuntimeError(
            "COCKROACH_DB_URL is not set.\n"
            "  cp .env.example .env  and paste your cluster's connection string.\n"
            "  CockroachDB Cloud > your cluster > Connect > General connection string."
        )
    return _RAW_DB_URL


def summary() -> str:
    """Human-readable config with the password masked — safe to print in scripts."""
    url = _RAW_DB_URL or "<unset>"
    if "@" in url:
        url = url.split("://", 1)[0] + "://***:***@" + url.split("@", 1)[1]
    return (
        f"provider={provider()}  EMBED_DIM={EMBED_DIM}\n"
        f"db={url}\n"
        f"tables: {VECTOR_TABLE} / {CHAT_TABLE} / {AUDIT_TABLE} / {'+'.join(CHECKPOINT_TABLES)}"
    )
