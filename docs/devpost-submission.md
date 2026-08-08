# Devpost submission — copy/paste answers

Fill the `<>` placeholders before submitting. Everything else is ready.

---

## Project name

Freelance Guardian

## Tagline

An AI negotiation agent with permanent, auditable contract memory.

## Elevator pitch (200 chars)

Freelancers sign bad contracts because they have no memory. Freelance Guardian reads a
contract against your rules and every clause that ever burned you — and stores its
reasoning in CockroachDB.

---

## Inspiration

Every freelancer has a clause that cost them. Unlimited revisions. Net 60. IP assigning on
creation instead of on payment. They learn the lesson expensively, once — and then forget
it, because there is nowhere for it to live. Six months later a different client sends a
different contract with the same trap, and it works again.

The problem is not intelligence. A freelancer reading their fourth contract of the year is
perfectly capable of spotting a bad indemnity clause. The problem is memory: the lesson from
March is not in the room in June.

That is exactly the gap agentic memory is supposed to close, so we built the agent that
closes it for a real, unglamorous, expensive problem.

## What it does

Paste a contract. Freelance Guardian:

1. **Archives the original** to S3, so every decision points back at the exact bytes it was made from
2. **Splits it into clauses** and runs a vector search per clause against two memories — your standing rules, and the specific clauses that have harmed you before
3. **Loads your negotiation history** with that client, so it never re-demands something you already conceded
4. **Scores the commercial risk** 0–100 and drafts a send-ready counter-offer email
5. **Writes everything down** — the decision to an append-only audit log, the exchange to the client's thread, the workflow state to a LangGraph checkpoint

The UI marks up the contract in place: the offending phrases are struck through in red, and
hovering a finding lights the clause that caused it.

On our sample contract it catches 11 of 12 planted traps, scores 93, and recommends
rejection as written.

Run it twice on the same client and the second run reports the prior messages it loaded.
That is the whole point.

## How we built it

**Memory is three different things, and we stored them separately.**

| Kind | Question | Primitive | Tables |
|---|---|---|---|
| Semantic | What do I believe? | `AsyncCockroachDBVectorStore` | `guardian_memory` |
| Episodic | What happened with this client? | `CockroachDBChatMessageHistory` | `guardian_chat_history` |
| Procedural | Where did I get to? | `AsyncCockroachDBSaver` | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` |

All in one CockroachDB cluster, all through `langchain-cockroachdb`. A fourth table,
`agent_audit_log`, is the record a human reads: one row per decision, never updated.

**Orchestration** is LangGraph — `ingest_contract → retrieve_memory → analyze_contract →
save_state` — with `AsyncCockroachDBSaver` checkpointing every super-step. `thread_id` is
the client slug, which is also the chat `session_id`, so episodic and procedural memory
describe the same relationship.

**Execution** is AWS Lambda (python3.11, arm64, ap-south-1 — same region as the cluster, so
DB round-trips stay local) with S3 for contract artifacts. Both free tier, both deployed by
one idempotent script.

**Inspection** is the CockroachDB Cloud MCP Server. We shipped an Agent Skill,
`skills/audit-memory/`, that teaches any AI how to interrogate the agent's memory — every
query in it verified against the live cluster before we wrote it down.

## Challenges we ran into

**The library README was wrong.** It documents `engine.ainit_chat_history_table()`, which
does not exist. The real path is `history.create_table_if_not_exists()` — and *that* calls
`asyncio.run()` internally, so it explodes inside an async context. We introspected the
installed package rather than trusting the docs, and used the private
`_acreate_table_if_not_exists()` with a comment explaining why duplicating the library's DDL
would be worse.

**The two sponsor libraries disagree on connection scheme.** `CockroachDBEngine` wants
`cockroachdb://`; `AsyncCockroachDBSaver` wants `postgresql://`. Rather than ask users to
keep two URLs in sync, `config.py` normalises one env var into both.

**Mock mode nearly killed the demo.** langchain-core's `DeterministicFakeEmbedding` maps
each string to a *random* vector, so similar sentences land nowhere near each other and
mock-mode retrieval returns nonsense. We wrote a 12-line bag-of-words hash embedder instead:
tokens into buckets, L2-normalised, so word overlap becomes cosine similarity. Same 1536
width as OpenAI, so switching modes needs no table rebuild.

**Retrieval quality was quietly bad** until we stopped querying with the whole document. One
3 KB query vector averages every clause together and the specific hits get washed out. One
query per clause, deduped, took distinct memories surfaced from 4 to 6 per namespace.

**A self-check caught a real bug.** `Path.as_uri()` percent-encodes, and our repo path had a
space in it, so naive `removeprefix("file://")` produced a path that did not exist. The
round-trip assertion failed immediately; `urlparse` + `unquote` fixed it.

