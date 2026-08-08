# Freelance Guardian

**An AI negotiation agent with permanent, auditable contract memory.**

Built for the **CockroachDB × AWS Hackathon — Build with Agentic Memory**.

---

## The problem

Freelancers sign bad contracts because they have no memory. The unlimited-revisions
clause that burned you in March is invisible in June. Every negotiation restarts from zero.

## What it does

Freelance Guardian reads a client contract, recalls **your own rules** and **every risky
clause you've been bitten by before**, scores the risk, and drafts a counter-offer — then
writes the whole reasoning trail to CockroachDB so it is permanent, queryable, and auditable.

The agent doesn't just *have* memory. Its memory is a database a judge can log into and inspect.

## Memory architecture — three kinds, one CockroachDB cluster

| Memory | Backed by | Table | What it holds |
|---|---|---|---|
| **Semantic** (what I believe) | `AsyncCockroachDBVectorStore` | `guardian_memory` | Freelancer rules + known-risky clause patterns, vector-searched |
| **Episodic** (what happened) | `CockroachDBChatMessageHistory` | `guardian_chat_history` | Per-client negotiation thread, survives restarts |
| **Procedural** (where I was) | `AsyncCockroachDBSaver` (LangGraph checkpointer) | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` | Workflow state — kill the Lambda mid-run, it resumes |

Plus `agent_audit_log` — append-only, one row per decision, never updated or deleted.

CockroachDB is not a bolt-on cache here. It is the only place the agent's mind lives.

## Agent workflow (LangGraph)

```
ingest_contract ─► retrieve_memory ─► analyze_contract ─► save_state
   (S3 + clause      (per-clause vector   (LLM / deterministic  (audit log + chat
    chunking)         search + history)    clause engine)        thread + checkpoint)
```

## CockroachDB tools used

The hackathon requires at least two. These two are wired into code and verifiable — we
claim only what runs.

### 1. Distributed Vector Indexing (C-SPANN)

`scripts/init_db.py` creates a C-SPANN index over the embedding column, prefixed by
`namespace` because every retrieval filters on it:

```sql
CREATE VECTOR INDEX guardian_memory_embedding_idx
    ON guardian_memory (namespace, embedding vector_cosine_ops)
```

Two details matter, and both were found the hard way. The index must be
`vector_cosine_ops` because the stores query with `<=>`; an L2 index cannot serve a cosine
query and would sit unused. And `namespace` must be a prefix column, or the planner falls
back to `guardian_memory_namespace_idx` and ignores the vector index entirely.

Prove it is actually in the query path — not merely present:

```
EXPLAIN SELECT content FROM guardian_memory
WHERE namespace = 'rules' ORDER BY embedding <=> '[…]'::VECTOR LIMIT 4;

  • vector search
    table: guardian_memory@guardian_memory_embedding_idx
    prefix spans: [/'rules' - /'rules']
```

### 2. ccloud CLI (Agent-Ready)

`scripts/cluster_info.sh` uses ccloud for cluster inventory and, crucially, to hand back
the connection string — so no connection string in this project is transcribed by hand.

```bash
brew install cockroachdb/tap/ccloud
ccloud auth login
./scripts/cluster_info.sh
```

```
═══ clusters in this organization ═══
NAME                ID                                    PLAN TYPE  STATE    CLOUD  VERSION
freelance-guardian  bfcceaa8-bfe3-44b4-9542-1598dc21450b  BASIC      CREATED  AWS    v26.2.5

═══ agent memory (via the connection string ccloud returns) ═══
  risks    12 entries
  rules    14 entries
  reviews  26 across 9 clients
  index    VECTOR INDEX guardian_memory_embedding_idx (namespace, embedding vector_cosine_ops)
