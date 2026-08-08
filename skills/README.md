# Agent Skills

Machine-executable skills for working with Freelance Guardian's memory layer.

## In this repo

| Skill | What it does |
|---|---|
| [`audit-memory/`](audit-memory/SKILL.md) | Interrogate the agent's decision log, semantic memory, client threads, and LangGraph checkpoints over the CockroachDB Cloud MCP Server. Includes tamper checks. Every query in it has been run against a live cluster. |

Install it the same way as any skill:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/audit-memory" ~/.claude/skills/audit-memory
```

Then ask your agent: *"audit the Freelance Guardian memory"* — or simply *"which contract
clause gets flagged most often across my clients?"*, which is the question the skill exists
to answer.

## Official CockroachDB skills

The 29 skills in [cockroachlabs/cockroachdb-skills](https://github.com/cockroachlabs/cockroachdb-skills)
cover schema design, transactions, multi-region, performance, and operations. Install them
alongside this one:

```bash
npx skills add cockroachlabs/cockroachdb-skills
```

**They are referenced, not vendored.** Copying 29 upstream skills into this repo would add
thousands of lines nobody here maintains, and they would be stale the first time Cockroach
Labs ships a fix. One install command stays current; a fork does not.

The ones most relevant to this project, if you want a starting subset:

- `cockroachdb-query-and-schema-design/cockroachdb-sql` — the SQL behind `agent_audit_log`
- `cockroachdb-application-development/designing-application-transactions` — retry and pooling behaviour, which `CockroachDBEngine` handles for us
- `cockroachdb-performance-and-scaling` — vector index tuning as `guardian_memory` grows
- `cockroachdb-observability-and-diagnostics` — for when a query gets slow

## Writing your own

Follow the upstream format: a directory containing `SKILL.md`, with YAML frontmatter
carrying `name`, `description`, `compatibility`, and `metadata`. The `description` is what
an agent matches against when deciding whether to load the skill, so write it as a list of
the situations it applies to, not as a summary of its contents.
