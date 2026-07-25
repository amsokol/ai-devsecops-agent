# ai-devsecops-agent

DevSecOps agent that reviews proposed changes and maintains a repository's default branch. It is the
executing half of a two-part system: all judgement lives in the knowledge library
[`ai-devsecops-skills-knowledge`](https://github.com/amsokol/ai-devsecops-skills-knowledge), and each
product supplies its own overlay of values and invariants.

The agent owns everything that must be exact and reproducible: turning a trigger into a plan of
parallel tasks, giving those tasks deterministic tools, aggregating their results into a verdict, and
acting once on the hosting platform. It contains no severity criteria, no quarantine rules and no
ecosystem procedures — those are prose in the library.

## Design

Start with [`DESIGN.ru.md`](DESIGN.ru.md) (Russian), which covers the architecture, the model
selection per role, the agent SDKs behind one narrow port, budgets and degradation, security
boundaries, and the implementation stages. The exchange contract between library and agent is
`CONTRACT.md` in the library.

## Usage

```bash
uv sync
uv run agent review  --repo . --change 12 --base main --library ./library
uv run agent maintain --repo . --library ./library [--scheduled]
uv run agent explain --run <run-id>
uv run agent pin --library ./library   # version and digest, to fill agent/config/library.yaml
```

This release runs on knowledge library `v0.1.5` and verifies it at startup: a library the pin does
not name is a configuration error, because a gate running on unverified knowledge cannot say what it
checked. The digest covers identity, index and document bodies rather than the directory, so a
checkout of the tag and the unpacked release artefact verify the same.

Local Cursor runs on a machine that cannot provide the SDK sandbox need `sandbox: false` in
`agent/config/execution.yaml` (or a `--config-dir` copy). The shipped default keeps the sandbox on.

`--plan-only` builds and prints the plan without executing tasks, which is the way to see what a
trigger would do. Add `--json` to print the run manifest instead of a summary, and `--no-cache` to
prove a verdict reproduces without the cache of immutable facts.

| Path | Meaning |
| --- | --- |
| `--library` | the knowledge library artefact, unpacked; verified against the pin in `agent/config/library.yaml` |
| `--overlay` | the product's overlay, default `<repo>/.devsecops`, holding `agent.yaml` and `NOTES.md` |
| `--run-dir` | where run records are written, default `.agent/runs`; publish it as a CI artefact |
| `--config-dir` | replaces the built-in configuration wholesale |

## Storage

Three kinds of persistence, separate because losing each one means something different.

| What | Where | Written by |
| --- | --- | --- |
| run record: manifest and evidence | `--run-dir`, published as a CI artefact | every run |
| cache of immutable facts | `.agent/cache`, a directory a CI cache can restore and save | only a run on the default branch |
| agent state | the git ref named in `agent/config/storage.yaml` | only a run on the default branch |

Only facts that cannot change are cached — a version's publication date, an artefact digest. Advisory
data and version lists never are, or a weekly run would stop noticing new advisories, which is the
reason the weekly run exists. Failures are not cached either: an unreachable host is not a fact about
a package.

Exit codes are an interface, because CI acts on them: `0` permitted, `5` blocked, `6` inconclusive,
`64` configuration error, `2` internal failure. A run that could not execute its tasks exits `6` and
never `0` — the absence of a result is not a result.

## Budgets

Analysis tasks are independent, so they run concurrently, and `agent/config/execution.yaml` states
what they may spend. Per task: wall clock and a ceiling on tool calls. Per run: how many sessions
overlap, and optionally a shared token ceiling. A scheduled run gets its own, tighter section — it
spends with nobody watching.

Every limit ends the same way. A task that hits one, or that the shared ceiling could not pay for, is
recorded as `exhausted`, and a required task in that state makes the run inconclusive. Nothing the
agent did not check is ever reported as checked, so the cheapest way to make a gate pass remains
fixing the code rather than starving the run.

A token ceiling only binds where the backend reports usage. Sessions that report nothing are counted
separately in the manifest rather than as zero, because a total that quietly omits them is not a
limit. There is no money ceiling: it needs a price per model, and a limit computed from prices nobody
maintains would refuse work for a wrong number.

The shipped ceiling is a guard against a run that stopped making progress, not a tight cap. For
scale, a measured four-task review of a small change costs about 3M tokens and 57 seconds with four
sessions overlapping; the ceiling sits several times above that, because a limit that bites on a
large change would turn real analysis into `exhausted` and teach the team to distrust the gate.

## Status

Stage 4: concurrency and budgets are in place on top of the decision path, the tool registry and the
Cursor SDK adapter. Every analyst capability the library defines can run, a run can still be narrowed
with `--only` (for example `deps-vuln@python-uv`), and GitHub actions, the `fixer` role and the eval
harness are still ahead. `backend: fake` remains available for CI that must exercise the pipeline
without a model.

The predecessor [`ai-devsecops-cursor`](https://github.com/amsokol/ai-devsecops-cursor) remains
frozen at its final tag for products that have not migrated.
