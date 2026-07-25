# Changelog

What changed in the agent, and why. Written for whoever has to decide whether to move a product's
runner pin: every entry says what a run will do differently afterwards.

Two version numbers matter together. This one is the runner; the knowledge it executes has its own,
pinned in [`agent/config/library.yaml`](agent/config/library.yaml) and verified by digest at startup,
so an entry that changes behaviour by moving that pin says which library version it moved to.

Versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html). Before 1.0 the command
line, the overlay shape and the run record are all still allowed to change in a minor release, and
every such change is named here.

## Unreleased

The first release is not cut yet, so this section is the whole history: what the agent does today.

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
- A refusal to be woken by itself or by another bot, so a comment the agent wrote cannot start the
  next run.

### Changed

- Models and spending ceilings live in the product's overlay and nowhere else; the agent ships no
  model name and no ceiling. The overlay is organised by kind of run — `review:` and `maintenance:`,
  each with its own models and limits — because a product outlives any one provider and switching
  should be one line, not a fork of the agent's configuration.
- A review reads the overlay from the merge base, so a change cannot rewrite the rules it is judged
  by. Where the two differ, the report says so in its first line.
