"""Create every table the agent needs. Idempotent — safe to re-run.

    python scripts/init_db.py           # create if missing
    python scripts/init_db.py --reset   # DROP the vector table first (destroys seeded memory)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src import config, memory  # noqa: E402


async def main(reset: bool) -> None:
    print(config.summary(), "\n")
    engine = memory.get_engine()
    try:
        if reset:
            print(f"dropping {config.VECTOR_TABLE} …")
            await engine.adrop_table(config.VECTOR_TABLE)

        # 1. Semantic memory. namespace_column is required for the rules/risks split.
        print(f"vector table   {config.VECTOR_TABLE} (dim={config.EMBED_DIM}) …")
        await engine.ainit_vectorstore_table(
            table_name=config.VECTOR_TABLE,
            vector_dimension=config.EMBED_DIM,
            namespace_column="namespace",
        )

        # 2. Episodic memory. The library creates this off any history instance.
        # NOTE: the public create_table_if_not_exists() wraps asyncio.run(), which
        # explodes inside a running loop. _acreate_table_if_not_exists() is the same
        # code minus that wrapper. Private, but better than duplicating its DDL here
        # and letting our copy drift from the library's on upgrade.
        print(f"chat table     {config.CHAT_TABLE} …")
        await memory.chat_history(engine, "bootstrap")._acreate_table_if_not_exists()

        # 3. Audit log. Plain SQL — no library owns this one, it is ours.
        print(f"audit table    {config.AUDIT_TABLE} …")
        async with engine.engine.begin() as conn:
            await conn.execute(text(memory.AUDIT_DDL))
    finally:
        await engine.aclose()

    # 4. Procedural memory. Owns its own psycopg connection, so it runs outside the engine.
    print(f"checkpoints    (LangGraph saver tables) …")
    async with memory.checkpointer() as saver:
        await saver.setup()

    print("\nok — all tables ready.")


if __name__ == "__main__":
    asyncio.run(main(reset="--reset" in sys.argv))
