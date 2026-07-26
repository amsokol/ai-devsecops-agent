# ai-devsecops-agent

AI DevSecOps agent that reviews proposed changes and maintains a repository's default branch. It is the
executing half of a two-part system: all judgement lives in the knowledge library
[`ai-devsecops-skills-knowledge`](https://github.com/amsokol/ai-devsecops-skills-knowledge), and each
product supplies its own overlay of values and invariants.

The agent owns everything that must be exact and reproducible: turning a trigger into a plan of
parallel tasks, giving those tasks deterministic tools, aggregating their results into a verdict, and
acting once on the hosting platform. It contains no severity criteria, no quarantine rules and no
ecosystem procedures — those are prose in the library.

## Write-up

Long-form article on Medium (what the agent is, how evidence and gates work, live demo scenarios):
[AI Agents Driving the Future of Software Development and DevSecOps Platforms — Model-Agnostic, and It Shows Its Evidence](https://medium.com/@amsokol.com/ai-agents-driving-the-future-of-software-development-and-devsecops-platforms-model-agnostic-and-219d6c6ca14a).

## Design

Start with [`DESIGN.md`](DESIGN.md), which covers the architecture, the model selection per role,
the agent SDKs behind one narrow port, budgets and degradation, security boundaries, and the
implementation stages. The exchange contract between library and agent is `CONTRACT.md` in the
library. What changed between releases, and what a run will do differently afterwards, is in
[`CHANGELOG.md`](CHANGELOG.md).

## Usage

```bash
uv sync
uv run agent review  --repo . --change 12 --base main --library ./library [--publish]
uv run agent maintain --repo . --library ./library [--scheduled] [--publish]
uv run agent maintain --repo . --library ./library --wake-issue 7 --wake-comment 42 --actor <login>
uv run agent explain --run <run-id>
uv run agent pin --library ./library   # version and digest, to fill agent/config/library.yaml
```

This release runs on knowledge library `v0.5.1` and verifies it at startup: a library the pin does
not name is a configuration error, because a gate running on unverified knowledge cannot say what it
checked. The digest covers identity, index and document bodies rather than the directory, so a
checkout of the tag and the unpacked release artefact verify the same.

Local Cursor runs on a machine that cannot provide the SDK sandbox need `sandbox: false` under
`backends: cursor:` in `agent/config/backends.yaml` (or a `--config-dir` copy). The shipped default
keeps the sandbox on.

`--plan-only` builds and prints the plan without executing tasks, which is the way to see what a
trigger would do. Add `--json` to print the run manifest instead of a summary, and `--no-cache` to
prove a verdict reproduces without the cache of immutable facts. `--outside` reviews a change as if
its head came from a fork, which is how the restrained mode below is exercised deliberately; it can
only take permission away.

| Path | Meaning |
| --- | --- |
| `--library` | the knowledge library artefact, unpacked; verified against the pin in `agent/config/library.yaml` |
| `--overlay` | the product's overlay, default `<repo>/.devsecops`, holding `agent.yaml` and `NOTES.md` |
| `--run-dir` | where run records are written, default `<repo>/.agent/runs`; publish it as a CI artefact |
| `--config-dir` | replaces the built-in configuration wholesale |

## Which overlay a review obeys

The overlay is where the meaning of a finding is settled for this product: the quarantine window, the
documented exceptions, which ecosystems are examined at all, and `NOTES.md`, which enters every task's
prompt. A review therefore reads it **from the merge base**, not from the change under review. Read
from the checkout, one commit would be enough to set the quarantine to zero, drop the ecosystem whose
dependency it bumps, or add a line to the notes telling the model what to conclude — and the run would
carry that out while reporting a pass. The change is examined in full; it just does not get to write
the rules it is judged by, and the edit takes effect the moment it is merged.

When the base version is not the visible one, the review says so in its first line and the manifest
records which commit the overlay came from. Two cases fall back to the checkout and both are named:
an overlay kept outside the repository is not part of any change, and a base with no overlay is a
change introducing one, where there is no earlier version to prefer. A maintenance run reads the
checkout, because there it *is* the default branch.

## A change from outside the repository

Before any session starts, a run establishes one fact: whether the head under review lives in this
repository. A branch here is the repository's own work and is reviewed as always. A head in a fork is
**read, and nothing in it is executed** — no scanner, no install, no verification from the overlay.

The reason is what the job holds rather than what the code might do. An installation token and a model
provider's key sit next to the process, and one command over a fork's manifest runs a stranger's build
script under the same user as the process holding both. Handing that command a scrubbed environment
changes nothing: a child can read the parent's original one out of `/proc`, along with the checkout and
whatever else that user can read. The containments that work are a different security context — a
container with no network, a separate user — or not executing, which costs nothing.

What the run does instead, and what it gives up:

| | Own branch | Fork |
| --- | --- | --- |
| reading the diff, files, registries | yes | yes |
| `run_command` in a session | offered | not in the toolkit; asking for it is refused with what to do instead |
| a check that needs a command | runs | recorded as a gap, reason `not-permitted` |
| a fix branch, a prepared patch | as configured | never |
| "how do I fix this?" | the change, verified | a paragraph, and a line saying why there is no patch |

A head the platform will not place is treated as a fork: not asked, an API error, or a fork since
deleted all read the same way. A review that guesses in the permissive direction is one an attacker
arranges by breaking a single call. The posture, its reason and the consequence are in the manifest and
in the report's first lines, so a thin review of a fork is never mistaken for a clean one.

This closes the fork case and not the general one. Malicious code already merged into the default
branch is executed by a maintenance run as the repository's own — the barrier there is the review that
happens before the merge, not the execution mode afterwards.

## Storage

Three kinds of persistence, separate because losing each one means something different.

| What | Where | Written by |
| --- | --- | --- |
| run record: manifest and evidence | `--run-dir`, published as a CI artefact | every run |
| cache of immutable facts | `.agent/cache`, a directory a CI cache can restore and save | only a run on the default branch |
| what a verification command downloads | `.agent/tools`: the crate registry, the module cache, wheels | any run that verifies a fix |
| agent state: which checks keep failing, how long each tracked finding has gone unreported, and what each check examined last time | the git ref named in `agent/config/storage.yaml` | any maintenance run, and only when it changed |

Every one of those paths, `--run-dir` included, is read relative to the repository being worked on when
it is not absolute. One rule, because git has the same one: a relative worktree path is resolved from
the repository, and a second rule based on the caller's working directory means the agent and git
disagree about where a fix is while both of them are right.

Only facts that cannot change are cached — a version's publication date, an artefact digest. Advisory
data and version lists never are, or a weekly run would stop noticing new advisories, which is the
reason the weekly run exists. Failures are not cached either: an unreachable host is not a fact about
a package.

The state is separate from the cache on purpose. A cache may vanish and cost only time; the state
decides whether a person is told something, so it must not depend on a runner keeping a directory.

A session is not trusted to stay in the tree it was given, either. Every change a task is meant to
make goes through the agent's tools, into a worktree the agent made for it — but a backend brings file
tools of its own, and what confines those is the backend's sandbox, which is a setting and can be off.
So the repository's checkout is watched: anything a session changes there is copied into the run
record, the checkout is put back, and the attempt is refused rather than believed. A session that
edited the wrong tree believes it made a change that is not on the branch, and that claim is the one
thing worse than no fix at all. Uncommitted work that was already there when the run started is never
touched.

The tool cache exists because a command is not given a home directory to keep things in. Every command
a task runs gets a built environment: the `PATH` and the toolchain locations the machine has, so a
compiler can be found at all, a home directory of its own that dies with the task, and no credential
of any kind. That last part is why the cache is named separately — a crate registry directory in a
developer's home may hold their publishing token, and a check that verifies a dependency move has no
business reading it.

Exit codes are an interface, because CI acts on them: `0` permitted, `5` blocked, `6` inconclusive,
`64` configuration error, `2` internal failure. A run that could not execute its tasks exits `6` and
never `0` — the absence of a result is not a result.

## Roles and models

**The agent names no model, anywhere.** Not in code, not in the configuration it ships. Which backend
and model answers for each role is stated by the product in its overlay, per kind of run, and so is
what that run may spend:

```yaml
review:                            # a change came up for review; somebody is waiting
  models:
    analyst: cursor/composer-2.5
maintenance:                       # the default branch is being maintained
  models:
    analyst: cursor/composer-2.5   # finds what has gone stale or vulnerable
    fixer: cursor/composer-2.5     # writes the fix and proves it safe
```

Per kind of run rather than once for the file, because "the careful model on a change somebody is
waiting for, the cheap one on the weekly sweep" is a decision products make. The cost is that a role
both kinds use is written twice.

That is not a preference about configuration style. A product outlives any one provider: a
subscription ends, an adapter appears, a project decides its reviews are worth a more expensive
model. With a default inside the agent, each of those would be a fork of the agent's configuration
directory, carried across every release for the sake of one line — and the agent would be deciding
how somebody else's money is spent. The provider is written with the model rather than beside it,
because the provider decides which models exist: `composer-2.5` alone is not an address, and moving a
role to another provider is then one word on one line. Provider credentials come from the environment
and never from the overlay, which is a file in git.

Only the roles a plan reaches are ever created, so a product that switches every role to another
provider never loads the previous adapter at all — the SDK it needed can be uninstalled. A role the
run needs and nobody bound is a startup error naming the role, not a silent substitution.

What a role *requires* is not configurable. An analyst that cannot call the tool registry has nothing
to establish a fact with, so those needs live in `agent/roles.py` next to the code that has them. Each
adapter declares what it implements, and a binding whose backend falls short is a startup error naming
the missing ability. Modifying files is not among those abilities: `edit_file` is a tool like any
other, so a `fixer` needs the registry and nothing more, and the check happens before a maintenance
run spends anything rather than when the first fix is attempted.

What the agent does ship is `agent/config/backends.yaml`: settings per adapter, such as whether the
SDK's own sandbox is used. A product chooses what it pays for; how tightly that runs is not a knob it
may loosen. The manifest records the pair each role was bound to, so "which model judged this" stays
answerable afterwards.

## Budgets

Analysis tasks are independent, so they run concurrently, and each kind of run states what it may
spend in its own block: `review.limits` and `maintenance.limits`, each with a token ceiling for the
whole run, wall clock per task, and how many tasks overlap. Neither inherits from the other, so what
a run may spend is visible where that run is described rather than assembled from two places by the
reader. A maintenance run started by hand gets the same numbers as one that woke on a timer: the work
is identical, and a second set would only be a second thing to keep in step.

How much a maintenance run may leave behind is a different question — how loud the tracker gets, not
what the run costs — so it has its own block inside the same section, `maintenance.queue`.

Every key is required, and a ceiling that is not wanted is written as `null` rather than omitted. A
missing key is a question nobody answered, and treating it as "no limit" would make the most
expensive setting in the file the one nobody ever typed. One number is the agent's and not the
product's: `tool_calls_per_task` in `agent/config/limits.yaml`, which counts a step nobody outside
the agent sees and guards against a session that has stopped making progress.

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
the queue `maintenance.queue` in the overlay allows. Every finding about one package pin or one file is one task,
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

**With `--publish`, a verified branch is pushed and proposed.** The agent asks the platform which of
its own branches already carry an open change request *before* preparing anything: that answer decides
what is worth fixing at all, since a subject already under review would otherwise get a second change
request with the same edit in it. If that answer cannot be read, no branches are prepared — preparing
them blind is how duplicates happen, and a wasted fix session costs less than a reviewer's afternoon.

The change request body is derived from the record: the finding, the remediation, what the fix task
reported in its own words, the surfaces that ran in full, and any command that was already failing on
the base commit. It links the issues it answers with a plain reference and no closing keyword — a merge
would close them on the platform's word, while closing here means the check that owns the finding
looked again and found nothing, which the next maintenance run establishes and records.

Pushing uses the agent's own credential over HTTPS, with the token passed to git through a credential
helper that reads it from the environment rather than from a command line, and with any configured
helper cleared first so a developer's keychain cannot answer instead. Nothing is ever force-pushed: a
branch that will not fast-forward is reported and left as it is. Commits are authored as
`ai-devsecops-agent <ai-devsecops-agent@users.noreply.github.com>` — a `users.noreply` address on purpose,
because platforms match commits to accounts by e-mail and a plausible address is how a machine's commit
ends up in somebody's contribution graph.

An agent's own change request is one it cannot approve: GitHub refuses a review event from a pull
request's author, whoever that author is. That is the correct shape anyway — merge authority stays with
the humans and the required checks, and the agent's part ends at proposing something verified.

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

The same credential is what a run's reads of the platform's API carry, and the reason to set one even
for a run that publishes nothing: anonymous access allows sixty requests an hour, which one ecosystem
of one task can spend. It travels no further than that — attached inside the agent's own GET-only tool,
to that platform's hosts alone, dropped on a redirect that changes host, and never placed in the
environment a command is given, because a command may be running code that arrived in the change under
review. Each run's record says whether the API was read as somebody or anonymously.

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
several repositories share, a GitHub App is the better identity: comments arrive from `<app>[bot]`,
permissions are granted per installation rather than per person, and nobody's departure takes the
reviewer with it. Nothing in the agent changes — the installation token goes in
`AGENT_GITHUB_TOKEN`:

```yaml
- uses: actions/create-github-app-token@v2
  id: agent
  with:
    app-id: ${{ vars.AGENT_APP_ID }}
    private-key: ${{ secrets.AGENT_APP_KEY }}
- run: uv run agent review --change ${{ github.event.pull_request.number }} --base ${{ github.base_ref }} --publish
  env:
    AGENT_GITHUB_TOKEN: ${{ steps.agent.outputs.token }}
```

The App needs *Pull requests: write* to review, *Issues: write* to raise and close findings,
*Contents: write* to push fix branches, and *Metadata: read*, which GitHub requires anyway. Install
it on the repositories it reviews and nothing else — an installation is the boundary.

By hand, outside CI, `scripts/app_token.py` mints the same token from the App's id and private key,
so a live check speaks as the App rather than as its author. Keep the key out of the repository;
`*.pem` is ignored. `scripts/live_maintain_check.py` drives the other half against a real repository —
a scratch branch pushed, proposed, tracked as an issue and then cleaned up — and prints the account
that owned each step. That check is how the label listing's few seconds of lag was measured, which is
why the reconciliation reads the open set before it writes anything.

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

## Tracking findings as issues

A maintenance run has no conversation to write in — there is no diff anyone is reading — so
`maintain --publish` tracks its findings as issues instead. It needs *Issues: write*, and it is the
same reconciliation by finding key that a review's threads get:

| On a rerun | What happens |
| --- | --- |
| the finding is still there, unchanged | nothing at all — no comment, no edit |
| the finding is still there, reworded | its existing issue is brought up to date |
| the finding is gone, its check finished, first time | the issue is left open and the record says why |
| the finding is gone, its check finished, again | the issue is closed, with a comment naming what looked and where |
| the finding is gone but its check did not finish | the issue is left exactly as it was, silently |
| the finding is gone but the check never reached its package | the issue is left open and the record says the sweep came up short |
| the issue has no marker | it belongs to a human, label or not |

Finished means the check reached a complete answer, which `findings` is as much as `clean`: a check
that listed four problems has said what it found, and a fifth is not on the list. Requiring `clean`
froze the tracker instead — while one pin in an ecosystem was outdated, nothing in that ecosystem could
close, fixed or not.

Complete is not the same as exhaustive, which is the row above about the sweep. A check answers with
what it examined, and how much it examined varies: two live runs over one unchanged repository got
through four and then all six of its action pins, both said `findings`, and the reports were the same
shape. So a closure also needs the check to have examined the package in question, read out of the
evidence — a fact cannot be recorded without citing a call that produced it, which makes the examined
set a record of what the run did rather than a claim about it. A run that got through less than the
last one says so in its report, under **Not examined this run**.

Twice, because nobody reopens a closed issue to check it, and a task is asked to be exhaustive rather
than proved to be. The count is per finding, lives in the state ref, and resets the moment the finding
is reported again. Two things close on the first answer: an issue about a *check* that keeps failing,
where a completed run is the thing itself and not an absence, and an issue somebody has just written
on, where a person is reading the reply.

The last two rows are the ones that matter in practice. "Still present" on an issue whose check failed
would be as unfounded as closing it, and a weekly reminder that nothing is known is what teaches a team
to mute the agent. And a label proves nothing about authorship: anyone can apply `agent`, so the marker
is what the reconciliation trusts.

Titles are built from the parts that identify a problem and none that drift — no version, no advisory
identifier — so a saved search keeps matching while the body is updated in place. New issues per run
are limited by the overlay (`maintenance.queue.max_new_issues_per_run`); findings over the limit are
left for the next run and named in the record, never dropped and never merged into somebody else's
issue.

The same run pushes what it verified and proposes it, so a weekly pass needs write access to all three:

```yaml
concurrency:                  # one maintenance run at a time; a skipped week costs less than two
  group: ai-devsecops-agent   # runs fighting over the same branches and issues
  cancel-in-progress: false
permissions:
  contents: write             # push fix branches and the agent's own state ref; never force
  issues: write               # raise, update and close tracked findings
  pull-requests: write        # propose the branches it prepared
steps:
  - uses: actions/checkout@v5
  - run: uv run agent maintain --scheduled --publish
    env:
      AGENT_GITHUB_TOKEN: ${{ steps.agent.outputs.token }}
```

## What a scheduled run holds back

Nobody reads a weekly run. Every restraint below exists because the failure mode of an unattended
agent is not a wrong answer, it is a stream of writing that a team learns to skip.

*Silence when there is nothing.* A scheduled run that finds nothing new, closes nothing and ships
nothing writes nowhere. No "all clear" issue, no weekly comment; the run record already says the run
happened and what it checked.

*A tighter budget.* `maintenance.limits` in the overlay usually gets less time, less parallelism and
a lower token ceiling than `review.limits`. The product states both sets of numbers; a configuration
mistake should not be discovered as an invoice a month later.

*Volume limits.* `maintenance.queue` caps new issues per run and fix branches open for review at
once. What does not fit waits for next week and is named in the record.

*One run at a time*, which is the platform's own concurrency group above rather than a lock the agent
invents. Two runs mutating one default branch fight over the same branches and issues, and a homegrown
lock left behind by a cancelled job is worse than a skipped week.

*One escalation when a check keeps failing.* The exception to the silence rule. A check that fails
twice for the same reason, without completing in between, gets a single issue about the failure itself
— keyed on the check and the reason, updated by later runs, and closed when the check runs to
completion again. This is what keeps a quietly broken source, an expired credential or a registry page
that changed shape, from reading as a clean repository for months.

That last one is why the agent keeps a memory: whether a failure is a repeat is the one thing a run
cannot work out for itself. It lives in `refs/agent/state` as one small JSON document — not a branch,
so it is in nobody's history — chained forward so no push is ever forced, and written only when it has
something new to say. A run whose memory cannot be stored says so in its warnings and carries on: the
cost is that next week's repeat may read as a first failure, which is the safe direction.

## Being woken by a comment

Somebody replies in one of the agent's threads, or on one of its issues, and a run follows:

```bash
uv run agent review   --change 12 --base main --wake-comment 2103847 --actor <login> --publish
uv run agent maintain --wake-issue 7 --wake-comment 2103999 --actor <login> --publish
```

Where the comment was left decides the playbook, and that is known from the event before any model
runs: a reply in a review thread is the review's business, a comment on an issue is maintenance. What
the comment *says* decides only the course, and that is the one place a model reads free text.

The classifier assigns one of five intents — `unlock`, `fix`, `question`, `recheck`, `unrelated` —
and a table in code turns that into a course. The separation is the safety property: the worst a
misread comment can do is answer instead of act, or re-establish a fact nobody asked about.

| Read as | What the run does |
| --- | --- |
| `question` | replies in the conversation and changes nothing |
| `fix`, in a review thread | prepares the change and offers it there. Nothing is committed or pushed |
| `fix`, on an issue | replies in prose: there is no diff to hang an offer on |
| `unlock`, `recheck` | runs the check that owns that one finding, then reports as a maintenance run does |
| `unrelated` | nothing at all, and writes nowhere: a machine that answers "thanks!" is a machine people mute |
| anything, but unsure | replies. An unsure `unlock` is permission nobody gave |

A recheck is narrow on purpose: only the capability the finding belongs to, in its own ecosystem. A
person who writes on one issue is asking about one thing, and a weekly sweep in reply would make
approving something the most expensive thing a person can do. On an issue the run also posts what came
of it — every sentence of that status is a recorded fact, written by the agent rather than a model —
because a run that closed the issue silently reads exactly like being ignored.

Four things end the run before the first model call, each recorded as `declined` with its reason:

| Refused when | Why it is not a judgement call |
| --- | --- |
| the actor is a bot, or the account the agent publishes as | answering its own comment is a loop, and each turn of it costs a model |
| the account has no write access, or the platform will not say | a comment is a way to spend somebody's budget and, for an unlock, to grant permission. The permissive mistake cannot be taken back |
| the issue or thread carries no marker | a remark the agent never made is not one it can answer for, and there is no finding key in it |
| the comment's author is not the actor the event named | something is passing one person's authority with another person's words, and the words are what would act |

An answer is text and nothing else. No thread is resolved by it, no issue is closed by it, and the
answering session gets no worktree, so it can read the repository and cannot change it. Everything
factual in the published comment — the run identifier, the finding key, the marker that makes later
runs recognise it as the agent's own — comes from the agent; the session supplies only the explanation.

"How do I fix this?" is the one case where a comment produces a change, and it is still only a comment.
The edit is made by a fixing session in a throwaway copy of the repository at the head of the change,
where the overlay's verification runs over it exactly as it would for a fix branch. What gets published
is the session's paragraph about why the edit looks like this, then the diff **git** reports — never the
session's account of it — then the label that verification earned:

| Shape of the edit | How it arrives |
| --- | --- |
| replaces exactly the lines the remark hangs on | a `suggestion` block, applied with one click |
| touches anything else, or more than one file | a diff to read and apply by hand |
| longer than a comment can carry | the files it touches, named; half a diff looks like a whole one |
| the session arrived at no change | its explanation of why, and no block at all |

Nothing is committed, nothing is pushed, and the copy is discarded either way: the branch under review
moves only if a person moves it. A patch that could not be shown safe is still offered — the question
was how to fix it — with the label saying so, because whoever clicks "commit suggestion" is trusting
that label. Preparing one needs `fixer` bound in the overlay's `review.models`; a product that binds
none has decided its reviews explain rather than propose, and those questions are answered in prose.

This is also why the caution about publishing under a human account matters: a workflow can filter
`[bot]` authors, but it cannot tell a machine account from a colleague. Publish as a bot, and pass
`--actor` so the agent can compare accounts.

## What waits for a person

A major move breaks callers by definition, so the library says one ships only after somebody approves
it. That used to be a sentence a session was asked to honour. It is arithmetic now, in two halves that
fail in opposite directions:

| Where the hold comes from | What it covers |
| --- | --- |
| the agent, from the versions | a semantic-version major, measured from the current pin to the `target` the finding states |
| the check that found it, by saying so | the majors no comparison can see: a `@v5` to `@v7` action pin, an image tag, a raised toolchain floor |

A held finding is reported and never changed — no branch, no change request, this run or any later
one — and its issue says what it is waiting for and what a comment there will cause. The declared half
can only add a hold, never remove one, so a session cannot argue its way past the policy; the measured
half means forgetting to declare one cannot switch the policy off by omission. A security remediation
is never held by the arithmetic: the library allows one to carry a major move because waiting is the
greater risk, and a hold the agent invented would park an advisory fix behind a question nobody asked.

The approval is a comment on that issue, from an account with write access, read by the same
classifier as every other comment. What the run does with it is deterministic:

1. the grant is written into the issue body as a stamp — who, which comment, what day;
2. the check that owns the finding runs, narrowed to it;
3. if it is still there, the fix is prepared, verified against the product's own commands, pushed and
   proposed, with the change request naming the issue;
4. the person is told all of that on the issue they wrote on.

The stamp is why the question is asked exactly once. It lives in the body rather than in the agent's
own memory because that is where the person granted it: "who approved this" is a question for whoever
reads the issue, not for whoever has shell access. It also outlives a failure — a fix that will not
verify is retried under the same approval next week — because approval is for the move, not for one
attempt at it, and asking again every week is how people learn to approve without reading.

A run that cannot read the issues cannot see the approvals, and then nothing that waits will ship: the
finding is reported for one more week and the run says so in its warnings.

## Status

Stage 7 is complete: a comment from a colleague wakes the agent, is read for what it asks for, and is
answered in prose, turned into the change it asks how to make, or read as the approval that releases a
major move the agent was holding back.

Stage 6 before it: a review run publishes its decision on GitHub and reconciles its threads by finding
key; a maintenance run tracks its findings as issues by the same key, pushes what it verified and
proposes it as a change request; a scheduled run holds itself back as described above, remembers which
checks keep failing, and refuses to be woken by its own comment. Under that sit the decision path, the
tool registry, the Cursor SDK adapter, role bindings, concurrency and budgets; every analyst capability
the library defines can run, and a run can be narrowed with `--only` (for example
`deps-vuln@python-uv`). What is still ahead is the eval harness — the way to choose a model per role on
evidence rather than taste. `backend: fake` remains available for CI that must exercise the pipeline
without a model.

The predecessor [`ai-devsecops-cursor`](https://github.com/amsokol/ai-devsecops-cursor) remains
frozen at its final tag for products that have not migrated.
