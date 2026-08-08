# Freelance Guardian — completion audit & implementation plan

> **For whoever picks this up next (human or LLM).** Everything below was verified by
> running commands against the live repo, the live CockroachDB cluster and the live AWS
> account on **2026-08-08**. Nothing here is assumed. Where a claim is unverified it says so.
>
> **Deadline: 2026-08-18, 5:00pm EDT** (= 2026-08-19 02:30 IST). Submit on the 17th.

---

## 1. What this project is

**Freelance Guardian** — an AI negotiation agent with permanent, auditable contract memory.
Built for the **CockroachDB × AWS Hackathon: Build with Agentic Memory**.

A freelancer pastes a client contract. The agent recalls their standing rules and every
clause that has burned them before, scores commercial risk 0–100, drafts a counter-offer,
and writes the whole reasoning trail to CockroachDB so it is permanent and queryable.

### Repo layout

```
freelance-guardian/
├── .env                    COCKROACH_DB_URL (gitignored — never commit)
├── .env.example            template
├── .mcp.json               CockroachDB Cloud MCP server, cluster id pre-filled
├── LICENSE                 MIT
├── Makefile                install / init / seed / test / ui / ui-aws / ui-locked / deploy / selfcheck
├── requirements.txt        langchain-cockroachdb, langchain-core, langgraph, psycopg[binary], boto3, python-dotenv
├── docs/                   architecture.md · mcp-usage.md · devpost-submission.md · completion-audit.md (this file)
├── skills/
│   ├── README.md
│   └── audit-memory/SKILL.md    Agent Skill, upstream frontmatter format, 8 verified query groups
├── src/
│   ├── config.py           env loading; derives cockroachdb:// and postgresql:// from one URL
│   ├── memory.py           all three CockroachDB memory types + HashEmbeddings + audit log
│   ├── graph.py            LangGraph: ingest_contract → retrieve_memory → analyze_contract → save_state
│   ├── llm.py              mock clause engine (12 regex patterns) + real LLM path
│   ├── prompts.py          shared DECISION_SCHEMA for both analysers
│   ├── s3.py               artifact storage + clause chunker
│   └── lambda_function.py  AWS entry point; validates at the trust boundary
├── scripts/
│   ├── init_db.py          creates all tables (idempotent)
│   ├── seed_data.py        loads rules + risks, probes retrieval and asserts non-empty
│   ├── test_graph.py       end-to-end with persistence assertions
│   ├── serve.py            stdlib web server: landing, login, app, all APIs
│   ├── deploy_aws.sh       idempotent Lambda + S3 + IAM deploy
│   └── destroy_aws.sh      teardown
├── ui/
│   ├── landing.html        public marketing page, live stats from the DB
│   ├── login.html          passcode gate (only active when APP_PASSCODE is set)
│   ├── app.html            Review / Dashboard / Settings, hash-routed
│   ├── theme.css           shared design system, light + dark
│   └── theme.js            shared theme toggle
└── sample_data/            freelancer_rules.txt · risk_patterns.txt · risky_contract.txt
```

### The memory model (the core of the submission)

