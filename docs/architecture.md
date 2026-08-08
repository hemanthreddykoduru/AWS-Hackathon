# Architecture

## The problem this solves

A freelancer's contract knowledge is trapped in their own head, and it leaks. The
unlimited-revisions clause that cost forty unpaid hours in March is invisible in June.
Every negotiation restarts from zero because there is nowhere for the lesson to live.

Freelance Guardian gives that knowledge somewhere to live: a CockroachDB cluster the agent
reads before every decision and writes to after every decision.

## Three kinds of memory, one cluster

Agentic memory is not one thing. This project separates it into three, each backed by a
different `langchain-cockroachdb` primitive, because they are read at different times and
have different lifetimes.

| | Semantic | Episodic | Procedural |
|---|---|---|---|
| **Question it answers** | What do I believe? | What happened with this client? | Where did I get to? |
| **Primitive** | `AsyncCockroachDBVectorStore` | `CockroachDBChatMessageHistory` | `AsyncCockroachDBSaver` |
| **Tables** | `guardian_memory` | `guardian_chat_history` | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` |
| **Read at** | `retrieve_memory` | `retrieve_memory` | every super-step |
| **Written at** | seed time | `save_state` | automatically, per node |
| **Lifetime** | permanent, curated | permanent, append-only | per workflow run |

A fourth table, `agent_audit_log`, is not memory the agent reads — it is the record a
*human* reads. One row per decision, never updated, never deleted.

### Why one vector table and not two

`guardian_memory` holds both the freelancer's rules and their catalogue of past harms,
separated by the native `namespace` column rather than by two tables. One schema, one
index, one migration — and `AsyncCockroachDBVectorStore` takes `namespace` as a constructor
argument, so keeping them apart costs one line each:

```python
rules_store(engine)   # namespace="rules"
risks_store(engine)   # namespace="risks"
```

Two tables would have bought nothing and doubled the DDL.

## The workflow

```
                    ┌──────────────────┐
   contract_text ──►│ ingest_contract  │  archive to S3, split into clauses
   client_name      └────────┬─────────┘
                             ▼
                    ┌──────────────────┐  vector search per clause × 2 namespaces
                    │ retrieve_memory  │  + load this client's chat thread
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐  mock clause engine, or a real LLM
                    │ analyze_contract │  → risk score, findings, counter-offer
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐  append to chat thread
                    │   save_state     │  + write audit row
                    └────────┬─────────┘
                             ▼
                         decision JSON