**Lambda broke three assumptions at once.** `sslmode=verify-full` looks for a CA cert the
sandbox does not have, so we ship it in the package. Stripping `*.dist-info` to save space
silently removed the entry points SQLAlchemy uses to find the `cockroachdb` dialect —
`NoSuchModuleError`, no hint of the cause. And `/var/task` is read-only, which exposed a
real design error: `MOCK_MODE` was gating both the AI providers *and* the storage backend.
Those are different decisions. Lambda wants the mock analyser (no API key) *and* real S3, so
storage now keys off `S3_BUCKET` alone.

**The Function URL is blocked account-wide.** The deployed URL 403s anonymous callers
despite `AuthType: NONE` and a correct resource policy. We proved it was not our
application by deploying a hello-world Lambda with identical public config — same 403. So
the UI server grew a second backend: set `LAMBDA_FUNCTION` and every review executes on the
deployed Lambda via a signed invoke. Same handler, same event shape, no public URL needed.

## Accomplishments we're proud of

- **The demo runs with no API keys and no AWS account.** `MOCK_MODE=true` is the default. Clone, `make install`, `make init`, `make seed`, `make test` — a real answer in under a minute.
- **The mock is not a stub.** It is a deterministic clause engine with 12 patterns, each carrying its own severity, diagnosis, and counter-offer. Because it uses `re.search` it knows exact character offsets, which is what powers the redline in the UI. A real LLM cannot do that.
- **Every non-trivial component asserts.** `make selfcheck` runs offline; `scripts/test_graph.py` asserts the audit log grew, the chat thread grew, a checkpoint was written, and the archived artifact round-trips byte-for-byte.
- **The audit trail answers a question no single review can.** Across clients, "Unlimited revisions" and "Payment terms" were each flagged 6 times. That is not a client problem — it is a missing standing term in the freelancer's own contract.

## What we learned

Agentic memory is not a vector store with a nicer name. Semantic, episodic, and procedural
memory are read at different times, written by different code, and have different lifetimes —
collapsing them into one bag is what makes agents feel amnesiac despite "having RAG."

And a database is a better observability layer than a dashboard, because an agent can query
it. Putting the reasoning in CockroachDB meant we could hand a judge an MCP connection
instead of asking them to trust us.

## What's next for Freelance Guardian

- **Learn from outcomes, not just clauses.** When a contract goes bad, write that back to the `risks` namespace so the agent's beliefs update from experience rather than seeding.
- **Multi-round negotiation.** The episodic thread already exists; use it to track what the client conceded and hold the line across rounds.
- **Calibrate the scoring.** The current weights are hand-tuned and marked as such in the source. They should be scored against real outcomes.
- **PDF and DOCX ingestion**, because that is how contracts actually arrive.
- **Auth on the API.** A public Function URL is correct for a judged demo, wrong for real contracts.

---

## Built with

`python` · `cockroachdb` · `langchain-cockroachdb` · `langgraph` · `aws-lambda` · `amazon-s3` · `mcp` · `psycopg` · `sqlalchemy`

## Try it out (links)

- GitHub: `<REPO_URL>`
- Demo video: `<VIDEO_URL>`

---

## Judging criteria — where to look

| Criterion | Evidence |
|---|---|
| CockroachDB as persistent memory | `src/memory.py` — three memory types, one cluster. `docs/architecture.md` |
| Agentic reasoning | `src/graph.py` — 4-node LangGraph, checkpointed per super-step |
| Fault tolerance | `AsyncCockroachDBSaver`; `checkpoints` table grows per node, resumable by `thread_id` |
| Agent Skills | `skills/audit-memory/SKILL.md` — upstream format, queries verified live |
| MCP integration | `.mcp.json` ships with the repo; `docs/mcp-usage.md` |
| Works for a judge | `MOCK_MODE=true` default — no keys, no AWS. `make test` |
| AWS Lambda + S3 | Deployed to ap-south-1 by `scripts/deploy_aws.sh`; `make ui-aws` runs every review on it |

## Demo script (3 minutes)

1. **The premise** (20s) — show `sample_data/risky_contract.txt`. Nine clauses, most of them traps.
2. **Empty memory** (15s) — `SELECT count(*) FROM agent_audit_log` over MCP. Nothing yet.
3. **Seed** (20s) — `make seed`. 14 rules, 12 past risks. The probe at the end proves retrieval works.
4. **Review** (40s) — `make ui`, Load sample, Review contract. Score 93, reject, 11 findings, contract redlined in place. Hover a finding, watch its clause light up. Open "Memory consulted" — the exact rules recalled.
5. **It remembers** (30s) — run it again. `prior messages` goes 0 → 2. The agent has met this client.
6. **The audit** (40s) — back to MCP: *"which clause gets flagged most across my clients?"* Unlimited revisions, 6 times. Close on that — it is the payoff.

## Notes before submitting

- [ ] Rotate the CockroachDB SQL password if it was ever pasted anywhere public
- [ ] Clear demo junk: `DELETE FROM agent_audit_log WHERE client_name LIKE 'Evil%';`
- [ ] Push to GitHub and fill `<REPO_URL>`
- [ ] Record the demo and fill `<VIDEO_URL>`
- [ ] Confirm `.env` is not committed (it is gitignored — verify with `git status`)
