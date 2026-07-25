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

### Changed

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
- The pinned knowledge library is `0.4.1`: it names the two roles a comment wakes, narrows a woken
  maintenance run to the finding it was written on, says that a fix task does not always end in a
  branch — its change may be offered to whoever asked how to fix it — and records why the merge is held
  by a required check rather than by the platform's approval flow, which matters to whoever sets up
  branch protection.
