# Issue tracker: Beads

Issues and specs for this repo live in Beads, a dependency-aware issue tracker stored in `.beads/`
(Dolt database). Use the `bd` CLI for all operations; add `--json` when parsing output.

## Conventions

- **Create an issue**: `bd create "Title" -t <bug|feature|task|epic|chore|decision|spike> -p <0-4> -l <labels> --body-file - <<'EOF' … EOF`
  (or `-d "…"` for a one-liner). Priority 0 is highest, default 2. `bd create` prints the new ID.
- **Create a child of an epic/spec**: add `--parent <epic-id>`.
- **Blocking edges**: at creation, `--deps blocked-by:<id>,blocked-by:<id>`; afterwards,
  `bd dep add <id> --blocked-by <blocker-id>`. Inspect with `bd dep tree <id>`.
- **Read an issue**: `bd show <id>` (description, deps, labels) and `bd comments <id>`.
- **List issues**: `bd list` (open by default) with `--status`, `-l/--label`, `--parent`, `--all`, `--json`.
- **Unblocked work**: `bd ready` (open, no open blockers), filterable with `--parent`, `-l`, `-u`.
- **Comment on an issue**: `bd comment <id> "…"` (or `--file`).
- **Apply / remove labels**: `bd update <id> --add-label <l>` / `--remove-label <l>` (or `bd label add/remove`).
- **Claim**: `bd update <id> --claim` (assigns you, status `in_progress`).
- **Close**: `bd close <id> -r "reason"`; `bd close <id> --suggest-next` shows what it unblocked.

## When a skill says "publish to the issue tracker"

Create a Beads issue with `bd create`. A spec is an issue of type `epic`; its implementation tickets
are its children (`--parent`), with blocking edges as `blocked-by` dependencies.

## When a skill says "fetch the relevant ticket"

Run `bd show <id>` and `bd comments <id>`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is an epic with **child** issues as tickets.

- **Map**: `bd create "…" -t epic -l wayfinder:map`, holding the Notes / Decisions-so-far / Fog body.
- **Child ticket**: `bd create "…" --parent <map-id> -l wayfinder:<research|prototype|grilling|task>`,
  with the question in the body.
- **Blocking**: native dependencies, `bd dep add <child> --blocked-by <blocker>`. A ticket is
  unblocked when every blocker is closed.
- **Frontier query**: `bd ready --parent <map-id> -u --json`; first by priority, then creation order, wins.
- **Claim**: `bd update <id> --claim`, the session's first write.
- **Resolve**: `bd comment <id> "<answer>"`, then `bd close <id> -r "resolved"`, then append a context
  pointer (gist + ID) to the map's Decisions-so-far (`bd edit <map-id>` or `bd update <map-id> -d …`).
