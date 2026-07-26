# Design of the AI DevSecOps agent

Status: draft. This document describes the agent (`ai-devsecops-agent`) — the executor that uses the
knowledge library [`ai-devsecops-skills-knowledge`](https://github.com/amsokol/ai-devsecops-skills-knowledge)
and a product overlay.

This design document is English, as are the code, configuration, schemas and README.

Terms and the exchange contract are defined in the library's `CONTRACT.md`; this document describes
how the agent executes them.

## 1. Purpose and boundaries

The agent is the execution layer. It contains no judgement: what counts as a problem and what to do
about it is described in prose in the library. The agent owns what must be exact and reproducible:

- turn a trigger into tasks and run them in parallel;
- give those tasks hands — deterministic tools;
- collect results, reduce them to a verdict, and perform actions on the hosting platform;
- stay within budgets and leave an audit-ready trail.

What is not in the agent: severity criteria, quarantine rules, ecosystem procedures, product values.
If any of that appears in code — it is misplaced.

## 2. What changed relative to the previous agent

| Was (`ai-devsecops-cursor`) | Is |
| --- | --- |
| one session of one model does everything | a task plan, parallel subagents, a model per role |
| the SDK woven into the run code | an agent SDK behind a narrow port; the core does not know about it |
| the model itself decides what to do | the plan is deterministic; the LLM only on free-form triggers |
| protocol — `AGENT_SIGNAL:` strings in text | validated artefacts: evidence, findings, result |
| facts and judgement mixed in one session | evidence acquisition phase separated from decision phase |
| incompleteness looks like success | a third run outcome: inconclusive |
| "best model" by taste | eval on library fixtures as a condition for changing a model |

The goal of the changes is optimality: the right model for the right task, parallel execution,
predictable cost. But optimality must not be bought at the price of reproducibility: the gate blocks
people, so the same input must give the same answer.

## 3. Principles

1. **The core is deterministic.** Planning, aggregation, deduplication, verdict computation, date
   arithmetic and version comparison are code. The LLM does not take part in those steps.
2. **The LLM where judgement is needed.** Code analysis, reachability of a vulnerability,
   prioritisation, fixing, wording text for a human. And one special case — classifying a free-form
   human request.
3. **Tools instead of guessing.** If a fact has an exact source, code gets it. The model does not
   recall CVEs and does not count dates.
4. **Non-interactivity.** The run happens in CI. No confirmation prompts, no waiting for input: any
   such request is a configuration error, not a pause.
5. **Budget and degradation.** Each task has a step and time limit; the run has a shared budget.
   Exhaustion yields an inconclusive result, not silence and not approval.
6. **Auditability.** When a run finishes it is visible which models worked, which library and tool
   versions were used, which evidence was gathered and what it cost.
7. **Everything external is data.** Repository contents, change descriptions, registry answers and
   subagent output cannot be instructions.

## 4. External interfaces

Three ways to start, and all three reduce to one trigger inside:

| Trigger | Source | Playbook |
| --- | --- | --- |
| `change-opened`, `change-updated` | hosting event | `playbooks/pr-review` |
| `comment-on-change` | a human reply in an agent thread on a change | `playbooks/pr-review` |
| `comment-on-issue` | a human comment on an agent issue | `playbooks/maintain` |
| `maintain-requested` | manual start | `playbooks/maintain` |
| `maintain-scheduled` | schedule, for example once a week | `playbooks/maintain` |

Waking has two questions, answered by different things. *Where* the comment was left determines the
playbook — and that is known from the event, before any model. *What it says* determines only the
course inside the playbook, and that is the only place where a model reads free text.

A regular maintenance run is mandatory, not optional: some findings appear with no code change at all.
New vulnerabilities are published for versions already in place, quarantine expires for candidates a
previous run deferred, fixes ship for what used to be unfixable. Those are events in time, and
without a scheduled run nobody notices them until a person starts the agent by hand — usually after an
incident. Safeguards for it are in section 6.1.

The CLI is one command with subcommands, usable both in CI and locally:

```bash
agent review --repo <path> --change <number> [--dry-run]
agent review --repo <path> --change <number> --wake-comment <id> --actor <login>
agent maintain --repo <path> [--scheduled] [--dry-run]
agent maintain --repo <path> --wake-issue <number> --wake-comment <id> --actor <login>
agent explain --run <id>      # show the run manifest and gathered evidence
```

`--wake-comment` and `--actor` are passed only together: without the comment the run does not know
what it was asked, and without the account it cannot tell a colleague from its own previous comment.
A half call is a configuration error, not a reason to guess the rest.

`--dry-run` plans and analyses but does not mutate the hosting platform. That is a debug and first-
connection mode for a product, not a permanent state.

## 5. Architecture

```text
trigger
  │
  ├─ intent (LLM, only for comment-on-*) ──────────────┐
  │                                                      │
  ▼                                                      ▼
planner (code) ──► task graph ──► subagent runner (backend port)
  ▲                                   │        │
  │                                   │        └─► tools (MCP, deterministic)
  │                                   ▼                    │
library slice ◄── library (digest)  evidence store ◄───────┘
  ▲                                   │
overlay                               ▼
                                 aggregator (code)
                                      │
                                      ├─► verdict (block table)
                                      ├─► reporter (texts, LLM optional)
                                      └─► actions (SCM, idempotent)
                                              │
                                              ▼
                                     run manifest + exit code
```

### 5.1 Planner

The plan is built by code from three inputs: the trigger, the list of changed files, the ecosystems
enabled in the overlay. For each playbook the scenario configuration describes which tasks exist and
under which condition they turn on; a condition is a set of file markers and/or a requirement for an
enabled ecosystem. There is no model call here.

The reason is exactly that: for a "change opened" event the task set is a quantity computable from
the diff. Spending a model call on it adds latency, cost and, above all, non-determinism to a
blocking check. A flickering gate loses trust after the first "I reran it and it passed".

### 5.2 Intent classifier

The only place where the input is truly free-form: a person wrote a comment. A cheap fast model works
here, and its job is narrow — assign the message to one of the known intents and return a structure,
not a reasoning. This session has no tools: the answer must depend on the two texts passed in and on
nothing else, otherwise the session will sooner or later start classifying by what it found in the
repository.

| Intent | Course | Meaning |
| --- | --- | --- |
| `unlock` | `recheck` | the person allowed what the agent held: re-check this finding and act |
| `recheck` | `recheck` | facts changed, look again |
| `fix` | `patch` | "how do I fix this" — make the edit and offer it in the conversation |
| `question` | `answer` | explain, changing nothing |
| `unrelated` | `ignore` | do nothing and write nowhere |

Course `patch` degenerates to `answer` where there is nothing to offer: an issue comment has no diff
to hang a suggestion on, and a product that did not bind a `fixer` role for its reviews decided that
its agent explains rather than proposes.

The classifier does not decide what to do next: the "intent → course" mapping is a table in code.
Therefore the cost of a misread comment is bounded: a spent session, an answer instead of an action,
or a recheck that was not asked for. A permission the person never gave never comes out of this. On
low confidence the course is `answer`: an uncertain `unlock` is a permission that was not given, and
an uncertain `recheck` is budget spent on a guess.

A recheck narrows to one check — the capability and ecosystem the finding from the marker belongs to.
A person who wrote on one issue is asking about one thing; a week-long sweep in reply would make
approval the most expensive action available to a person and drown the answer in unrelated findings.

Before the first spend the run refuses in four cases, each recorded as `declined` with a reason: the
author is a bot or the account the agent itself publishes as; the account has no write right or the
platform refused to confirm it; the issue or thread has no marker, so the conversation is not the
agent's; the comment author does not match the actor from the event. The first case is loop
protection; the second is protection against anyone who can write comments handing out budget and
permissions.

A reply to a person is text only: the thread is not resolved, the issue is not closed, the answering
session gets no worktree, so it cannot change files. Everything factual in the published comment —
run id, finding key, marker — is written by the agent; the session supplies only the explanation. On
an issue, besides the reply, a status of what happened is published: every phrase of it is assembled
from recorded facts, because a silently closed issue reads exactly as "I was ignored".

Course `patch` is the only one where a comment produces a change, and that change also stays a
comment. The edit is made by a `fixer` session in a throwaway copy of the repository at the head of
the change, and the overlay's verification runs over that copy — the same as for a fix branch. What
is published is the session's paragraph on why the edit is as it is, then the diff read **from git**,
not the session's account of it, then the label verification earned: a `suggestion` block if the edit
replaces exactly the lines the remark hangs on, otherwise a diff for reading. Nothing is committed or
pushed; the copy is discarded in every outcome: the branch under review moves only by a person's hand.
An unverified patch is still offered — they asked about a fix — but labelled as such, because the
person clicking "commit suggestion" is trusting that label.

### 5.3 Subagents and roles

A task is executed by a subagent in one of the library roles: `analyst` (read only) or `fixer`
(edits in an isolated worktree). Each role is bound in configuration to a backend and a model.

| Role | Kind of work | Model requirements |
| --- | --- | --- |
| `intent` | classify short text | fast, cheap, structured output |
| `analyst` | judgement over prepared context | strong on code, long context, tool use |
| `fixer` | edits, iterate until verification is green | strong on code, reliable tool use |
| `writer` | word text for a human | medium; enabled optionally |

Model requirements in this table are a guide for choosing a pair, not a checkable condition: "strong
on code" cannot be verified at startup. What is checked is something else — that the backend can do
what the role's code cannot work without: a tool registry. File editing is not in that list —
`edit_file` is a tool like the others, and a backend that has a registry has it too. A role nobody
bound to a backend is also a startup error if the plan requires it: a silently skipped task is worse
than a loud refusal.

The `writer` role is separate on purpose: aggregation and the verdict are computed in code, but a
remark is read by a person, and the quality of wording has a price. That is the only thing a model is
brought in for after the decision phase, and it cannot change the verdict or the set of findings.

Finding texts are still worded by `analyst` inside its task: a separate role would cost a second
model call every run and add a place where the text drifts from the set of findings. `writer` turns
on where there is no finding at all — in a reply to a person who asked something in a thread or an
issue. That is the only prose published in the agent's name with no finding behind it, and therefore
everything around it is tight: the reply changes nothing, no thread is resolved by it, length is
limited in code.

The boundary between `writer` and `fixer` is not who writes code, but what remains after the session.
`fixer` leaves a tree over which the product's verification can run, so its result gets a label and
applies in one click; `writer` leaves text. Hence the split: "how do I fix this" goes to `fixer`,
even though the answer is published as a comment — a patch quoted from prose is unchecked by anyone,
and that is exactly what people will apply. The paragraph "why the edit is as it is" is written by the
same `fixer` in the result field: the one who made the edit explains it from the same context, and
there is nowhere for the words to diverge from the diff.

**Prepared context beats agent wandering.** For review the diff, affected files, scanner output and
dependency metadata are gathered deterministically in advance. The subagent receives a ready slice and
in the typical case does not walk the repository with tools: that is one or two calls that cache,
parallelise and have predictable cost. Tool exploration turns on as a departure when the model
explicitly asks for more context.

### 5.4 Backend port

The agent is designed for several agent SDKs, and they live behind a port. The port is narrow and
describes only what a run needs: start a session with a given model, system text and tool set; receive
an event stream; receive completion; receive usage accounting. Everything else stays inside the
adapter.

The first adapter implemented is the **Cursor SDK**; a second is added later and must not require
core changes. A port with one adapter is justified not "for the future": it has two consumers from
day one, because tests and the eval harness work through a fake backend that returns pre-recorded
events. Without the port, the planner, aggregation and verdict could be checked only with a live
model — slowly, expensively and non-deterministically.

Consequences that must be in the implementation:

- **A model is named as a "backend + model" pair.** The set of available models is determined by the
  backend, not by an abstract provider list. "The agent is not tied to Cursor" means Cursor is the
  first among equal backends, not the only path.
- **Backend capabilities differ**, and the differences are declared explicitly: tool use, file edit,
  structured output, token accounting detail. Role–backend compatibility is checked at startup; an
  incompatible configuration is a launch error, not oddness mid-review. A capability is declared only
  where it is known: a boolean invented for a table looks exactly like a verified one.
- **Role requirements live in code, not in configuration.** What a role needs follows from what its
  code does: an analyst without a tool registry has nothing to ground a finding on, and a `fixer`
  without file-edit rights fixes nothing. That is not a settings knob. The knob is the "role →
  backend and model" mapping, and it lives in the product overlay. The check runs between the
  requirement from code and the adapter's declared capability.
- **There is no model name anywhere in the agent** — neither in code nor in the files it ships. A
  product outlives any provider: a subscription ends, a new adapter appears, an important project
  decides its reviews are worth an expensive model. A default inside the agent would turn every such
  case into a fork of the configuration directory carried across every release, and the agent into
  the one deciding how somebody else's money is spent. Budgets follow: a token ceiling is part of the
  model decision. Provider keys come from the environment and never from the overlay, because the
  overlay lives in git.
- **Permissions are set explicitly** and forbid any interactive prompts.
- **Budgets and timeouts are attached by the port**, not by SDK facilities: backends account
  differently, and relying on their internal limits is unsafe.
- **The result is an artefact file**, not a final message. Parsing reply text is fragile, and that is
  exactly where the previous agent broke.

### 5.5 Tools

Tools are implemented once in the agent's code and offered to subagents as an MCP server. That way
they are reused by any backend, and the port does not become an abstraction over someone else's
abstractions.

| Group | Tools |
| --- | --- |
| repository | `read_file`, `list_files`, `search_text`, `read_change` |
| commands | `run_command` (allowlist, no shell, timeout, scratch directory) |
| network | `http_get` (host allowlist), `scm_read` |
| derived facts | `compare_versions`, `date_math`, `evidence_cache` |
| mutations (`fixer`) | `edit_file`, `git_ops`, `scm_write` |

`read_change` answers "what the change did to this file" — added and deleted lines with numbers,
straight from git. It exists only where there is a change, and it exists because the review scope
must not be a matter of opinion: a model that read a whole manifest also sees pins nobody touched and
reports them. That makes an author wait because of a line they did not write — and such a gate gets
switched off.

Separate from the network limit is a limit on **how much a tool returns to the model**. Downloading a
large document is cheap; putting it in context is not: an aggregate version index for a popular package
costs more than the whole review that asked for it. A document over the limit is not returned, the call
is marked unsuccessful, and a fact cannot cite it.

So refusal does not become a dead end, `fetch` can return part of a document: `select` names a path
inside JSON, `keys_only` that only names are needed. A package's version list is the names in a
releases object; the megabytes under them describe files the version question does not care about.
Knowledge of which key to pick stays a recipe in the library; registry specifics do not move into
agent code. The manifest records what was read, not what was requested: `<url>#<select>`, otherwise
the run cannot be reproduced.

Rights to utilities and hosts are granted by a "request and ceiling" mechanism: the ecosystem document
declares in `Requirements` what it needs, and the agent satisfies the request within its own ceiling.
A line in a knowledge document cannot widen the agent's rights; something requested outside the
ceiling is rejected at startup with a clear message.

The request is read from prose — every name in backticks under the `Binaries:` and `Hosts:` bullets.
That kind of parsing was rejected for the overlay, and the difference is in the failure mode. A
rephrased overlay sentence silently changes the verdict; a rephrased `Requirements` line grants
nothing, the fact becomes `not-permitted`, the run reports it and does not allow the merge. Hedges
like "`skopeo`, when image metadata is needed" are not distinguished by the parser and yield a
superset — the real boundary remains the ceiling, not the wording. Rights are granted only to
requests of enabled ecosystems and before the first task starts: a refusal found mid-review would
look like a broken tool and would cost budget.

`run_command` works without a shell: no pipes, redirections or chains. That shrinks the attack surface
and makes calls reproducible. If a procedure needs a pipeline, the right answer is a dedicated tool
for that need, not allowing a shell.

### 5.6 Evidence store

Evidence is written to a single run store: question, subject, value, origin, source, time,
reliability, status. Reliability is derived from origin by code, not chosen by the model, because
finding rights depend on it.

Three different things have to be kept across runs, and they cannot share one place: they survive loss
differently. Lose the run record — the audit disappears, but work does not change. Lose the cache —
the run is slower. Lose state — the agent **changes behaviour**, because it forgets that a source has
been broken for a third week.

| What | Where | Who writes |
| --- | --- | --- |
| run record: evidence and manifest | run directory, uploaded as a CI artefact | any run |
| cache of immutable facts | directory that the CI platform cache restores and saves | only a run on the default branch |
| agent state | git-ref `refs/agent/state` in the product repository | only a run on the default branch |

**The run record** is immutable and lives as long as the platform keeps artefacts. Key rule: a future
run never takes facts from a past record — only `agent explain` reads it. Otherwise the audit log
quietly becomes the source of truth and one run's error lives forever.

**The cache** holds only facts immutable by nature: a concrete version's publish date, an artefact
digest. Available version lists and vulnerability data are never cached — otherwise a weekly run would
stop noticing new advisories, which is exactly why it exists. Failures are not cached either: an
unreachable host is not a fact about a package. A cache miss is a normal event, not a failure;
platform entries are evicted, and the run must calmly obtain the fact again.

The cache is a directory, and no separate platform backend is needed: GitHub Actions cache and its
analogues restore and save a directory. One implementation works on a laptop and in a pipeline, and
"where is the cache" becomes a configuration string, not a branch in code.

Every entry carries origin and the recipe version that produced it, so a fixed recipe invalidates the
old. Reliability does not grow by caching: a heuristic date stays heuristic forever. The `--no-cache`
flag lets you prove the verdict reproduces without the cache.

Only runs on the default branch may write the cache; runs on changes read. That closes cache poisoning
by anyone who can open a change, and matches how the platform cache is already scoped by branch. A
useful consequence: the weekly maintenance run acts as a warmer, and the blocking gate gets ready
dates. The gain is largest where acquisition is expensive and fragile — ecosystems with origin `web`;
neighbouring runs also stop disagreeing on a date because of a differently rendered page.

**State** is kept deliberately minimal — only what cannot be derived from the hosting platform:
counters of consecutive failures by reason and surface, for the escalation rule in section 6.1. The
number of open agent changes and issues is not stored; it is asked of the hosting platform, otherwise
state starts drifting from reality. The ban on two concurrent runs is not state either — it is a CI
concurrency group.

A separate ref was chosen over a file on the default branch and over the platform cache for three
reasons: it is durable and independent of eviction; it creates no noise on the branch or in changes;
it has history that shows when and why a counter grew. Inside — one `state.json` file, replaced
whole. The ref is read with the credential already available to checkout: anyone who can clone can
get the ref. It is written with the same credential as fix branches, because leaving a trail is not
the same as reading. The commit is assembled with plumbing and does not touch the working tree: a fix
task may be working in it at that moment.

Reading never fails. A missing ref, an unreachable remote and a document hand-edited into invalid
JSON mean the same here: this run has nothing to lean on. Otherwise a helper would become a
dependency, and a broken document would refuse work to every scheduled run.

Publish dates do not depend on the product, so a shared organisation-wide fact store would warm a new
product from day one. The `evidence_cache` interface allows that, but the first version does not turn
it on: shared mutable state between products means poisoning one poisons all.

### 5.7 Aggregation and verdict

Aggregation is code:

1. Collect task results; a missing or invalid result is not "clean" — it is a failure.
2. Deduplicate findings by a stable key; one problem found by two tasks is one finding.
3. On disagreement take the stricter assessment: class `security` outweighs `routine`, higher severity
   outweighs lower. Both original judgements stay in the manifest.
4. Cap each finding's action by the reliability of its evidence.
5. Reconcile with already open threads and issues; closing is allowed only for what whose task finished
   with result `clean`.
6. Compute the verdict from the "class × severity" table and determine the run outcome.

Disagreement is resolved by a table, not by calling a stronger model again. Escalation would put
latency, cost and, above all, non-determinism into a blocking check: two runs on one input would give
different answers. The risk of a model overstating severity is closed another way — blocking is allowed
only on reproducible evidence, so heuristic overstatement yields a remark, not a refusal.

There are three outcomes: allowed, blocked, inconclusive. Inconclusive arises when a required task
failed — the scanner did not start, the host did not answer, the source changed shape, rights were not
granted, the budget ran out. That outcome does not allow the merge and asserts nothing about the code.
A documented limit (`none` in the ecosystem profile) does not count as a failure.

Exit codes:

| Outcome | Code |
| --- | --- |
| allowed | 0 |
| non-blocking findings remain | 0 |
| blocked | 5 |
| inconclusive | 6 |
| configuration error | 64 |
| internal failure | 2 |

### 5.8 Actions

Actions on the hosting platform are performed only by the agent, after aggregation, and exactly once
per run. A subagent does not write comments — that is what makes repeated runs idempotent.
Reconciliation is by the finding's stable key: an existing thread is updated, not duplicated; a
confirmedly gone finding is closed; comment placement is taken from the mutable `location` field,
because the key deliberately contains no line numbers.

The agent never merges.

**The port is narrow, the adapter is one.** The port can do exactly what review and maintenance need:
read a change, find its own threads, publish one review with new threads, edit a thread, resolve a
thread. An interface that mentioned merge or branch rules would eventually be used. The GitHub adapter
speaks through `gh api`: the client already reads the token from the environment, retries on rate
limit and speaks GraphQL, and thread resolution has no REST endpoint at all. The token never enters
arguments — only the environment that `gh` inherits.

**The agent's identity is set explicitly, not picked up by the client.** The token is read from
environment variables in order `AGENT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`; a machine-saved login
is never used, and without a token nothing is published. The adapter's first live check published five
machine reviews under a human's name, and the name is the lesser half of the trouble: a decision signed
by a colleague cannot be told from a colleague's opinion, and a workflow that wakes the agent on human
comments and filters bots cannot filter a human account — its own comment wakes the agent again. The
account used for publishing is written into the manifest, and if the platform shows it as a human, the
run warns and names the needed filter. That is a warning, not a refusal: a separate machine account is
a lawful way to work, and the platform shows it as an ordinary user.

**Own comments are recognised by a marker.** An HTML comment with the finding key is placed in the
body: `<!-- agent:key=... -->`. Comment identifiers change when a thread is recreated, line numbers
when a file is edited above, and the key does not change. A thread without a marker does not belong to
the agent: it never edits or resolves a human comment, and that is guaranteed by absence of a match,
not by prompt politeness.

**Resolving a thread requires a clean run by the owner.** A thread closes only if the finding is gone
and its capability's task finished `clean` on this head. Absence in a run that did not look is not a
fix; closing on that basis quietly removes a real problem.

**Review stance is derived from the result.** `pass` — approve, `blocked` — request changes,
`inconclusive` — an ordinary comment: the check still forbids the merge, and "changes requested" is a
claim about the code that a run without a result has not earned. GitHub will not let you review your
own change at all — neither approve nor request changes — and then the same text is published as a
comment. The manifest records what the platform wrote, not what was requested: a run that claimed
"requested changes" on a pull request with no request for changes devalues both its record and the
page itself.

**Nothing is published for someone else's commit.** If the change moved on while the run worked,
comments would point at lines nobody proposed: the run says so and does not write. Drafts are not
commented. A platform failure is a warning, not loss of the verdict: analysis is already paid for, and
the exit code carries the decision without a comment.

Publishing is off by default and enabled by `--publish`: a laptop run reports to whoever started it,
and a CI run explicitly says it is about to write into someone else's pull request.

**A maintenance run has nowhere to write, so findings become issues.** It has no diff someone is
reading, and a finding not written to the tracker is a finding nobody will see. Reconciliation is the
same as for threads, by the same key: one issue per finding, update instead of a second issue, close
only when the capability owner finished `clean` and the finding is gone — with a comment naming what
looked and on which head. If the check did not finish, the issue stays exactly as it was, and the
agent is silent: "still there" is as unfounded a claim as closing, and a weekly reminder that nothing
is known teaches the team to switch the agent off. The `agent` label is not proof of authorship —
anyone can add it — so reconciliation trusts only the marker. The title is built from the parts that
identify the problem and does not contain what drifts (versions, advisory identifiers): otherwise a
saved search stops matching. The number of new issues per run is limited by the overlay; the rest are
deferred to the next run and named in the record, but not dropped and not merged into one issue.

**A verified branch goes to the platform and is proposed as a change.** The order is the reverse of
the expected one: first the agent asks the platform which of its own branches already carry open
changes, and only then decides what is worth fixing at all — a subject already under review would
otherwise get a second change with the same edit. If that answer is unavailable, no branches are
prepared at all: blind preparation is exactly the mechanism that produces duplicates, and a lost fix
session costs less than a reviewer's evening.

The change body is assembled from the record: the finding, the means, the fixer task's words as a
quote, the surfaces that passed whole, and the commands that were already failing on the base commit.
Issues are linked with an ordinary link without a closing keyword: a merge would close them by the
platform's word, whereas closing here means the capability owner looked again and did not find — that
is established and recorded by the next maintenance run.

Push goes over HTTPS with the agent's credential; the token enters git through a credential helper
that reads an environment variable, not through a command argument: an argument is visible to every
process on the machine. Configured helpers are cleared first, otherwise the developer's keychain
answers instead of the agent — the very substitution this path was built against. There is no
force-push: a branch that will not fast-forward is reported and left as is. The commit author is
`ai-devsecops-agent` with a `users.noreply` address: platforms associate commits with accounts by
email, and a plausible address is a way into someone else's contribution graph without their
knowledge.

The agent cannot approve its own change: GitHub forbids review by the pull request author, whoever
that author is. That is the right shape — merge authority stays with people and required checks, and
the agent's role ends at proposing a verified change.

### 5.9 Fix phase

After the verdict a maintenance run tries to fix what can be acted on: a finding with reproducible
evidence and a named means, in a fixed order (`security` before `routine`), within the overlay's
ceiling on open changes. Selection and order are computed by code: model choice would mean the same
repository yields a different set of branches every week.

**One task per subject, not per finding.** Findings of one class about one pin or one file are merged
into one edit. The first live run of this phase opened three branches with the same edit and paid for
three sessions — that is how a weekly run turns into noise people stop reading. The branch is named by
class and subject, not by advisory: the subject's strictest finding changes as advisories appear and
close, and a branch that moved while the repository did not would open a second pull request.

**The subagent edits files; everything else is done by the agent.** Staging, the commit message, the
branch and later the pull request are the agent's: that way the message is derived from the finding,
not from how the model read the instruction. The `fixer` role has no git or hosting tools.

**Verification is read from the call ledger, not from the model's answer.** The overlay groups
commands into surfaces, the library decides which surfaces are touched (including couplings invisible
from paths), and the agent reconciles the commands that actually ran: to ship a branch, one surface
must pass whole and no executed command may have failed. The rule "at least one command" did not
survive the first live run: two sessions ran the cheapest of five commands and shipped a branch; a
third ran all five, found a failure and refused — for the same edit.

**Pre-existing failure is separated from caused failure.** A failed command is re-run by the agent in
the same worktree reset to the original state. If it fails there too, the branch is still not shipped
— a red branch is not trusted regardless of cause — but the run reports that the product's checks were
already red before the edits, and names them. Otherwise a week of refusals reads as "the agent cannot
raise dependencies".

### 5.10 Hold and unlock

A major update breaks calling code by definition, and the library requires that it ship only after a
person's permission. While that was a sentence addressed to the model, the guarantee sounded like
"the session read the document and agreed" — the same class of promise as "never force-push" if a
push tool existed. Now a hold is a property of a finding, and it is made of two halves that err in
opposite directions.

*Proven.* The finding names the version the remediation means leads to; the agent itself compares it
with the current one and holds a semver major whether or not the session declared it. Forgetfulness
would otherwise silently switch the policy off, and the failure would arrive as an open pull request
nobody was asked about.

*Declared.* Majors that string comparison cannot see are declared by the session: a floating action
pin from `@v5` to `@v7`, an image tag raised half a toolchain. That is judgement, and it stays with
the model. A declaration can only add a hold and never remove one: a session cannot talk the policy
out of it.

A held finding does not enter the fix queue — it is reported and not changed, neither in this run nor
in later ones — and its issue says what it is waiting for and what will happen after a comment.
Remediating a vulnerability is not held by arithmetic: the library explicitly allows a major as part
of a security fix, because waiting there is the greater risk, and a hold invented by the agent would
put an advisory fix behind a question nobody asked.

Unlock is a comment on that issue from an account with write rights, read by the same classifier as
any other. After that everything is deterministic: the agent stamps the permission into the issue body
(who, which comment, when), re-checks the finding, prepares and verifies the edit, pushes a branch and
opens a pull request linking the issue, and writes on the same issue how it ended.

The stamp lives in the issue body, not in the agent's own memory, because that is where the person
gave permission: "who approved this" should be a question for a reader of the issue, not for someone
with console access. It also survives failure: an edit that failed verification is retried next week
under the same permission. Permission was given for the move, not for one attempt; asking again every
week teaches people to approve without reading. A run that could not read the issue ships nothing
held and says so in warnings — the safe side of the error.

## 6. Parallelism

Analysis tasks are independent and start concurrently with a limit on the number of simultaneous
sessions. The limit is not an optimisation but a necessity: providers rate-limit, and without a
limiter fan-out becomes a series of refusals.

Writing tasks are isolated: each gets its own git worktree, so parallel edits do not interfere and a
failed attempt does not leave a dirty tree. Order inside a run is fixed — class `security` first, then
`routine`, and classes are never mixed in one change.

Default granularity is one task per capability for the whole change. Splitting turns on only when the
prepared context exceeds a configuration threshold, and remains a function of the diff: grouping by
directory, fixed order, the same split on the same input. Otherwise parallelism becomes a source of
flickering results.

Splitting has a cost that must be paid knowingly: a defect spread across two files disappears if
shards cannot see each other. Therefore each shard receives a full summary of the change and only its
own files for deep reading, and groups try to match modules rather than cut across them.

### 6.1 Regular maintenance run

A scheduled run differs from a manual one in one property: nobody is waiting for it at that moment.
Therefore it has the same tasks but different limits — otherwise it becomes a noise generator, and the
first thing a team does is switch the schedule off.

**Volume limit.** A run has a ceiling on the number of open changes from the agent and on the number
of new issues in one start. Concrete values are product-specific and live in the overlay. On hitting
the ceiling the agent does not drop findings — it defers them: no issue is created, the finding stays
in the run report, the next start takes it when the queue frees. Priority is fixed — class `security`
first, then `routine`, and within a class by severity.

**No duplicates.** The already described idempotency by stable key applies here: a weekly run updates
existing issues and changes rather than creating seconds. Without that property a schedule would be
unacceptable in principle, so it is a precondition, not an improvement.

**Silence when there is nothing new.** If there are no findings and nothing changed, the run writes
nowhere: no "all clear" comment, no issue. A regular message that everything is fine trains people to
ignore the agent's messages, and a real remark stops being read too. The result of such a run is
visible in the manifest — that is enough.

**Concurrency.** Two runs mutating one default branch fight over the same branches and issues, so only
one runs at a time; skipping a week is safer. That is ensured by the CI platform's concurrency group,
not by a lock inside the agent. A private lock would mean state that must be released on failure: a
cancelled job leaves it held, and the schedule is silent not for a week but until someone notices. The
platform already knows how and does it before the agent starts spending.

**Separate budget.** A scheduled run has its own limits — time, steps and tokens — usually lower than
manual: nobody is watching it, and a configuration mistake must not become an unexpected bill. On
budget exhaustion the run ends inconclusive and says what it did not check.

**Escalation of repeated failures.** An inconclusive scheduled outcome is shown to nobody — there is
nobody looking at the check status. Therefore the agent remembers failure series (run memory below)
and, when the same reason of class "failure" — most often a registry page that changed shape or expired
access — appears a **second** time, opens **one** issue for people: key `capability:failure:reason`,
the same reconciliation as for findings, so the third and fourth run update it rather than file a
second. It closes the same way as everything else tracked: the check reached the end again.

The second time, not the third — because the series here is counted strictly: it lives only until the
first run in which that check finished. A counter of "two" already means the check has not worked for
two weeks running, not that two failures were remembered a month apart. Waiting for a third week to
report a broken vulnerability scanner costs more than occasionally reporting a two-week hiccup. A
single failure is never escalated: it stays in the run record and nowhere else.

That is the only case where a scheduled run breaks the silence rule, and it also closes the schedule's
main danger: a silently broken scanner that nobody learns about for months.

**Run memory.** The answer to "does this repeat" is the only thing a run cannot derive itself, and the
only state that writing to people depends on. Therefore it is not in the cache: the cache is allowed
to disappear, and the cost of losing it is time, not a missed message. State lives in a separate
git-ref (`refs/agent/state`, section 5.6) as one small JSON document: the ref is not a branch, so it
is in nobody's history and no change can be opened against it; commits chain to each other, so push
never needs `--force`; the document is written only when it changed, otherwise a run with nothing to
say would leave a commit a week in someone else's repository.

The parent is read before writing, not taken from an earlier read: a fresh checkout does not know about
the ref, and a commit without a parent is rightly rejected by the platform. Competing writes are not
merged — a rejected push becomes a warning, state stays as it was, and the cost of that is exactly
one: a repeat next week will be read as a first failure. That is the safe side of the error.

**Do not wake yourself.** A comment on an issue is a lawful way to start a run, and it is also the
most obvious way to get an infinite loop: the agent replies, the reply wakes the agent. Therefore the
event author is passed to the agent (`--actor`) and checked before the first spend: a machine comment
(`[bot]`) does not wake at all, and the login the agent itself publishes as stops the run with result
`declined`. The account is compared, not the text, because the question "is this my comment" has
exactly one honest answer. The run is still recorded: "the agent refused to answer itself" is a
property that must be visible, not an absence of trail.

**Publication time.** The schedule is set by the product and should fall in the team's working hours.
A change from the agent that appears Friday evening is either merged without attention or sits until
Monday and goes stale.

## 7. Budgets and degradation

How much a run may cost is set by the product in the overlay, and set inside the block that describes
the run itself: `review.limits` and `maintenance.limits` — a shared token ceiling, time per task and
how many tasks run at once. Neither block inherits the other: a few repeated lines buy that the run's
ceiling is visible where the run itself is named, rather than assembled by the reader from a models
section, a spending section and a volume section. Maintenance started by hand lives by the same
numbers as on a schedule: the work is the same, and a second set of numbers would just be a second
place that must be kept in agreement with the first.

How much a maintenance run may leave behind is a question of another nature: not "what does it cost"
but "how loud is the agent" — so that is a separate block inside the same section,
`maintenance.queue`.

Every key is required, and "no ceiling" is written as an explicit `null`: a missing key is a question
nobody answered, and treating it as permission to spend without limit would make the most expensive
setting the one nobody typed. One number stays with the agent, not the product —
`tool_calls_per_task`: it counts a step invisible from outside, and sits there as a fuse against a
session that stopped moving forward.

There is deliberately no money limit. It needs a price per model, and a limit computed from a price
list nobody maintains refuses work on a wrong number. When price accounting appears, the limit will
too.

Parallelism is limited not for thrift but because providers count requests: an unbounded fan becomes
a series of refusals, not a fast run. The shared ceiling and parallelism are introduced together —
four sessions, each in its own limit, still spend four times the agreed amount.

Behaviour on exhaustion: the task finishes with status `exhausted`, the run continues the other
tasks, the outcome becomes inconclusive, and the report names what was not checked. A task the shared
ceiling could not pay for is recorded the same way: not "skipped" but "not checked" — otherwise the
cheapest way through the gate would be to exhaust the budget. Result order stays plan order
regardless of who finished first: the report must not reshuffle between runs. Retry — one, and only
for transient reasons; repeating what does not exist in the ecosystem is pointless.

Cost accounting goes only from backend data. If the backend gives no detail, the run's cost is
recorded as unknown, not estimated from own counters: an invented number in the manifest looks exactly
like a real one, and a month later a conclusion "this model is cheaper" will be built on it. An
estimate is allowed only labelled as an estimate and is never summed with actual figures. Where there
is no accounting, the budget applies by time and step count — there is nothing to limit with money,
and pretending there is is worse than admitting the limit.

## 8. Security

The agent reads untrusted code and text, holds rights on the hosting platform and talks to external
models. Three groups of measures.

**Injections.** Everything external is fed to the model as an isolated block marked untrusted: file
contents, the diff, the change description, comments, registry answers, command output. Instructions
inside such a block are ignored, including requests to approve a change, skip a check or widen rights.
Subagent output is also untrusted input: it is validated against a schema and never executed as an
instruction.

**Code leakage.** Supporting several SDKs means source code may leave to different external parties,
so an egress policy is stated while there is still one adapter: which backend is allowed for which
product, redaction of secrets before a prompt is sent, a ban on sending files marked in the overlay as
never-send. A hosted model-proxy router is not used — that is another party receiving code.

**Rights.** Least privilege, a separate credential for editing workflow files, start only on human
actions, and for a run woken by a comment — a check that the comment author has write rights in the
repository. Without that check anyone who can comment on an issue controls the agent.

**Foreign code.** The head of a change from a fork is untrusted code, and a run over it **executes
nothing**: no linter, no dependency install, no product verification. The reason is not code quality
but the contents of the run itself: next to it sit the app token and the model-provider key, and any
command over a foreign manifest runs a foreign build script as the same user as the process that holds
those secrets. A scrubbed child-process environment does not save — the original is readable from
`/proc`. Real isolation is another security context (a networkless container, a separate user) or
refusal to execute, which is free.

Review continues: reading the diff, files and registries needs no commands. What disappears are
commands, verification and any prepared change — a check that needed a command records a
`not-permitted` gap, and "how do I fix this?" gets prose explaining why there is no patch. A head whose
origin the platform did not confirm is treated as a fork: a review that in a disputed case guesses
toward permission is a review an attacker gets by breaking one API call.

The hole this does not close is named outright: malicious code already merged into the default branch
is executed by a maintenance run as its own. The barrier there is only the change review before merge,
not the execution mode. Making that review notice *adversarial* changes — not only cooperative
mistakes — is planned work; see section 16.

## 9. Observability

Every run leaves a manifest: identifier, trigger, agent version, library digest, overlay hash, the
list of tasks with their outcomes, the "backend + model" pairs used, versions of utilities called,
tokens and money spent, every piece of evidence and every finding.

The manifest is not a luxury. It answers "why did the agent decide that" without a second run, and it
makes model evaluation possible: without recorded cost and latency the claim "this model is better for
this task" is uncheckable.

## 10. Configuration

| File | Contains |
| --- | --- |
| `agent/config/scenarios/*.yaml` | which tasks a playbook has, under which condition they turn on, split threshold |
| `agent/config/backends.yaml` | adapter settings (for example SDK sandbox) — no models and no roles |
| `agent/config/limits.yaml` | fuse on task step count and overlay notes size limit |
| `agent/config/library.yaml` | library pin: version and digest |
| `agent/config/ceiling.yaml` | utility and host ceiling, egress policy |
| `agent/config/storage.yaml` | fact-cache path, state ref |
| `agent/schemas/*.json` | schemas of what the model produces: task result |

Only what comes from the model is described by a schema — there form is guaranteed by nothing but the
check. Evidence, findings and overlay values are types in the code that owns them: a second
declaration of the same form in JSON eventually drifts from the first, and then it is unclear which is
true. Run-record retention is not our parameter either: the platform keeps it by its own rules, and
duplicating them here would mean contradicting them.

Configuration is part of the agent, not the library: these are run parameters, not knowledge. A
product overrides from it only what the library overlay describes.

Configuration lives **inside the package**, not beside it. That way it enters the distribution on its
own, with no exceptions in packaging rules, and an installed agent behaves the same as one run from
the repository. The directory is overridden whole by `--config-dir` — enough for debugging and for a
product with special ceilings.

The product overlay is two entities of different nature: `agent.yaml` with values and `NOTES.md` with
prose for the model. Values are schema-checked at startup, and an unknown key is a launch error. The
agent will not parse values out of prose: a rephrased sentence would change meaning silently, and a
silently changed quarantine length is worse than a loud parse error.

The overlay is organised by kind of run: the agent does two things — reviews a change and maintains
the default branch — and `review:` with `maintenance:` describe each entirely: `provider/model` per
role, spending ceiling, and for maintenance also `queue` — how much it may leave behind. Both sections
are required. There are no defaults for them in the agent at all, so a product without them does not
run — and that is better than a run on a model nobody chose. Adapter settings (SDK sandbox) stay in
the agent: the product chooses what it pays for, not how tightly what it paid for is confined.

**Which overlay review reads.** The overlay sets what a finding means in this product: quarantine
length, documented exceptions, which ecosystems are checked at all, and `NOTES.md`, which lands in
every task's prompt. Therefore review reads the overlay **from the merge base**, not from the change
under review. Otherwise one commit would be enough to zero quarantine, drop the ecosystem whose
dependency this change raises, or add to the notes an instruction to the model about what to conclude
— and the run would follow that instruction, reporting "nothing blocking". The change is checked
whole, but it does not write the rules by which it is judged; an edited overlay takes effect after
merge.

When the required version is not the one visible in the tree, review says so in the first line, and
the manifest writes which commit the overlay was read from. Two cases lawfully fall back to the tree,
and both are named: an overlay outside the repository is in no change, and a base without an overlay
is the change that introduces it, and there is nothing earlier to prefer. A maintenance run reads the
tree: there it is the default branch.

## 11. Compatibility and versions

The agent declares the library contract version range it supports; the library declares a minimum
agent version. The library is fetched as a versioned artefact by tag with a digest check; a digest or
version mismatch is a startup error.

The digest is computed over the knowledge content — identity, index and bodies of indexed documents —
not over the directory. Otherwise a tag checkout and an unpacked artefact would give different digests
for the same knowledge, and the pin would work only in CI. The agent itself computes it
(`agent pin --library`): a second implementation on the library side would eventually drift from the
one that checks, and then neither side could say which is right.

Release order does not change: knowledge library first, then the library pin inside the agent, then
the agent release, then product repositories.

## 12. Quality evaluation

Without measurements, choosing a model for a task remains taste. Therefore an eval harness lives in
the agent repository, and the goldens live in the library's `fixtures/`, because they change with the
knowledge.

A harness run gives per task recall and precision against the golden, latency and cost. Changing a
model or a notable edit to a role prompt goes through the harness; a drop in recall on class
`security` is blocking; a cost rise is a matter for discussion.

## 13. Implementation

Stack: Python and `uv`.

```text
/
├── README.md, DESIGN.md, CHANGELOG.md
├── pyproject.toml, uv.lock
├── agent/
│   ├── cli.py                 # review / maintain / explain commands
│   ├── orchestrator.py        # the run as a whole
│   ├── planner.py             # deterministic plan from trigger and diff
│   ├── intent.py              # free-form request classifier
│   ├── library/               # artefact load, digest check, slice by INDEX
│   ├── overlay.py             # product values by schema and its notes
│   ├── backends/              # port, cursor adapter, fake backend for tests
│   ├── tools/                 # deterministic tools + MCP server
│   ├── evidence/              # records and the run store
│   ├── storage/               # fact cache, state in git-ref, run record
│   ├── findings/              # keys, deduplication
│   ├── verdict.py             # block table, run outcome
│   ├── report/                # report body assembly
│   ├── scm/                   # GitHub adapter
│   ├── budget.py, manifest.py
│   ├── config/                # scenarios, models, ceiling, storage
│   └── schemas/               # evidence, finding, result, overlay schemas
├── tests/, eval/, scripts/
└── .github/
```

Stages, each ending in a working slice:

1. Skeleton: CLI, library and overlay load, planner, run manifest. No models.
2. Tools, evidence store and fact cache; acquisition phase fully deterministic.
3. Cursor SDK adapter, `analyst` role, `deps-vuln` task end to end, including verdict and report.
4. Remaining analysis tasks, parallelism, budgets, inconclusive outcome.
5. "Role → backend and model" mapping and role–backend compatibility check.
6. `fixer` role, worktree, maintenance run with issues and fix branches, schedule limiters.
7. Intent classifier and wake on a human comment.
8. Eval harness and first measurement; after that — deliberate model choice.
9. Second SDK adapter — when the core is already proven on the first.

## 14. Accepted decisions

Decisions that used to be open questions; details are in the named sections.

- There are three stores, not one: the run record as a CI artefact, the immutable-fact cache in the
  platform cache, state in git-ref `refs/agent/state`. Only runs on the default branch may write the
  cache and state (5.6).
- Finding texts are worded by `analyst`; the `writer` role works only where there is no finding behind
  the text — in a reply to a person on a comment (5.2, 5.3).
- A patch in reply to "how do I fix this" is prepared by `fixer`, not `writer`: the difference between
  the roles is what remains after the session — a verifiable tree or text. The explanation of the patch
  is written by the same `fixer` in the result field, so words and diff do not diverge (5.2, 5.3).
- The "intent → course" mapping is a table in code, and on low confidence the course is always
  `answer`: the cost of a misread comment is bounded by a session, not by a permission (5.2).
- The comment author's right to write in the repository is asked of the platform, not inferred from
  their organisation role; a non-answer counts as refusal (5.2).
- Cost is taken only from backend accounting; when there is none it is unknown, and the budget applies
  by time and steps (7).
- Disagreement of assessments on one finding is resolved by a "stricter of two" table, without
  escalation to a stronger model (5.7).
- Granularity is one task per capability; deterministic split by directory turns on only when a
  context threshold is exceeded (6).
- Escalation threshold for a repeated failure is the second run with the same reason, and the series
  lives only until the run in which the check finished: "two" already means two weeks of a broken check
  (6.1).
- The backend port is laid out for several agent SDKs at once, but one adapter is implemented — Cursor
  SDK; a second appears after the core is proven on the first (5.4).
- Configuration lives inside the package and is overridden whole via `--config-dir` (10).
- The product overlay is `agent.yaml` with values by schema and `NOTES.md` with prose; values are not
  extracted from prose (10).
- File edit is a tool, not a backend capability: the `fixer` role needs the same tool registry as
  `analyst` (5.3).
- Fixes are grouped by class and subject; the branch is named by them too (5.9).
- Verification counts as passed only for a whole surface; a failure that reproduces on the original
  state is marked pre-existing, but still does not ship the branch (5.9).
- Hosting is addressed through `gh api`, not a custom HTTP client: thread resolution exists only in
  GraphQL, and token, limits and pagination are already solved in the client (5.8).
- Own threads are recognised by a marker with the finding key in the comment body; a thread without a
  marker is treated as human and is not changed (5.8).
- Publishing is off by default and enabled by `--publish` (5.8).
- The publishing token is taken only from the named environment variables; a machine-saved client login
  is not used, and without a token nothing is published (5.8).
- The ban on two concurrent runs is enforced by a CI concurrency group, not an agent lock: a private
  lock would have to be released after failure, and a forgotten lock is silent longer than a skipped
  week (6.1).
- Run memory is written only when the document changed and chains by commit to the previous, so push
  is never forced, and a run with no news leaves no trail (5.6, 6.1).
- Wake is checked by the event author's account: a bot comment does not wake, and the agent's own login
  stops the run before the first spend (6.1).
- The wake playbook is determined by where the comment was left (change or issue), not by its text:
  triggers `comment-on-change` and `comment-on-issue` are known from the event (4, 5.2).

## 15. Found in operation

Defects and gaps that surfaced on live runs of the demo repository `amsokol/ai-devsecops-demo`. The
list is kept so findings do not dissolve between sessions; order is by the cost of the mistake for
whoever trusts the agent. Each item names the observation, the cause in the current design, and the
direction of the fix. What is fixed is removed from here into the CHANGELOG.

**1. False issue close from incomplete enumeration.** A run closed an issue about
`Swatinem/rust-cache@v2` with the wording "the capability finished and this finding is no longer among
its results", although the pin remained in `.github/workflows/ci.yml` on the default branch; the next
run filed an issue with the same key. The cause is that the coverage gate (5.7) checks that the
capability did look at the subject, but the pin list itself is built by the model, and its miss is
indistinguishable from disappearance of the problem. Direction: subject enumeration must become a
deterministic agent tool — from the files and patterns the ecosystem declares — and absence may count
only for subjects that list contains and for which a fact appeared in the run. Until that tool exists,
absence of a finding on a subject that used to be on the list must not count as proof.

**2. A returning finding opens a new issue instead of reopening the old one.** The finding key is the
same, but the issue is different: the thread, edit history and human comments stay in the closed one.
The promise "one subject — one ticket, updated in place" holds only while the finding never
disappeared. Direction: reconciliation (5.8) searches by key among closed issues too, and reopens a
returning finding with a comment that it returned.

**3. A fix PR has no lifecycle.** The PR stays open after its finding was closed and filed again;
neither closing the issue nor the finding's return affects it, and nothing stops the next run from
preparing a second branch for the same subject. Direction: the PR is marked with the finding key, so
its state is derivable — finding gone means the PR is closed with an explanation; finding returned
means the existing PR is reused or explicitly linked to the new issue.

**4. Refusal to fix does not reach the platform.** The reason for refusal ("none of the overlay's
verification commands ran in this task", "the overlay has no verification surface for this ecosystem")
is written only to the run report and manifest, and `issues.py` knows nothing about the fix phase. A
person sees the issue and does not understand why the work did not move — even though that refusal is
exactly the most valuable thing the agent said. Direction: a comment on the issue with the reason,
idempotent by run key, so repeated runs do not multiply the same messages.

**5. A multi-part finding hits the new-issue ceiling.** One `urllib3` pin brought nine advisories: five
issues filed, four deferred to the next run. The ceiling counts findings, not subjects, so one bad
dependency stretches across several runs, and in each of them a person sees part of the picture.
Direction: either count the ceiling by subjects, or adopt grouping of one subject's advisories into
one issue (the decision is taken together with the library; see its "Found in operation" section).

**6. A narrow run costs more than expected.** One `deps-vuln` task over one manifest — 2.2 million
tokens across two sessions and 2 minutes 19 seconds. Before choosing models by price, measure where
context goes: suspected are reloading library documents into every session and full API answers landing
whole in the conversation.

**7. The run record exists only for whoever started it.** The product keeps `.agent/runs/` in
`.gitignore`; in CI it is an artefact with limited retention. Everything the agent decided and
deliberately did not do is provable only locally, although that is exactly the part for which
traceability was built. Direction: a publishable run summary with a durable link — a comment, an
artefact or a records branch; the decision depends on what counts as long-term storage.

**8. Maintenance does not catch pins already on the default branch that break quarantine.** The
library already names a currently pinned version younger than **N** as a forbidden state, and says
maintenance must open or update an issue for it. Live runs still behave as if quarantine applied
only to *candidates* for a bump: what is already merged and still inside the window stays silent.
Direction: a maintenance run must evaluate every current pin against `check_quarantine`, not only
the versions it might move to, and turn a not-cleared current pin into a published finding (issue)
without "fixing" it by adopting a newer version that is also inside the window. The knowledge half
of the same gap is in the library design, section 16.

## 16. Planned: trust surfaces

Fork-safe execution (section 8) protects the *run*. It does not yet ask whether an external
contributor's change *reaches* places a maintainer must look at before merge. That is a different
problem from `code-vuln` or `deps-vuln`: the author is an adversary who reads the rules, the diff is
shaped to look ordinary, and asking a model "is this malicious?" is theatre — xz lived in a binary
test fixture and a build script; legitimate crypto and network code would drown such a question in
false positives.

What works is a deterministic census of **trust surfaces** in the change, plus one narrow question
for the model. Surfaces (computed in code, not by prompt):

| Surface | Why it carries trust |
| --- | --- |
| Opaque files | binaries, minified bundles, `.pyc`, wasm, fixtures a person does not read by eye |
| Build-time execution | `setup.py`, `build.rs`, `postinstall`, Makefile, `go:generate`, git hooks, Dockerfile, devcontainer |
| CI/CD | `.github/workflows/**`, especially `pull_request_target`, `workflow_run`, self-hosted runners, secret access, actions not pinned by SHA |
| Dependency sources | new registry/index URL, git and path deps, `overrides`, lock diverging from the manifest, typosquat-like names |
| Weakened checks | removed test, `# nosec`, `eslint-disable`, `InsecureSkipVerify`, `verify=False` |
| Text that is not what runs | bidi / homoglyphs (Trojan Source), zero-width characters, anomalous line length, high entropy |

The model's job is then only: does the change do what it claims, and is there something beyond that
claim? A backdoor almost always needs something extra relative to its cover story — and that "extra"
is what a model names well.

**In this architecture.** A new capability (library: `capabilities/trust-surfaces`), not folded into
`code-vuln` — different presumption, different evidence, different action. A deterministic extension
of `read_change` that reports the *shape* of each file in the diff (binary, workflow triggers, bidi,
entropy), which today's tools do not see. Findings are **holds**, not fixes: "these N hunks need a
maintainer's eyes", unlocked by the same stamp mechanism as majors (5.10). Published wording never
says "malicious" and never judges the author — only what the change reaches and what it asks beyond
its stated goal. The capability is optional per product: a closed repo without external contributors
need not spend tokens on it; an open one needs it most.

**Honest limit.** This is not an authorisation boundary. It makes cheap attacks expensive and
expensive ones visible, and tells a maintainer what they must read — not that they can stop reading.
Whether it works is measured only against real incidents as eval fixtures (xz, event-stream,
tj-actions, Trojan Source). Without that, we cannot tell catching from noise.

The knowledge side of the same plan is in the library design, section 17.

## 17. Planned: design-faithful review

A healthy delivery practice is to write a **proposal / design document** before coding: what new
functionality you intend to ship, why, and how it should behave. That document is how teammates, the
project lead, product and anyone else who must care align — before implementation starts. Only after
the design is agreed do you build.

Then the hard part appears on the pull request. Whoever reviews the new feature is supposed to check
the diff against that agreed document and confirm: *what we approved is what landed*. At PR speed
humans usually cannot: the review becomes spot-checks, vibe, and trust that the author “followed the
design.” Gaps between the document and the code slip through. That is the problem the first article
named; this generation still has not closed it.

**In this architecture.** A capability (library: `capabilities/design-faithful`) on the review
playbook — compare the implementation to the brief that was written and agreed, surface mismatches,
and open a dialogue with the developer on those points (the same wake / intent / answer loop as today),
not only comment on local quality or advisories. How the product attaches a design doc to a change —
path convention, PR body link, label, overlay note — lives in the overlay (`NOTES.md` and values if
needed), because that attachment is product-specific. Verdict behaviours stay the ones already owned:
request changes when the code drifts from the brief in a way the policy table allows to block, discuss
when it only needs eyes, never invent a fourth bot.

**What is not yet decided.** Which artefact counts as “the agreed design” when several exist; how the
agent proves it read the version that was approved rather than one edited in the same PR (trust
boundary: merge-base / linked issue, same spirit as overlay-from-merge-base); whether a missing design
doc for a large feature is a finding or a gap. Those belong in the capability and overlay template
when the work starts. Eval needs paired fixtures: a design brief plus a diff that quietly diverges.

The knowledge side is in the library design, section 18.

## 18. Planned: deployment and infrastructure capabilities

Today the catalogue stops at code and dependencies. A large class of risk lives one layer out: how
the product is **deployed** — cloud accounts, Kubernetes clusters, serverless runtimes, container
orchestration, IaC that provisions them. A PR that only touches `Dockerfile`, Helm, Terraform, or a
cloud workflow can open the world without changing a line of application code, and none of the
current capabilities is obliged to look there as a first-class job.

**In this architecture.** One or more library capabilities under a clear family (working names:
`capabilities/deploy-k8s`, `capabilities/deploy-cloud`, and siblings as surfaces mature) on both the
review and maintain playbooks where the product enables them. Judgement stays in prose: overly open
RBAC, secrets in plain values, privileged containers, public buckets, missing network policy, drift
between declared and running shape. Facts that can be established without a model — which resources
a change touches, which API versions, which images and tags — belong in deterministic tools the
agent owns (parse and census of known manifests), not in a prompt that asks the model to "find
misconfigurations". Findings follow the same publish and wake loop as today; fixes ship only when
the overlay names a verification surface for that ecosystem of deployment, otherwise they stay
human-only with an explicit reason.

**What is not yet decided.** Whether cloud, Kubernetes and generic IaC are separate capabilities or
one with ecosystem profiles; how far maintenance goes beyond the tree (live cluster / account state
vs manifests only); which product overlays opt in first. Eval needs fixtures that look like ordinary
infra PRs and still carry a known breach. Until those land, deployment review remains a gap a
scanner or a person fills outside this agent.

The knowledge side of the same plan is in the library design, section 19.