| Kind | Question it answers | Primitive | Tables |
|---|---|---|---|
| Semantic | What do I believe? | `AsyncCockroachDBVectorStore` | `guardian_memory` (namespaces `rules`, `risks`) |
| Episodic | What happened with this client? | `CockroachDBChatMessageHistory` | `guardian_chat_history` |
| Procedural | Where did the workflow get to? | `AsyncCockroachDBSaver` | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` |
| Audit | What did it decide, and why? | plain SQL, append-only | `agent_audit_log` |

### Live infrastructure

```
CockroachDB   freelance-guardian · BASIC · AWS ap-south-1 · v26.2.5 · cluster bfcceaa8-bfe3-44b4-9542-1598dc21450b
AWS Lambda    freelance-guardian · python3.11 · arm64 · 1536MB · ap-south-1 · account 933036664603
AWS S3        freelance-guardian-933036664603 (private, contracts archived per client)
AWS IAM       freelance-guardian-lambda-role (s3:PutObject/GetObject on that bucket only)
GitHub        https://github.com/hemanthreddykoduru/AWS-Hackathon  (exists, empty)
```

### Known-good state (verified 2026-08-08)

```
make selfcheck            → ok (offline, no DB, no keys)
scripts/init_db.py        → ok — all tables ready (idempotent)
scripts/seed_data.py      → 14 rules + 12 risks, retrieval probe returns correct hits
scripts/test_graph.py     → 93/reject · 11 findings · audit+chat+checkpoints all grew
make ui-aws               → reviews execute on the deployed Lambda, artifacts land in real S3
```

---

## 2. Hackathon requirements (verified from the rules page)

Source: <https://cockroachdb-ai.devpost.com/>

- **At least TWO** of these CockroachDB tools:
  1. CockroachDB Cloud Managed MCP Server
  2. CockroachDB Distributed Vector Indexing
  3. ccloud CLI (Agent-Ready)
  4. CockroachDB Agent Skills Repo (open source)
- **At least ONE** AWS service (Bedrock, Lambda, ECS/EKS, S3, SageMaker, …)
- **Public open-source repo**, MIT or Apache 2.0, with README, dependencies, setup/run instructions
- **Functional demo app URL**
- **Video under 3 minutes** on YouTube or Vimeo, showing the CockroachDB memory layer in action
- **Documentation** naming which CockroachDB tools and which AWS services were used, and how
- Optional: architectural diagram

Judging: Agentic Memory Design · Technical Implementation · Real-World Impact ·
Production Readiness · Creativity & Originality.

---

## 3. Gap analysis

Status key: ✅ Wired · ⚠️ Documented-only · ❌ Missing

| Requirement | Status | Evidence | Gap | Fix |
|---|---|---|---|---|
| **Distributed Vector Indexing** | ❌ | `SHOW CREATE TABLE guardian_memory` → `embedding VECTOR(1536)` but only `INDEX guardian_memory_namespace_idx`. No vector index exists. | Similarity search is a full scan; the named tool is unused | **M2** |
| **Managed MCP Server** | ⚠️ | `.mcp.json` ships with cluster id; `docs/mcp-usage.md`; `skills/audit-memory/SKILL.md` with 8 query groups all run against the live cluster | Used interactively during development, never by application code | **S5** |
| **ccloud CLI** | ❌ | `command -v ccloud` → not installed. Zero references in the codebase. | Entirely unused | **S1** |
| **Agent Skills Repo** | ⚠️ | Own skill in correct upstream format; `skills/README.md` references `cockroachlabs/cockroachdb-skills` | Upstream skills referenced, not installed or exercised | **N1** |
| **AWS Lambda** | ✅ | `freelance-guardian` live in ap-south-1; every review executes there when `LAMBDA_FUNCTION` is set | — | — |
| **AWS S3** | ✅ | `s3://freelance-guardian-933036664603/contracts/<client>/<ts>-<hash>.txt`; URI stored in `agent_audit_log.contract_uri` | — | — |
| **Public demo URL** | ❌ | Lambda Function URL returns 403 to anonymous callers. Proven account-wide, not app-specific: a hello-world Lambda with identical public config returned the same 403. | **Explicit submission requirement** | **M3** |
| **Public repo** | ❌ | `git rev-parse --show-toplevel` → `/Users/hemanthmacbook`; remote is `SalauddinShaik001/My-Website.git`. The project is untracked files inside an unrelated repo. | No repository exists | **M1** |
| **MIT license visible** | ⚠️ | `LICENSE` present and correct | GitHub About won't show it until pushed | **M1** |
| **Kill-and-resume demo** | ❌ | `checkpoints` has 76 rows for one thread, but resume was never exercised | Claim is unproven | **S2** |
| **Video** | ❌ | Nothing recorded | **Explicit requirement** | **M4** |
| **Devpost text** | ⚠️ | `docs/devpost-submission.md` is complete prose with a 3-minute demo script | `<REPO_URL>` and `<VIDEO_URL>` placeholders | **M5** |
| **Architecture diagram** | ⚠️ | ASCII flow diagram in `docs/architecture.md` | No image | **S3** |
| **Credentials hygiene** | ❌ | AWS access key `AKIA5SPKRU4NQN2ZUOXA` and the CockroachDB SQL password were both pasted into a chat session | Both must be treated as public | **S4** |

