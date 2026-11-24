# EverOS MemTrace: developer instrumentation guide

MemTrace is work from the ZJUNLP lab. It adds a replayable "memory pipeline"
to EverOS: the key data of a conversation — from write, organization and
persistence through to the next retrieval and answer — is chained together
and exported as execution graphs that can be viewed, merged and consumed by
attribution tooling.

This guide is for engineers who develop, debug or evaluate EverOS. It covers:

1. what the feature gives EverOS;
2. how to enable it locally and find the output;
3. where to add records when you introduce a new memory path or search stage.

## Why instrument EverOS

EverOS memory is not a simple "write to a database". A user message may
first land in a buffer, get cut into memcells by boundary detection, be
extracted into episodes, settle as Markdown, and finally be recalled by
another request. Looking only at the final Markdown or the search results
makes it hard to tell which step went wrong. That is the core problem
MemTrace solves.

MemTrace puts these steps on one traceable chain:

```mermaid
flowchart LR
    M[User message] --> B[Message buffer]
    B --> D[Boundary detection]
    D --> C[memcell]
    C --> E[Memory extraction]
    E --> P[Markdown memory]
    P --> R[Retrieval candidates]
    R --> H[Hits and context]
    H --> A[Answer and evaluation (optional)]
```

The practical value for EverOS:

| Value | What it means for a developer |
| --- | --- |
| Faster memory debugging | Distinguishes "not extracted correctly", "updated or deleted", "not recalled" and "the answer stage used the wrong context", cutting the time spent guessing from logs. |
| Explainable Markdown memory | Markdown stays the source of truth; the execution graph adds the provenance of "how this memory was produced and when it was referenced" — it does not replace Markdown, SQLite or LanceDB. |
| Quality regression support | The same graph can replay failed Q&A, compare candidate-pool changes, and feed MemTrace error attribution. |
| Low instrumentation cost | The business layer only submits real inputs, outputs and dependencies; `smartcomment` owns graphs, versions and export, so developers do not maintain their own trace JSON. |
| Friendly to the online path | This is an optional capability. Without `smartcomment`, or with the switch off, the hooks degrade to no-ops; a collection failure never fails a memory request. |

## Getting started

### Installation and the switch

Install the optional dependency in an EverOS checkout:

```bash
uv sync --frozen --extra memtrace
```

MemTrace is off by default and is enabled per process when needed:

```bash
export EVEROS_MEMTRACE_ENABLED=1
```

