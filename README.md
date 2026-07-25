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
uv run agent review  --repo . --change 12 --base main --library ./library [--publish]
uv run agent maintain --repo . --library ./library [--scheduled]
uv run agent explain --run <run-id>
uv run agent pin --library ./library   # version and digest, to fill agent/config/library.yaml
```

This release runs on knowledge library `v0.2.0` and verifies it at startup: a library the pin does
not name is a configuration error, because a gate running on unverified knowledge cannot say what it
checked. The digest covers identity, index and document bodies rather than the directory, so a
checkout of the tag and the unpacked release artefact verify the same.

Local Cursor runs on a machine that cannot provide the SDK sandbox need `sandbox: false` under
`backends: cursor:` in `agent/config/models.yaml` (or a `--config-dir` copy). The shipped default
keeps the sandbox on.

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

## Roles and models

A task runs as a role, and `agent/config/models.yaml` says which backend and model answers for each
one. A model is named as a pair, because the backend decides which models exist: `composer-2.5` alone
is not an address. Only the roles a plan reaches are ever created, so a binding to an SDK this machine
has not installed costs nothing until something needs it — one configuration serves a laptop and a
pipeline.

What a role *requires* is not configurable. An analyst that cannot call the tool registry has nothing
to establish a fact with, so those needs live in `agent/roles.py` next to the code that has them. Each
adapter declares what it implements, and a binding whose backend falls short is a startup error naming
the missing ability. Modifying files is not among those abilities: `edit_file` is a tool like any
other, so a `fixer` needs the registry and nothing more, and the check happens before a maintenance
run spends anything rather than when the first fix is attempted.

One model everywhere for now. Choosing per role before the eval harness exists would be taste with a
version number attached. The manifest records which pair each role was bound to, so a later comparison
has something to compare.

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

## Fix branches

A maintenance run does not stop at reporting. Findings it can act on — demonstrated by reproducible
evidence, with a remedy stated — become fix tasks, in a fixed order with `security` first and within
the queue the overlay's `limits` allow. Every finding about one package pin or one file is one task,
because three advisories against one pin are one bump, and three branches carrying the same edit is
how a weekly run teaches a team to stop reading it.

Each task works in its own git worktree on its own branch, named after the class and the subject so
that the same problem lands on the same branch next week instead of a second pull request. The branch
name does not follow the advisory: which of a subject's advisories is the strictest changes as they
appear and get fixed, and a branch that moved under an unchanged repository would duplicate itself.

The subagent changes files and runs commands. Everything else is the agent's: staging, the commit
message derived from the finding, the branch, and later the pull request. That division is what keeps
a commit message from depending on how a model read an instruction, and `--dry-run` prints what a run
would fix without creating anything.

**Verification is read from the record, not from the answer.** The overlay groups commands into
surfaces; the library decides which surfaces a change affects, including couplings a changed path
does not reveal. The agent then matches the run's own log of executed commands: shipping needs one
surface run in full, with no failing command among any that ran. Partial credit is deliberately
unavailable — its first live run had three sessions make the same change, two run the cheapest command
of a five-command surface and ship, and one run all five, find a failure and refuse.

When a command does fail, the agent re-runs it with the change taken away. A failure that reproduces
on the unchanged head still stops the fix — a red branch is a branch nobody trusts, whoever made it
red — but the run reports the product's own checks as the cause and names them, so a week of refusals
is not read as the agent being unable to bump a dependency.

## Publishing a review

A review run says nothing on the platform unless it is asked to: `--publish` with `--change`. Local
runs report to the person who started them, and a pipeline that means to write in somebody's pull
request says so in its own command line. Publishing needs `gh` on PATH, and the target repository
comes from the checkout's own remote rather than from configuration, because a slug in a config file
eventually disagrees with the checkout and then a review lands on the wrong repository.

### Who the agent speaks as

The credential is read from the environment, in this order, and never from the account a machine
happens to be logged in as:

| Variable | Use |
| --- | --- |
| `AGENT_GITHUB_TOKEN` | the agent's own credential; first, so a laptop with a developer's token still publishes as the agent |
| `GH_TOKEN`, `GITHUB_TOKEN` | the client's own precedence, so there is no second rule to learn; in Actions the workflow's `GITHUB_TOKEN` posts as `github-actions[bot]` |

With none of them set, nothing is published and the run says why. That refusal is deliberate: left to
itself the client falls back to the stored login, and the first live check of this adapter published
five machine-written reviews under a person's name. The name is the smaller half of the problem. A
decision signed by a colleague cannot be told apart from that colleague's opinion — and a workflow
that wakes the agent on human comments while filtering bots cannot filter a human account, so the
agent's own comment wakes the agent, which comments, which wakes it again.

The run records the account it published as, and if the platform will show that account as a person
rather than a bot, `manifest.warnings` says so and names the filter the workflow needs. A dedicated
machine account is a legitimate setup, so this is a caution rather than a refusal.

For a repository of one's own, the workflow token is enough and needs no secret. For an agent that
several repositories share, a GitHub App is the better identity: comments arrive from
`<app>[bot]`, permissions are granted per installation rather than per person, and nobody's departure
takes the reviewer with it. Mint an installation token in the workflow and put it in
`AGENT_GITHUB_TOKEN`; nothing in the agent changes.

```yaml
permissions:
  contents: read
  pull-requests: write        # threads and review bodies; nothing else is needed