```

### Also present, but we do not count them

- **Managed MCP Server** — [`.mcp.json`](.mcp.json) ships with the cluster id, and
  [`skills/audit-memory/SKILL.md`](skills/audit-memory/SKILL.md) is an Agent Skill built
  entirely around it, with every query verified against the live cluster. It is a genuine
  inspection surface for judges, but application code never calls it, so we do not list it
  among our two.
- **Agent Skills Repo** — our skill follows the upstream format and
  [`skills/README.md`](skills/README.md) points at `cockroachlabs/cockroachdb-skills`, but
  we reference rather than vendor it.

## AWS services used

- **AWS Lambda** — `freelance-guardian` (python3.11, arm64, ap-south-1, same region as the
  cluster so DB round-trips stay local). Every review executes here. Deployed by
  `scripts/deploy_aws.sh`.
- **Amazon S3** — `freelance-guardian-<account>`, private. Each contract is archived before
  analysis and its URI stored in `agent_audit_log.contract_uri`, so every decision points
  back at the exact bytes it was made from.

## Stack

- **CockroachDB Cloud (Free Tier)** — `langchain-cockroachdb` (vector store, chat history, LangGraph checkpointer)
- **LangGraph** — multi-step agent orchestration with durable checkpoints
- **AWS Lambda (Free Tier)** — serverless agent execution via a Function URL
- **Amazon S3 (Free Tier)** — original contract artifact storage
- **MCP** — the CockroachDB Cloud MCP Server, so any AI (or judge) can audit the agent's memory in natural language

## Quickstart

```bash
cd freelance-guardian
make install            # venv + deps
cp .env.example .env    # then fill COCKROACH_DB_URL
make init               # create the three tables
make seed               # load rules + risk patterns
make test               # run the agent on sample_data/risky_contract.txt
```

`MOCK_MODE=true` is the default: **no OpenAI key, no AWS account needed** to run the demo.
The mock LLM and mock embedder are deterministic, so `make test` gives the same answer every time.

### Running on AWS

```bash
make deploy     # build + deploy Lambda, S3 bucket and IAM role (idempotent)
make ui-aws     # same UI, but every review executes on the deployed Lambda
```

`make ui` keeps everything in-process. Both paths run the identical handler with the
identical event shape; the server prints which backend is live at startup.

## Repo map

```
docs/        architecture, MCP usage, Devpost answers
skills/      Agent Skills — including audit-memory for auditing the agent's brain over MCP
src/         config, graph (LangGraph), memory (CockroachDB), llm, prompts, s3, lambda_function
scripts/     init_db, seed_data, test_graph, serve, deploy_aws, destroy_aws
ui/          landing, login, app (Review / Dashboard / Settings), shared theme
sample_data/ freelancer rules, past risks, and a deliberately awful contract
```

## Pages

| Route | What it is |
|---|---|
| `/` | Landing page. Live counts and the most-flagged-clause table come from the real database, not fixtures. |
| `/app#/review` | Paste a contract, get it redlined in place with the recalled memory beside it. |
| `/app#/dashboard` | Audit rollups — most-flagged clauses, clients, recent decisions. |
| `/app#/settings` | Add and remove rules and past risks. Editing the agent's beliefs directly. |
| `/login` | Only reachable when `APP_PASSCODE` is set; otherwise the workspace is open. |

## Docs

- [architecture.md](docs/architecture.md) — why memory is split three ways, and what was deliberately left out
- [mcp-usage.md](docs/mcp-usage.md) — connect over MCP and interrogate the agent's brain yourself
- [devpost-submission.md](docs/devpost-submission.md) — submission answers and the 3-minute demo script
- [skills/](skills/README.md) — the `audit-memory` Agent Skill

## Verify it works

```bash
make selfcheck                          # offline assertions, no database
.venv/bin/python scripts/test_graph.py  # end-to-end + persistence assertions
```

`test_graph.py` asserts the audit log grew, the client thread grew, a checkpoint was
written, and the archived contract round-trips byte-for-byte. Run it twice and watch
`prior messages` go from 0 to 2 — that is the project's whole claim, checked.

## License

MIT — see [LICENSE](LICENSE).
