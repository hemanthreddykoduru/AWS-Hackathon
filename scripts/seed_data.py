"""Load the freelancer's rules and past risky-clause patterns into semantic memory.

    python scripts/seed_data.py           # skip if already seeded
    python scripts/seed_data.py --force   # wipe both namespaces and reload

Both files share one format:  KIND | severity | text
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src import config, memory  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [
    (config.NS_RULES, ROOT / "sample_data" / "freelancer_rules.txt"),
    (config.NS_RISKS, ROOT / "sample_data" / "risk_patterns.txt"),
]


def parse(path: Path) -> tuple[list[str], list[dict]]:
    """`KIND | severity | text` per line. Blanks and #-comments ignored."""
    texts, metas = [], []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3:
            raise ValueError(f"{path.name}:{lineno}: expected 'KIND | severity | text', got: {raw}")
        kind, severity, body = parts
        if not severity.isdigit() or not 1 <= int(severity) <= 5:
            raise ValueError(f"{path.name}:{lineno}: severity must be 1-5, got {severity!r}")
        texts.append(body)
        metas.append({"kind": kind.lower(), "severity": int(severity),
                      "source": path.name, "embedder": config.embedder_id()})
    if not texts:
        raise ValueError(f"{path.name}: no entries found")
    return texts, metas


async def count(engine, namespace: str) -> int:
    async with engine.engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT count(*) FROM {config.VECTOR_TABLE} WHERE namespace = :ns"),
            {"ns": namespace},
        )
        return result.scalar_one()


async def main(force: bool) -> None:
    print(config.summary())
    print(f"embedder: {config.embedder_id()}\n")
    engine = memory.get_engine()
    try:
        for namespace, path in SOURCES:
            store = memory.vector_store(engine, namespace)
            existing = await count(engine, namespace)

            if existing and not force:
                print(f"{namespace:<6} {existing} rows already present — skipping (--force to reload)")
                continue
            if existing:
                print(f"{namespace:<6} clearing {existing} rows …")
                async with engine.engine.begin() as conn:
                    await conn.execute(
                        text(f"DELETE FROM {config.VECTOR_TABLE} WHERE namespace = :ns"),
                        {"ns": namespace},
                    )

            texts, metas = parse(path)
            await store.aadd_texts(texts, metadatas=metas)
            print(f"{namespace:<6} loaded {len(texts)} entries from {path.name}")

        # Prove retrieval works before declaring success — a silent empty index is
        # the classic way a vector demo dies on stage.
        probe = "client wants unlimited revisions and will pay Net 60"
        for namespace, _ in SOURCES:
            hits = await memory.vector_store(engine, namespace).asimilarity_search(probe, k=2)
            print(f"\nprobe [{namespace}] {probe!r}")
            for doc in hits:
                print(f"  sev{doc.metadata.get('severity')} {doc.page_content[:88]}…")
            assert hits, f"{namespace} returned no hits — retrieval is broken"
    finally:
        await engine.aclose()

    print("\nok — semantic memory seeded.")


if __name__ == "__main__":
    asyncio.run(main(force="--force" in sys.argv))