steps:
  - uses: actions/checkout@v5
    with:
      fetch-depth: 0          # the base is needed to know which lines the change touched
  - run: uv run agent review --change ${{ github.event.pull_request.number }} --base ${{ github.base_ref }} --publish
    env:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

A run posts one review body — the same text as `report.md` — carrying the stance the result earns:
`pass` approves, `blocked` requests changes, and `inconclusive` comments, because the check refuses
the merge either way and "changes requested" is a claim about the code that a run without a result has
not earned. Where GitHub refuses an approving review from a pull request's own author, the same text
arrives as a comment; merge authority was never the approval event.

Each finding with a line inside the diff also gets its own thread, and this is where the finding key
earns its keep:

| On a rerun | What happens |
| --- | --- |
| the finding is still there, unchanged | nothing at all — no second comment |
| the finding is still there, reworded | its existing thread is edited in place |
| the finding is gone and its capability finished `clean` | the thread is resolved, with a note saying why |
| the finding is gone but its capability failed | the thread stays open: absence in a run that did not look is not a fix |
| the comment has no marker | it belongs to a human and is never touched |

The anchor is an HTML comment holding the finding key. Comment identifiers change when a thread is
recreated and line numbers change whenever somebody edits the file above them; the key does not move.
A finding whose line is outside the diff stays in the review body, because the platform rejects a
comment there and, if it did not, the comment would blame whoever wrote that line years ago.

What the platform records is what the run reports. GitHub allows nobody to review their own pull
request, approving or requesting changes alike, so a run on a change the publishing account opened has
its decision recorded as a comment — and `manifest.actions` says so, rather than claiming a stance the
pull request does not show. `scripts/live_publish_check.py` drives all of this against a real scratch
change, which is how that behaviour, and the identity problem above, were found in the first place.

Two things stop publishing outright: a draft change, and a head that moved while the run was working —
comments derived from one commit and posted on another point at lines nobody proposed. A platform
failure is a warning rather than an end. The analysis is the expensive part and it is already done, the
exit code still carries the decision, and `manifest.actions` records every thread the run touched,
including the ones it deliberately left alone.

## Status

Stage 6, second slice: a review run publishes its decision on GitHub and reconciles its threads by
finding key. Before it, a maintenance run prepares verified fix branches locally, on top of the
decision path, the tool registry, the Cursor SDK adapter, role bindings, concurrency and budgets;
every analyst capability the library defines can run, and a run can be narrowed with `--only` (for
example `deps-vuln@python-uv`). What is still ahead: issues and fix pull requests for maintenance
runs, the restraints a scheduled run needs, and the eval harness. `backend: fake` remains available
for CI that must exercise the pipeline without a model.

The predecessor [`ai-devsecops-cursor`](https://github.com/amsokol/ai-devsecops-cursor) remains
frozen at its final tag for products that have not migrated.