```

Each arrow is a checkpoint boundary. `AsyncCockroachDBSaver` writes state to CockroachDB
after every node, keyed on `thread_id`. Kill the Lambda between `retrieve_memory` and
`analyze_contract` and the next invocation resumes with the retrieved memory already in
state — it does not re-query the vector store or re-bill the LLM.

`thread_id` is the client name slugified, which is *also* the chat history `session_id`.
Episodic and procedural memory therefore describe the same relationship, and resuming a
client resumes both.

### Why retrieval is per-clause

`ingest_contract` splits the contract on numbered clause headings, and `retrieve_memory`
issues one vector query per clause rather than one for the whole document.

This is not decoration. A single query over a 3 KB contract averages all nine clauses into
one embedding, and the specific hits — the non-compete, the IP-timing clause — get washed
out by boilerplate. Measured on the sample contract, per-clause retrieval raised distinct
memories surfaced from **4 to 6** per namespace.

## Mock mode

`MOCK_MODE=true` is the default, and the whole system runs with no API keys and no AWS
account. Two substitutions make that work.

**The analyser** is a deterministic clause-pattern engine, not a fake LLM returning canned
JSON. Twelve regex patterns mirror `sample_data/freelancer_rules.txt`, each carrying its own
severity, diagnosis, and counter-offer term. It produces a genuinely correct answer on the
sample contract — and because it uses `re.search`, it knows the exact character offsets of
every offending phrase, which is what lets the UI redline the source document. A real LLM
cannot produce those offsets reliably, so `span` is optional to every consumer.

Scoring is `min(100, 12 × max_severity + 3 × count)`: one catastrophic clause already reads
as serious, while a pile of small ones still accumulates, and a clean contract scores 0. The
weights are hand-tuned, not calibrated against outcomes — marked `ponytail:` in the source.

**The embedder** is a 12-line bag-of-words hash embedder. langchain-core ships
`DeterministicFakeEmbedding`, but it maps each string to a *random* vector — similar
sentences land nowhere near each other, so mock retrieval returns nonsense and the demo dies
on stage. `HashEmbeddings` hashes tokens into buckets and L2-normalises, turning word overlap
into cosine similarity. It is lexical only; there are no synonyms. The vector width matches
OpenAI's 1536, so flipping `MOCK_MODE=false` needs no table rebuild.

## Deployment

`src/lambda_function.py` is the AWS entry point, behind a Lambda Function URL. It validates
at the trust boundary — missing, blank, oversized, and malformed bodies all return 400 —
logs exceptions server-side without leaking stack traces, and answers CORS preflight.

`scripts/serve.py` runs the same handler locally over stdlib `http.server`. `/api/review`
delegates to `lambda_function.handler` rather than calling `graph.run` directly, so what is
exercised in local development is byte-for-byte what deploys.

**It is deployed and live.** `scripts/deploy_aws.sh` is idempotent and builds the whole
stack in one command:

```
lambda   freelance-guardian · python3.11 · arm64 · 1536MB · ap-south-1
s3       freelance-guardian-<account> · private, all public access blocked
iam      freelance-guardian-lambda-role · s3:PutObject/GetObject on that bucket only
```

`scripts/destroy_aws.sh` removes it again, keeping the bucket unless you pass `--bucket`,
because the bucket holds the artifacts every audit row points at.

Three things about the Lambda environment differ from local, and each cost a debugging
cycle worth recording:

- **`sslmode=verify-full` fails.** The sandbox has no `~/.postgresql/root.crt`, and the CockroachDB CA is not in its system trust store either. The deploy script ships the 2.7 KB cert in the package and points `sslrootcert` at `/var/task/root.crt`.
- **`*.dist-info` must not be stripped.** It is tempting to delete for size, but SQLAlchemy resolves the `cockroachdb` dialect through entry points declared there. Removing it produces `NoSuchModuleError` with no hint of the cause.
- **`/var/task` is read-only.** This exposed a design error: `MOCK_MODE` was gating both the AI providers and the storage backend. They are different decisions — Lambda wants the mock analyser *and* real S3. Storage now keys off `S3_BUCKET` alone.

`boto3`, `botocore` and `s3transfer` ship with the runtime and are excluded, cutting 28 MB.

### The Function URL is blocked on this account

The deployed URL returns `403 Forbidden` to anonymous callers despite `AuthType: NONE` and a
correct public-invoke resource policy. This was confirmed to be account-wide, not
application-specific: a hello-world Lambda with an identical public URL config returns the
same 403. Only AWS can lift it.

So `scripts/serve.py` gained a second backend. Set `LAMBDA_FUNCTION` and every review is
executed by the deployed Lambda through a signed `boto3` invoke instead of running
in-process:

```bash
make ui-aws   # reviews run on AWS Lambda
make ui       # reviews run locally, no AWS needed
```

Both paths deliver the same event shape to the same `handler`, so neither is a mock of the
other. The server prints which backend is live at startup.

### Connection strings

The two sponsor libraries disagree on scheme, so `src/config.py` normalises one env var
into both:

- `CockroachDBEngine` (SQLAlchemy) wants `cockroachdb://`, which it rewrites internally to `cockroachdb+psycopg://`
- `AsyncCockroachDBSaver` (raw psycopg) wants `postgresql://`

`config.sqlalchemy_url()` and `config.psycopg_url()` each swap the scheme and leave
credentials, host, and TLS parameters untouched.

## Observability by design

The audit log is the observability story, and it is deliberately queryable by an agent
rather than by a dashboard. `skills/audit-memory/SKILL.md` documents the queries, all of
them verified against a live cluster. The most valuable one:

```sql
SELECT f->>'clause' AS clause, count(*) AS times_flagged, count(DISTINCT client_name) AS clients
FROM agent_audit_log, jsonb_array_elements(decision->'findings') AS f
GROUP BY 1 ORDER BY times_flagged DESC;
```

A clause flagged across many clients is not a client problem. It is a missing standing term
in the freelancer's own contract — a conclusion no single review could reach, available only
because every decision was written down.

## What is deliberately absent

- **No auth on the API.** A signed invoke is the current access path, which is stricter than a demo needs. A real deployment behind a public URL would need a token before it accepts contracts.
- **No vector index tuning.** 26 rows do not need a C-SPANN index. Add one when the corpus reaches thousands.
- **No re-ranking, no HyDE, no query expansion.** Per-clause retrieval already surfaces the right memories on this corpus; complexity should follow a measured failure, not precede it.
- **No PDF ingestion.** The agent takes text. Extraction is a solved problem and not what this project is about.
