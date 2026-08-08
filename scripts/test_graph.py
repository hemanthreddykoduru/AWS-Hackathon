"""End-to-end check: run the agent on the sample contract and prove memory persisted.

    python scripts/test_graph.py

Asserts, not just prints — this fails loudly if retrieval, the analyser, the audit
log, the chat thread, or the LangGraph checkpoint silently stops working.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src import config, graph, memory, s3  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLIENT = "Apex Dynamics LLC"


async def scalar(engine, sql: str, **params):
    async with engine.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def main() -> None:
    print(config.summary(), "\n")
    contract = (ROOT / "sample_data" / "risky_contract.txt").read_text()

    engine = memory.get_engine()
    try:
        thread_id = memory.session_id_for(CLIENT)
        audit_before = await scalar(
            engine,
            f"SELECT count(*) FROM {config.AUDIT_TABLE} WHERE client_name = :c",
            c=CLIENT,
        )

        print(f"running agent on {len(contract)} chars for {CLIENT!r} …\n")
        decision = await graph.run(contract, CLIENT)

        # ---- the decision itself ------------------------------------------------
        print(f"risk_score      {decision['risk_score']}")
        print(f"recommendation  {decision['recommendation']}")
        print(f"mode            {decision['mode']}")
        print(f"findings        {len(decision['findings'])}")
        for f in decision["findings"]:
            print(f"  sev{f['severity']}  {f['clause']}")
            print(f"        ask: {f['counter']}")
        print(f"\nreasoning       {decision['reasoning']}")
        print("\n--- counter-offer email " + "-" * 40)
        print(decision["counter_offer_email"])
        print("-" * 64)

        assert decision["recommendation"] == "reject", "this contract is awful; expected reject"
        assert decision["risk_score"] >= 70, decision["risk_score"]
        assert len(decision["findings"]) >= 8, "should catch most planted clauses"
        assert CLIENT in decision["counter_offer_email"]

        # every decision must point back at the exact bytes it was made from
        uri = decision["contract_uri"]
        assert uri and "apex-dynamics-llc" in uri, uri
        assert s3.get_contract(uri) == contract, "archived artifact does not match input"
        print(f"\nartifact        {uri}")

        stored_uri = await scalar(
            engine,
            f"SELECT contract_uri FROM {config.AUDIT_TABLE} WHERE client_name = :c "
            "ORDER BY ts DESC LIMIT 1",
            c=CLIENT,
        )
        assert stored_uri == uri, f"audit log lost the artifact URI: {stored_uri}"

        # ---- memory actually persisted -----------------------------------------
        audit_after = await scalar(
            engine,
            f"SELECT count(*) FROM {config.AUDIT_TABLE} WHERE client_name = :c",
            c=CLIENT,
        )
        assert audit_after == audit_before + 1, f"audit log did not grow: {audit_before}->{audit_after}"

        chat_rows = await scalar(
            engine,
            f"SELECT count(*) FROM {config.CHAT_TABLE} WHERE session_id = :s",
            s=thread_id,
        )
        assert chat_rows >= 2, f"episodic memory empty for {thread_id}"

        ckpt_rows = await scalar(
            engine, "SELECT count(*) FROM checkpoints WHERE thread_id = :t", t=thread_id
        )
        assert ckpt_rows > 0, f"no LangGraph checkpoint written for {thread_id}"

        print(f"\nmemory persisted:")
        print(f"  agent_audit_log        {audit_after} rows for this client")
        print(f"  guardian_chat_history  {chat_rows} messages on thread {thread_id!r}")
        print(f"  checkpoints            {ckpt_rows} for thread {thread_id!r}")
    finally:
        await engine.aclose()

    print("\nok — end-to-end passed.")


if __name__ == "__main__":
    asyncio.run(main())
