# Changelog

What changed in the agent, and why. Written for whoever has to decide whether to move a product's
runner pin: every entry says what a run will do differently afterwards.

Two version numbers matter together. This one is the runner; the knowledge it executes has its own,
pinned in [`agent/config/library.yaml`](agent/config/library.yaml) and verified by digest at startup,
so an entry that changes behaviour by moving that pin says which library version it moved to.

Versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) read as Cargo reads it
while the major number is `0`: the **middle** number moves on a breaking change, and the **last** one
on everything else, fixes and additions included. Breaking means a product has to change something to
adopt it — the command line, the overlay shape, a required overlay key, or the library contract range.
Until 1.0 those are all still allowed to move; every time one does, it is named here.

## Unreleased

The first release is not cut yet, so this section is the whole history: what the agent does today.
Once a version is tagged, this heading is replaced by that version and no `Unreleased` section is kept
— an empty one is a step to forget, and the library repository has already forgotten it once.

### Added

- A deterministic core with the model kept to the parts that need judgement. Planning, evidence
  keeping, deduplication, the verdict table and every arithmetic are code; the models analyse, decide
  severity and write prose. Facts come from tools — registries, advisories, the repository — never
  from a model's memory.
- Analysis tasks per capability, run concurrently, each in its own session with its own budget, and a
  run record naming every model, library digest, token count and piece of evidence behind the
  verdict.
- A tool registry the subagents share: reading files, the changed lines of a diff, running the
  commands an ecosystem declared, and fetching from the hosts it declared, all within a ceiling the
  agent owns.
- Roles bound to a backend and model by the product, checked against what each adapter can do before
  anything is spent. The Cursor SDK is the first adapter; the port exists so a second one does not
  touch the core.
- A `fixer` role for maintenance runs: it edits files in an isolated worktree, and the agent stages,
  commits, pushes and proposes. A fix ships only when every command of its surface passed, and a
  failure that was already red on the unchanged head is reported as pre-existing rather than blamed
  on the fix.
- Publishing that reconciles instead of repeating: review threads and issues are matched by finding
  key, updated in place while a problem persists, and closed with evidence when it is gone. The agent
  publishes as itself through a GitHub App, never as the developer who started the run.
- Restraints for the run nobody watches: silence when there is nothing new, a ceiling on how many
  issues and fix branches it may leave behind, its own spending limits, cross-run memory in
  `refs/agent/state`, and an escalation issue when the same check fails twice in a row.
- Being woken by a comment: somebody replies in one of the agent's threads or on one of its issues,
  the `intent` role reads what they ask for, and a table in code turns that into one of four courses
  — answer in prose, prepare the change and offer it, re-establish the one finding it was written on,
  or do nothing. Where the comment was left decides the playbook before any model runs; what it says
  decides only the course.
- An answer to "how do I fix this?" that carries the fix. A `fixer` session makes the edit in a
  throwaway copy of the repository at the head of the change, the overlay's verification runs over it
  there, and the thread gets the session's paragraph, the diff **git** reports, and the label that
  verification earned — as a `suggestion` block when the edit replaces exactly the lines the remark
  hangs on, and as a diff otherwise. Nothing is committed and nothing is pushed: the branch under
  review moves only if a person moves it. Needs `fixer` bound in `review.models`; without it those
  questions are answered in prose.
- The `writer` role, used for exactly one thing: replying to a person. The answering session can read
  the repository and gets no worktree, the reply resolves nothing and closes nothing, and every fact in
  the published comment — run, finding key, marker — is the agent's own.
- A status note on an issue after a run somebody woke, written by the agent from recorded facts: what
  the check found, what the fix session did, and where the prepared change can be reviewed.
- One decision, taken before any session starts, about whether this run may execute anything it
  reads. A change whose head lives in this repository is the repository's own work and runs its
  commands as before. A change from a fork is read and executed never: the command tool is not in the
  toolkit, a session that asks for it anyway is told to record the gap instead, no fix branch and no
  prepared patch are produced, and the report opens by saying so. A head the platform will not place
  — because it was not asked, or would not answer — is treated as a fork, since a review that guesses
  in the permissive direction is one an attacker can arrange by breaking a single API call.
  `--outside` forces that posture and can only take permission away.
- Findings that wait for a person, and approvals that release them. A major move is held from the
  fix queue — reported, never changed — and its issue says what it waits for and what a comment there
  will cause. The hold comes from two places that fail in opposite directions: the agent measures a
  semantic-version major from the finding's `target` and holds it whatever the session said, and a
  session declares the majors no comparison can see, such as a `@v5` to `@v7` action pin or a raised
  toolchain floor. A declaration can only add a hold, never remove one, and a security remediation is
  never held by the arithmetic. An approval is a comment from an account with write access, stamped
  into the issue body so it is read without a model and asked for exactly once; the same run then
  prepares, verifies, pushes and proposes the change, and says so on the issue. The stamp outlives a
  fix that would not verify, because approval is for the move rather than for one attempt at it. A run
  that cannot read its issues ships nothing that waits and says why.

### Changed

- The result file is named to a session by absolute path, and a result written elsewhere inside the
  session's own workspace is read where it landed rather than declared missing. The first live run on a
  six-ecosystem repository finished an analysis, wrote the file one tree deeper — a relative path
  resolved against the session's working directory, which is not the repository root — and paid for the
  whole task twice. The record names the salvaged path, so a habit of writing elsewhere stays visible.
