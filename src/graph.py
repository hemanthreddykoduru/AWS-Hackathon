"""The LangGraph workflow.

    ingest_contract ──► retrieve_memory ──► analyze_contract ──► save_state

Every super-step is checkpointed to CockroachDB by AsyncCockroachDBSaver, keyed on
thread_id. Kill the process between nodes and the next run resumes from the last
completed node instead of re-reading memory and re-billing the LLM.
"""

from typing import TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from . import config, llm, memory, s3

PER_CLAUSE_K = 2  # hits per clause per namespace
MAX_MEMORIES = 6  # cap on distinct rules (and risks) handed to the analyser


class GuardianState(TypedDict, total=False):
    # inputs
    contract_text: str
    client_name: str
    # filled by ingest_contract
    contract_uri: str | None
    chunks: list[str]
    # filled by retrieve_memory
    retrieved_rules: list[dict]
    retrieved_risks: list[dict]
    all_rules: list[dict]        # full rule set, for gap detection
    negotiation_history: list[str]
    # filled by analyze_contract
    final_decision: dict


def _docs_to_items(docs: list[Document]) -> list[dict]:
    return [
        {"text": d.page_content, "severity": d.metadata.get("severity"), **d.metadata}
        for d in docs
    ]


def build_graph(engine, checkpointer):
    """Wire the three nodes. `engine` and `checkpointer` are injected so the caller
    owns their lifecycle — a Lambda reuses them across warm invocations."""

    async def ingest_contract(state: GuardianState) -> GuardianState:
        """Archive the original bytes, then split into clauses for retrieval.

        Upload first: if the graph dies later, the artifact the decision was about
        still exists and the checkpoint points at it.
        """
        contract = state["contract_text"]
        uri = state.get("contract_uri") or s3.put_contract(contract, state["client_name"])
        return {"contract_uri": uri, "chunks": s3.chunk_contract(contract)}

    async def _search(store, chunks: list[str]) -> list[dict]:
        """One query per clause, then dedupe. A single whole-document query averages
        every clause into one vector and the specific hits get washed out."""
        seen: dict[str, dict] = {}
        for chunk in chunks:
            for doc in await store.asimilarity_search(chunk, k=PER_CLAUSE_K):
                seen.setdefault(doc.page_content, {"text": doc.page_content, **doc.metadata})
        ranked = sorted(seen.values(), key=lambda i: -(i.get("severity") or 0))
        return ranked[:MAX_MEMORIES]

    async def retrieve_memory(state: GuardianState) -> GuardianState:
        """Semantic memory (both namespaces) + episodic memory for this client."""
        chunks = state.get("chunks") or [state["contract_text"]]

        history = memory.chat_history(engine, state["client_name"])
        messages = await history.aget_messages()

        return {
            "retrieved_rules": await _search(memory.rules_store(engine), chunks),
            "retrieved_risks": await _search(memory.risks_store(engine), chunks),
            # Similarity cannot surface a rule the contract never mentions, so gap
            # detection gets the whole rule set rather than the recalled subset.
            "all_rules": await memory.all_rules(engine),
            "negotiation_history": [f"{m.type}: {m.content}" for m in messages],
        }

    async def analyze_contract(state: GuardianState) -> GuardianState:
        """Mock or real analyser — same memory in, same JSON shape out."""
        decision = await llm.analyze(
            contract_text=state["contract_text"],
            rules=state.get("retrieved_rules", []),
            risks=state.get("retrieved_risks", []),
            all_rules=state.get("all_rules", []),
            history=state.get("negotiation_history", []),
            client_name=state["client_name"],
        )
        return {"final_decision": decision}

    async def save_state(state: GuardianState) -> GuardianState:
        """Commit the decision to episodic memory and the audit log.

        The LangGraph checkpoint is written by the saver automatically once this
        node returns — that is the third memory, and we do not manage it by hand.
        """
        client = state["client_name"]
        decision = state["final_decision"]

        history = memory.chat_history(engine, client)
        await history.aadd_messages(
            [
                HumanMessage(
                    content=f"Contract submitted for review ({len(state['contract_text'])} chars)."
                ),
                AIMessage(
                    content=(
                        f"risk_score={decision['risk_score']} "
                        f"recommendation={decision['recommendation']} "
                        f"findings={[f['clause'] for f in decision['findings']]}"
                    )
                ),
            ]
        )

        await memory.write_audit(
            engine,
            thread_id=memory.session_id_for(client),
            client_name=client,
            contract_uri=state.get("contract_uri"),
            risk_score=decision["risk_score"],
            decision=decision,
        )
        return {}

    builder = StateGraph(GuardianState)
    builder.add_node("ingest_contract", ingest_contract)
    builder.add_node("retrieve_memory", retrieve_memory)
    builder.add_node("analyze_contract", analyze_contract)
    builder.add_node("save_state", save_state)

    builder.add_edge(START, "ingest_contract")
    builder.add_edge("ingest_contract", "retrieve_memory")
    builder.add_edge("retrieve_memory", "analyze_contract")
    builder.add_edge("analyze_contract", "save_state")
    builder.add_edge("save_state", END)

    return builder.compile(checkpointer=checkpointer)


async def run(contract_text: str, client_name: str, contract_uri: str | None = None) -> dict:
    """One-shot entry point: open memory, run the graph, close memory.

    thread_id is the client slug, so the checkpoint and the chat thread describe the
    same relationship — resuming a client resumes both.
    """
    if not contract_text.strip():
        raise ValueError("contract_text is empty")
    if not client_name.strip():
        raise ValueError("client_name is empty")

    engine = memory.get_engine()
    try:
        async with memory.checkpointer() as saver:
            graph = build_graph(engine, saver)
            state = await graph.ainvoke(
                {
                    "contract_text": contract_text,
                    "client_name": client_name,
                    "contract_uri": contract_uri,
                },
                config={"configurable": {"thread_id": memory.session_id_for(client_name)}},
            )
        rules = state.get("retrieved_rules", [])
        risks = state.get("retrieved_risks", [])
        history = state.get("negotiation_history", [])
        return {
            **state["final_decision"],
            "contract_uri": state.get("contract_uri"),
            # Return the memory itself, not just a count. A judge should be able to
            # see *which* remembered rule produced *which* finding, without opening
            # a SQL client — that visibility is the whole point of the project.
            "memory_used": {
                "rules": [{"text": r["text"], "severity": r.get("severity")} for r in rules],
                "risks": [{"text": r["text"], "severity": r.get("severity")} for r in risks],
                "prior_messages": len(history),
                "chunks": len(state.get("chunks") or []),
            },
        }
    finally:
        await engine.aclose()
