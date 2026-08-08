"""Contract artifact storage, and the chunker that feeds retrieval.

Storage is chosen by S3_BUCKET, *not* by MOCK_MODE — they are different decisions.
MOCK_MODE is about AI providers (no LLM key, no embedding key); a Lambda running the
mock analyser still needs real S3, because /var/task is read-only and there is nowhere
local to write.

    S3_BUCKET set    -> boto3 put_object, returns an s3:// URI
    S3_BUCKET unset  -> writes under .local_s3/, returns a file:// URI

Same key layout either way, so switching backends changes the scheme and nothing else.
The URI is recorded in agent_audit_log, giving every decision a pointer back to the
exact bytes it was made from.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config

LOCAL_ROOT = Path(__file__).resolve().parent.parent / ".local_s3"


def _key(contract_text: str, client_name: str) -> str:
    """contracts/<client-slug>/<utc-timestamp>-<content-hash>.txt

    The content hash makes the key idempotent per contract body: re-submitting the
    same text on the same second overwrites rather than littering the bucket.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", client_name.strip().lower()).strip("-") or "unknown-client"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(contract_text.encode()).hexdigest()[:8]
    return f"contracts/{slug}/{stamp}-{digest}.txt"


def put_contract(contract_text: str, client_name: str) -> str:
    """Store the original contract. Returns the URI recorded in the audit log."""
    key = _key(contract_text, client_name)

    if not config.S3_BUCKET:
        path = LOCAL_ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contract_text)
        return path.as_uri()

    import boto3  # imported lazily so mock mode never needs AWS credentials

    boto3.client("s3", region_name=config.AWS_REGION).put_object(
        Bucket=config.S3_BUCKET,
        Key=key,
        Body=contract_text.encode(),
        ContentType="text/plain; charset=utf-8",
    )
    return f"s3://{config.S3_BUCKET}/{key}"


def get_contract(uri: str) -> str:
    """Read back what put_contract() stored. Used to re-open a past decision's exact bytes.

    file:// URIs are percent-encoded by Path.as_uri(), so a repo path containing a
    space produces '...AWS%20X%20COCKROCH/...'. Stripping the scheme by hand yields a
    path that does not exist; urlparse + unquote is the only correct way back.
    """
    if uri.startswith("file://"):
        return Path(unquote(urlparse(uri).path)).read_text()

    import boto3

    parsed = urlparse(uri)
    obj = boto3.client("s3", region_name=config.AWS_REGION).get_object(
        Bucket=parsed.netloc, Key=parsed.path.lstrip("/")
    )
    return obj["Body"].read().decode()


# Contracts are numbered clauses far more often than they are flowing prose, so split
# on clause headings first and fall back to blank lines for anything unstructured.
_CLAUSE_HEADING = re.compile(r"\n(?=\s*\d+\.\s+[A-Z])")


def chunk_contract(contract_text: str, min_chars: int = 40) -> list[str]:
    """Split a contract into clause-sized pieces for retrieval.

    Querying semantic memory once per clause beats one query with the whole document:
    a single 3KB query vector averages every clause together and the specific hits
    (the non-compete, the IP timing) get washed out by the boilerplate.
    """
    parts = _CLAUSE_HEADING.split(contract_text)
    if len(parts) == 1:
        parts = re.split(r"\n\s*\n", contract_text)
    return [c for c in (p.strip() for p in parts) if len(c) >= min_chars]


# --------------------------------------------------------------------------- #
# Self-check:  python -m src.s3
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sample = (Path(__file__).resolve().parent.parent / "sample_data" / "risky_contract.txt").read_text()

    chunks = chunk_contract(sample)
    assert len(chunks) >= 8, f"expected the 9 numbered clauses, got {len(chunks)}"
    assert any("non-compete" in c.lower() for c in chunks), "non-compete clause lost in chunking"
    assert all(len(c) >= 40 for c in chunks), "fragment leaked through the min_chars filter"

    # unstructured text must still chunk rather than returning nothing
    prose = "First paragraph that is comfortably long enough to survive.\n\nSecond paragraph, also long enough to count."
    assert len(chunk_contract(prose)) == 2, chunk_contract(prose)

    uri = put_contract(sample, "Apex Dynamics LLC")
    assert uri.startswith("file://") and "apex-dynamics-llc" in uri, uri
    assert get_contract(uri) == sample, "round-trip mismatch"

    print(f"ok — {len(chunks)} chunks, stored at {uri}")