- `fetch` can take one field from every member of a collection (`select: "*.tag_name"`), and a document
  refused for its size now says how long it is and what a member holds. A list of releases is the shape
  a version list arrives in from a hosting platform, and without a projection the only way to read tag
  names was to ask for the array whole: a hundred kilobytes of release notes, refused, twice, because a
  refusal that says only "too large" leaves nothing to narrow towards.

- Reads of the hosting platform's API carry the agent's own credential, on that platform's hosts and
  nowhere else. Anonymous access there allows sixty requests an hour, which one ecosystem of one task
  exhausted on the first live run of a repository with six: the facts a quarantine decision needed went
  missing, the findings resting on them disappeared, and the same repository came back blocked or
  passing depending on what the previous hour had spent. Nothing else changes hands — no session sees
  the token, no command's environment carries it, the tool is still GET-only and still confined to the
  allowlist, and a redirect that changes host drops the header before following. The run record says
  whether the API was read as somebody or anonymously.
- No hosting client in the ceiling, and the image registries in it. A task's commands get an
  environment with no credentials, so `gh` could only ever fail while looking available; the agent's own
  publishing never went through the ceiling. `hub.docker.com`, `registry-1.docker.io`, `auth.docker.io`
  and `ghcr.io` are permitted, because the library now names them and a host nobody names is a host
  nobody grants — container image facts were unobtainable while the profile called them reproducible.
- The pinned knowledge library is `0.4.5`: acquisition recipes no longer send a session to a command
  that has to log in, an action's publish date comes from the platform API with the tag-without-a-release
  case covered, the image registry hosts are named rather than described, and the contract says what an
  absent target means — a pin with nowhere to move is reported, not fixed.
- A finding about a package that names no version to move to is reported and not queued for a fix.
  Quarantine produces one every week: the newest release is real, it is worth reporting, and there is
  nothing to move to until the clock runs out. The first live maintenance run queued one anyway, and
  the session did what a session asked to fix an unfixable pin does — it invented a move, downgrading
  an action by a major version that nobody had asked to downgrade and no evidence supported.
- The repository's checkout is watched while sessions run, instead of the agent trusting a backend's
  sandbox to keep them out of it. A write that appears there is copied into the run record, undone, and
  the attempt refused. On the same live fix two sessions edited the checkout through the backend's own
  file tools, left the worktree they were given empty, and were recorded as having claimed a fix
  without making one — two paid sessions wasted, two misleading refusals, and a developer's tree
  modified in files nobody had asked about. Uncommitted work that predates the run is left alone.
- A command is told where the machine's toolchains live and given a cache of its own to download into,
  instead of a `PATH` of three directories and a home nobody ever installed anything in. The first live
  fix failed on that and nothing else: `cargo clippy` and `cargo test` answered "no default toolchain is
  configured", which made verification — the thing that decides whether a fix ships — impossible to pass
  on any Rust repository regardless of the change. `RUSTUP_HOME`, `GOROOT`, `JAVA_HOME` and their kin are
  passed through when the agent has them; downloads land in `.agent/tools` rather than in a home
  directory, since a crate registry in somebody's home may hold their publishing token. The rest is
  unchanged: no credential of the agent's reaches a command, and its home directory still dies with the
  task.

- A status note on a woken issue now reports what the run actually did. It asked whether the owning
  check finished `clean`, which is the rule for *closing* an issue, so a recheck that found the
  finding still present always answered "the check did not finish" — and never mentioned the fix it
  had just prepared and proposed.

- `--wake-comment <id>` names the comment that woke a run and is required with `--actor`. A wake with
  no comment to read is a run guessing what it was asked. The `human-comment` trigger is now
  `comment-on-change` and `comment-on-issue`, so the playbook comes from the event rather than from a
  model's reading of free text.
- A refusal to act on a comment now covers everything that can be checked before spending anything: a
  bot, the agent's own account, an account without write access — including one the platform will not
  answer about — a conversation with no marker, and a comment whose author is not the actor the event
  named. Each is recorded as a declined run with its reason, because "it did not take orders from a
  reader" is a property that has to be visible to be believed.
- `review.models` and `maintenance.models` in the overlay now also bind `intent` and `writer`. Both are
  required wherever a comment can wake a run, checked at startup: discovering that the answering model
  is unbound after classifying would leave a person with silence.
- Models and spending ceilings live in the product's overlay and nowhere else; the agent ships no
  model name and no ceiling. The overlay is organised by kind of run — `review:` and `maintenance:`,
  each with its own models and limits — because a product outlives any one provider and switching
  should be one line, not a fork of the agent's configuration.
- A review reads the overlay from the merge base, so a change cannot rewrite the rules it is judged
  by. Where the two differ, the report says so in its first line. A base whose overlay this agent
  cannot read is the exception, and also says so: without it, the shape of an overlay could never
  change again, because every such change would need a run that already understood the new shape.
- "How do I fix this?" on a fork gets a paragraph and a sentence saying why there is no change to
  click: one could not have been verified without running the fork's code, and an unverified edit is
  not worth offering.
- The pinned knowledge library is `0.4.3`: it names the two roles a comment wakes, narrows a woken
  maintenance run to the finding it was written on, says that a fix task does not always end in a
  branch — its change may be offered to whoever asked how to fix it — records why the merge is held
  by a required check rather than by the platform's approval flow, and states what a review of a fork
  may and may not establish, including the workflow shapes that hand a fork's code the job's
  credentials. It also makes a hold a field on the finding rather than a rule to remember, says that
  an unlock stays granted across a failed attempt, and corrects the tool names it promised, which
  until now sent sessions calling tools this agent never had.