**Headline: 0 of 4 CockroachDB tools are at ✅ Wired. Two are required.**
M2 and S1 are the cheapest path to two hard ✅.

---

## 4. Implementation plan

### 🔴 MUST HAVE — without these the submission is invalid

---

#### M1 · Create the public repo — 10 min

```bash
cd "/Users/hemanthmacbook/Desktop/AWS X COCKROCH/freelance-guardian"
git init
git add -A
git commit -m "Freelance Guardian: agentic contract memory on CockroachDB + AWS"
git remote add origin https://github.com/hemanthreddykoduru/AWS-Hackathon.git
git branch -M main
git push -u origin main
```

**Critical check before pushing** — `.env` holds a live database password:

```bash
git ls-files | grep -c '^\.env$'      # MUST print 0
git ls-files | grep -E 'env|secret'   # should show only .env.example
```

**Verify:** GitHub shows the README and an MIT badge. Set the About section:
description, and topics `cockroachdb`, `aws-lambda`, `langgraph`, `agentic-memory`, `mcp`.

---

#### M2 · Wire Distributed Vector Indexing — 20 min — **makes tool #1 ✅**

CockroachDB's C-SPANN index is one of the four named tools. Right now there is no vector
index at all, so every `asimilarity_search` scans the whole table.

Edit **`scripts/init_db.py`**. Add the import at the top:

```python
from langchain_cockroachdb import CSPANNIndex, DistanceStrategy
```

and after the `ainit_vectorstore_table` block, inside `main()`:

```python
        # 1b. C-SPANN: CockroachDB's distributed vector index. Without it every
        # similarity search is a sequential scan of guardian_memory.
        print(f"vector index   C-SPANN on {config.VECTOR_TABLE}.embedding …")
        await memory.vector_store(engine, config.NS_RULES).aapply_vector_index(
            CSPANNIndex(
                distance_strategy=DistanceStrategy.COSINE,
                name="guardian_memory_embedding_idx",
            )
        )
```

The API was confirmed against the installed package:
`AsyncCockroachDBVectorStore.aapply_vector_index(index: CSPANNIndex, prefix_columns=None)`
and `CSPANNIndex(distance_strategy=..., min_partition_size=None, max_partition_size=None, name=None)`.

**Verify:**

```bash
.venv/bin/python scripts/init_db.py
.venv/bin/python -c "
import asyncio,sys; sys.path.insert(0,'.')
from sqlalchemy import text
from src import memory
async def m():
    e=memory.get_engine()
    async with e.engine.connect() as c:
        print((await c.execute(text('SHOW CREATE TABLE guardian_memory'))).all()[0][1])
    await e.aclose()
asyncio.run(m())"
```

Expect `VECTOR INDEX guardian_memory_embedding_idx` in the output. Also confirm
`scripts/test_graph.py` still passes — retrieval results should be unchanged.

---

#### M3 · Public demo URL — 3–4 h — **the largest piece of remaining work**

**Why the obvious route is closed:** Lambda Function URLs return 403 to anonymous callers
on this AWS account. This was proven to be account-wide, not application-specific, by
deploying a hello-world Lambda with an identical public URL configuration — same 403. Auth
type is `NONE` and the resource policy is correct. Only AWS can lift it.

**API Gateway does not use Function URLs, so the block does not apply.** That is the route.

**Blocker to solve first:** the site calls `/api/memory`, `/api/dashboard`,
`/api/memory/items` and `/api/review`. Only `/api/review` exists in the Lambda; the rest
live in `scripts/serve.py`. A static S3 site cannot serve them.

##### M3a — extract shared data functions

Create **`src/api.py`** and move these functions out of `scripts/serve.py` verbatim,
renaming off the leading underscore:

- `_memory_stats` → `memory_stats`
- `_dashboard` → `dashboard`
- `_list_items` → `list_items`
- `_add_item` → `add_item`
- `_delete_item` → `delete_item`

They currently reference `LAMBDA_FUNCTION`/`LAMBDA_REGION` for the `backend` field — pass
that in as an argument instead so `src/api.py` has no dependency on the server module.
Then have `scripts/serve.py` import from `src.api` rather than defining them. One
implementation, two callers.

##### M3b — route them in the Lambda

Edit **`src/lambda_function.py`**:

```python
from . import api, graph

def handler(event, context=None):
    event = event or {}
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "POST")
    path = (event.get("rawPath") or event.get("path") or "/api/review").rstrip("/")

    if method == "OPTIONS":
        return _reply(204, {})

    try:
        if path.endswith("/api/memory"):
            return _reply(200, asyncio.run(api.memory_stats(backend="AWS Lambda")))
        if path.endswith("/api/dashboard"):
            return _reply(200, asyncio.run(api.dashboard()))
        if path.endswith("/api/sample"):
            return _reply(200, {
                "contract_text": SAMPLE_CONTRACT,
                "client_name": "Apex Dynamics LLC",
            })
    except Exception as exc:                       # noqa: BLE001
        logging.exception("api call failed")
        return _reply(503, {"error": f"{type(exc).__name__}: {exc}"})

    # ... existing /api/review path unchanged below
```

`sample_data/risky_contract.txt` is not in the Lambda package — either add
`sample_data/` to the deployment zip in `scripts/deploy_aws.sh`, or inline the sample as a
constant. Adding the directory to the zip is one line and keeps a single source of truth.

**Decide before building:** whether Settings (add/delete memory) should be reachable from
the public demo. Recommendation is **no** — a public write endpoint lets anyone edit the
agent's beliefs. Serve Settings only from the local server, and let the public demo be
read + review. Say so in the README rather than silently omitting it.

##### M3c — create the HTTP API

```bash
REGION=ap-south-1
ACCOUNT=933036664603
FN=freelance-guardian

API_ID=$(aws apigatewayv2 create-api \
  --name freelance-guardian-api \
  --protocol-type HTTP \
  --region $REGION \
  --target arn:aws:lambda:$REGION:$ACCOUNT:function:$FN \
  --cors-configuration AllowOrigins='*',AllowMethods='*',AllowHeaders='content-type' \
  --query ApiId --output text)

aws lambda add-permission \
  --function-name $FN --region $REGION \
  --statement-id apigw-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/*"

echo "API base: https://$API_ID.execute-api.$REGION.amazonaws.com"
```

`--target` creates the default route and `$default` stage automatically.

**Verify:**

```bash
curl -s "https://$API_ID.execute-api.$REGION.amazonaws.com/api/memory"
curl -s -X POST "https://$API_ID.execute-api.$REGION.amazonaws.com/api/review" \
  -H 'content-type: application/json' \
  -d '{"contract_text":"Unlimited revisions. Net 60. No deposit.","client_name":"API GW Test"}'
```

##### M3d — host the UI on S3

```bash
SITE=freelance-guardian-demo-933036664603

aws s3api create-bucket --bucket $SITE --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1
aws s3api delete-public-access-block --bucket $SITE
aws s3 website s3://$SITE --index-document landing.html --error-document landing.html
aws s3api put-bucket-policy --bucket $SITE --policy "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[{\"Effect\":\"Allow\",\"Principal\":\"*\",
    \"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$SITE/*\"}]}"
aws s3 sync ui/ s3://$SITE/ --exclude '.*'

echo "Demo URL: http://$SITE.s3-website.ap-south-1.amazonaws.com"
```

This bucket is deliberately public and holds **only** static UI files. It is a different
bucket from `freelance-guardian-933036664603`, which holds archived contracts and must
stay private.

##### M3e — point the UI at the API

The pages currently call same-origin paths. Add one constant near the top of the inline
script in **both** `ui/landing.html` and `ui/app.html`:

```javascript
// Empty string = same origin (local server). Set to the API Gateway base URL for the
// static S3 deployment, where there is no server to call.
const API = '';
```

then prefix every `fetch('/api/...')` with `` `${API}/api/...` ``. Set `API` to the API
Gateway base URL before `aws s3 sync`. Also drop the `/login` link and the Settings tab
from the S3 build if you followed the M3b recommendation.

**Verify the whole thing:** open the S3 website URL on a phone, on mobile data, **with
your laptop closed**, and run a full review. That is the actual requirement.

---

#### M4 · Record the video — 2 h

Under 3 minutes, public on YouTube. `docs/devpost-submission.md` already contains a timed
six-beat script. The rules specifically ask to see **the CockroachDB memory layer in
action**, so the video must include:

1. The problem — the sample contract and its planted traps
2. Empty memory — `SELECT count(*) FROM agent_audit_log` over MCP
3. `make seed` — 14 rules, 12 risks, retrieval probe
4. A review in the UI — 93/reject, contract redlined, "Memory consulted" expanded
5. **Run it twice** — `prior messages` goes 0 → 2. This is the money shot.
6. **The audit question over MCP** — *"which clause gets flagged most across my clients?"*
   → Unlimited revisions, N times across M clients. Close on this.

If S2 is done, add the kill-and-resume between 5 and 6.

---

#### M5 · Fill in the Devpost submission — 45 min

`docs/devpost-submission.md` has every field written. Replace `<REPO_URL>` and
`<VIDEO_URL>`, add the demo URL, then paste each section into the form. Make sure the
"which CockroachDB tools and how" answer names the **two** tools that are actually ✅ after
M2 and S1 — do not claim a tool that is only documented.

---

### 🟡 SHOULD HAVE — these raise judging scores

---

#### S1 · Wire the ccloud CLI — 45 min — **makes tool #3 ✅**

```bash
brew install cockroachdb/tap/ccloud
```

Create **`scripts/cluster_info.sh`**:

```bash
#!/usr/bin/env bash
# Reports live cluster state using the ccloud CLI (Agent-Ready), alongside the
# application's own view of its memory. Read-only.
set -euo pipefail

CLUSTER="${CLUSTER:-freelance-guardian}"

ccloud auth login --core
echo "=== clusters ==="
ccloud cluster list

echo
echo "=== semantic memory ==="
ccloud cluster sql "$CLUSTER" --execute \
  "SELECT namespace, count(*) AS entries FROM guardian_memory GROUP BY 1 ORDER BY 1;"

echo
echo "=== decisions on record ==="
ccloud cluster sql "$CLUSTER" --execute \
  "SELECT count(*) AS reviews, count(DISTINCT client_name) AS clients FROM agent_audit_log;"
```

**Verify:** `bash scripts/cluster_info.sh` prints the cluster and the row counts.
Document it in the README under a "CockroachDB tools used" heading.

---

#### S2 · Prove kill-and-resume — 1 h — **scores directly on Agentic Memory Design**

The procedural-memory claim ("kill it mid-run and it resumes") is currently untested.

Create **`scripts/test_resume.py`**:

1. Build the graph with an `analyze_contract` node that raises on first call.
2. Invoke with a fixed `thread_id`; assert it raises.
3. Assert `SELECT count(*) FROM checkpoints WHERE thread_id = :t` is greater than zero.
4. Re-invoke the same `thread_id` with the failure removed.
5. Assert the run completes **and** that `retrieve_memory` did not run a second time —
   easiest via a counter incremented inside the node.
6. Print `resumed from checkpoint N`.

**Verify:** `.venv/bin/python scripts/test_resume.py` exits 0. Record it for the video.

---

#### S3 · Architecture diagram — 45 min

One SVG at `docs/architecture.svg`, embedded at the top of the README:
contract → Lambda → the three memory types inside one CockroachDB cluster → S3 for
artifacts → MCP as the outbound inspection surface. Keep the palette consistent with
`ui/theme.css` (ink `#171412`, accent `#F26522`, paper `#FAF9F6`).