Use it for debugging and evaluation only, not in production. The graphs hold
raw message and memory text, and each export carries a latency and disk cost.
`/memory/delete` and purge do clean the export (the scope's graphs are
dropped and its entries redacted elsewhere), but data deleted while tracing
was off leaves no signal the tracker can follow — remove the export directory
yourself in that case. See
[memtrace-graphs.md](memtrace-graphs.md#enabling-tracing) for the full
caveat list.

Graphs are written to `<memory-root>/memtrace/` by default. For debugging,
point at a separate directory:

```bash
export EVEROS_MEMTRACE_ENABLED=1
export EVEROS_MEMTRACE_DIR=/tmp/everos-memtrace
```

Start EverOS and run a few `/memory/add`, `/memory/flush` and
`/memory/search` requests. Afterwards one or more `.json` graph files appear
in that directory. Request hooks only record in memory; the markdown diff
and the exports run on a coalescing background worker, and the API lifespan
runs a final sync at shutdown to catch the last markdown changes. Set
`EVEROS_MEMTRACE_SYNC_MODE=inline` to put the diff back on the request path
(useful for deterministic local checks).

### A quick local check with the graphs

Export the graphs to PNG first to confirm messages, memories and search
results connect:

```bash
.venv/bin/python examples/memtrace/export_png.py \
  /tmp/everos-memtrace --out /tmp/everos-memtrace-png
```

By default each query gets one image showing only the top two candidates of
each pool; the source JSON is not modified. Use `--top-k 0` for complete
candidate pools and `--combined` for a single overview image.

### Handing failed Q&A to the attribution tool

`/memory/search` only returns memories; it does not produce the final
answer, so answers and evaluations are recorded by the caller. HTTP
experiments usually run after the service stops:

```bash
.venv/bin/python examples/memtrace/inject_qa.py \
  --graph-dir /tmp/everos-memtrace \
  --qa-results /path/to/qa_results.json

.venv/bin/python examples/memtrace/merge.py /tmp/everos-memtrace \
  --output /tmp/everos-merged.json

.venv/bin/python examples/memtrace/attribute.py \
  --graph /tmp/everos-merged.json --list-cases
```

`inject_qa.py` attaches the prompt (optional), the actual answer, the
reference answers and the judge result to the matching query. `merge.py`
merges construction and retrieval graphs; do not write the merged result
back into the input directory. See
[examples/memtrace/README.md](../examples/memtrace/README.md) for more
attribution options.

## What gets recorded

Recording concentrates on the business boundaries users actually care
about, rather than writing every function call into the graph.

| Scenario | What the graph shows | Questions it answers |
| --- | --- | --- |
| `/memory/add` | Input messages, buffer changes, boundary results, memcells, episode extraction results, memories persisted successfully | Was this message accepted, split and extracted? |
| `/memory/flush` | The remaining buffer, the final boundary result, the flush request — recorded even when no new file appears | Why has this batch not formed memories yet? |
| `/memory/search` | Query, sparse/dense/fusion candidate pools, actual hits, retrieval context, the unprocessed-message branch | Was the memory never generated, or just not recalled? Which step changed the candidates? |
| Markdown observation | Creation, update and deletion of episode, atomic fact, foresight, agent case, profile, skill and knowledge files | When did the on-disk facts change? |
| Request failure | Failure status of add, flush and search with bounded error info; a failed search keeps the candidate stages collected so far | How far did the request get before failing? |
| Answer and evaluation (optional) | Prediction, reference answers, judge response, and the context the answer actually used | Is the error a retrieval problem or an answer-stage problem? |

Construction and search graphs are partitioned by `app_id`, `project_id`,
owner and session bucket. The same business object keeps a stable identity,
and content changes form new versions, so you see a memory evolve rather
than only its final state.

## Reading a graph

You do not need smartcomment's internal schema first:

1. **Find the query or the input messages.** They are the request's start.
2. **Follow the arrows along the business chain.** The typical path is
   "message → buffer → boundary result → memcell → extraction result →
   Markdown memory → candidate pool → hit → context".
3. **Check versions and provenance.** `construction_event` means captured
   live inside a business step; `file_diff` means a change noticed by the
   markdown scan at a request boundary — it proves "a change was observed",
   not which request caused it.
4. **Look at the answer chain last.** Predictions, golden answers and judge
   responses only appear once the caller has recorded them.

A candidate pool is a stage snapshot; by default it keeps at most 50 ids,
scores and sources, plus `total_count` and `truncated`. So "a candidate is
absent from the graph" does not mean "it was never recalled". A hit is the
result of this query; the persistent memory is the historical revision it
references — do not conflate the two.

Empty results, skips, failures and "not recorded" are different situations:

- `deferred` means the messages entered the buffer first, with boundary
  detection and extraction postponed;
- `no_split` means this round cut no memcell; a result node should still
  exist;
- `status: error` means the request failed — the business exception is
  still re-raised;
- a missing node may simply mean that path is not instrumented yet, not
  that it never ran.

## smartcomment in a nutshell

`smartcomment` is an execution-graph recorder we built. It does not
automatically intercept EverOS functions, file writes or LLM calls — EverOS
declares records explicitly at real business boundaries:

- **Variables**: messages, buffers, memories, candidate pools, context and
  other business data;
- **Operations**: add, boundary detection, extraction, retrieval, update,
  deletion and other business steps;
- **Dependency edges**: which input produced which output;
- **Versions**: how one memory or buffer changed over time.

Graphs are exported in smartcomment's JSON format: readable with
`ExecNetwork.import_graph()` and consumable by MemTrace's graph-exploration
and attribution tools. EverOS's `MemTraceTracker` handles request grouping,
markdown scanning, restart recovery, file locking and export; business code
only submits data through the tracker or the trace collector.

## Costs and limits

- **It is not zero-cost logging.** Request hooks record in memory and a
  background worker diffs the memory root and serializes dirty graphs;
  enabling it adds some latency and disk usage. The first scan after a
  start parses every markdown file.
- **It is not a full recording.** It does not automatically capture every
  LLM prompt, raw response, retry round, OME-internal evolution, each
  LanceDB/embedding index step, or the answering model.
- **The file diff is post-hoc observation.** An intermediate state written
  and overwritten between two scans may be invisible; `file_diff` alone
  cannot prove causality.
- **Multiple processes need a write plan.** The file lock keeps exports
  from being corrupted but does not merge different workers' in-memory
  graphs. In production choose a single writer, or give each worker its own
  `EVEROS_MEMTRACE_DIR`.
- **Graphs are bounded.** Exports are pruned to
  `EVEROS_MEMTRACE_MAX_GRAPH_NODES` nodes per graph (default 20000, 0
  disables); the oldest retrieval sessions are pruned first, then
  superseded versions, and the latest state always survives.
- **Graphs may contain raw text.** Message, memory and answer content can
  appear in the JSON. Deleting or purging memories removes or redacts the
  matching nodes, but set directory permissions to your data-sensitivity
  level and sanitize before sharing graphs or attaching them to issues.

If a project only needs EverOS's normal memory capability, skip the
`memtrace` extra; the core path runs unchanged. The main payoff of
installing and enabling it is an extra evidence chain when you ask "why is
this memory wrong" or "why is this answer wrong".

## Verifying changes

Run the tests most relevant to this feature in an EverOS checkout:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_service/test_memorize_memtrace_hook.py \
  tests/unit/test_service/test_memtrace_delete_hook.py \
  tests/unit/test_memory/test_extract/test_trace.py \
  tests/unit/test_memory/test_search/test_trace.py \
  tests/unit/test_infra/test_memtrace_no_split.py \
  tests/unit/test_infra/test_memtrace_boundary_decision.py \
  tests/unit/test_infra/test_memtrace_buffer_lifecycle.py \
  tests/unit/test_infra/test_memtrace_regressions.py \
  tests/unit/test_infra/test_memtrace_delete_purge.py
```

When adding instrumentation, add at least one success, empty-result or
failure test for it, and check the exported JSON can be re-read by
`smartcomment.runtime.ExecNetwork.import_graph()`. See
[memtrace-graphs.md](memtrace-graphs.md) for a fuller graph-attribution
example.