---

#### S4 · Rotate both credentials — 15 min

Both were pasted into a chat session and must be treated as public.

1. **AWS:** IAM → Users → `PrajnaDev` → Security credentials → deactivate and delete
   `AKIA5SPKRU4NQN2ZUOXA`, create a new key, `aws configure`.
   This account also runs unrelated Prajna stacks, so the exposure is not theoretical.
2. **CockroachDB:** cluster → Connect → Regenerate password. Update `.env`, then
   `make deploy` so the Lambda environment variable picks up the new value.

---

#### S5 · Make MCP usage self-evident — 30 min

The MCP server is genuinely used, but a judge has to set it up themselves to see it.
Add `docs/mcp-transcript.md`: a real session showing `list_tables`, `get_table_schema` on
`guardian_memory`, and the clause-frequency query with its actual output. Link it from
the README. This converts ⚠️ into visible evidence without new code.

---

### 🟢 NICE TO HAVE

- **N1** — add `npx skills add cockroachlabs/cockroachdb-skills` to the README setup steps,
  and a one-line symlink command for `skills/audit-memory`. Strengthens tool #4.
- **N2** — delete demo junk before recording:
  ```sql
  DELETE FROM agent_audit_log
  WHERE client_name IN ('Lambda Live Test','Public URL Test','Proxy Demo Co',
                        'Memory Proof Co','Final Test Co','Pages Regression Co','API GW Test')
     OR client_name LIKE 'Evil%';
  DELETE FROM guardian_chat_history WHERE session_id LIKE 'evil-%';
  ```
  Then run 3–4 reviews across 3 plausible client names so the dashboard tells a story.
- **N3** — GitHub About: description, topics, and the demo URL in the website field.

---

## 5. Ten-day sprint

One action per day, each ≤3 hours.

| Day | Date | Action | Done when |
|---|---|---|---|
| 1 | Aug 8 | **M1** git init + push · **S4** rotate both credentials · **N3** repo About | GitHub shows the code with an MIT badge; old keys are dead |
| 2 | Aug 9 | **M2** vector index · **S1** install and wire ccloud | Two tools at ✅; `SHOW CREATE TABLE` proves the index |
| 3 | Aug 10 | **M3a + M3b** extract `src/api.py`, route it in the Lambda | `aws lambda invoke` returns dashboard JSON |
| 4 | Aug 11 | **M3c** API Gateway + redeploy | `curl` against the API URL returns 200 |
| 5 | Aug 12 | **M3d + M3e** S3 static site, point the UI at the API | Phone on mobile data runs a review, laptop closed |
| 6 | Aug 13 | **S2** kill-and-resume test | `test_resume.py` exits 0 |
| 7 | Aug 14 | **S3** diagram · **N2** clean data and reseed a believable dataset · **S5** MCP transcript | README renders the diagram |
| 8 | Aug 15 | **M4** record video — rehearse twice, record once | Raw footage covers all six beats |
| 9 | Aug 16 | **M4** edit and upload, under 3:00, set to public | Public YouTube link |
| 10 | Aug 17 | **M5** submit on Devpost · full dry run from the public URL only | Devpost shows "Submitted" |

**Aug 18 is buffer, not a work day.**

---

## 6. Rules for whoever executes this

- **Never assume code exists.** Open the file. This audit exists because several things
  were documented but not wired.
- **Never commit `.env`.** It contains a live database password.
- **Never propose paid services.** The submission must be free to run and demo.
  API Gateway HTTP API, Lambda, S3 and CockroachDB Basic are all free tier.
- **Verify against the live cluster** with the MCP tools (`list_tables`,
  `get_table_schema`, `select_query`) or `ccloud` before claiming a schema fact.
- **Do not claim a tool is used unless it is wired.** Judges can read the repo. An
  overstated claim costs more than an honest gap.
- If a library's README disagrees with the installed package, **trust the package** —
  `ainit_chat_history_table()` is documented upstream and does not exist.
