# fir-exts

Monorepo of [fir](https://github.com/kfet/fir) extensions + their companion skills.

Each extension lives in its own top-level directory holding the `.py` extension
and (optionally) a `SKILL.md` presentation skill:

```
fir-exts/
  reminders/
    reminders.py      # extension: tools + lifecycle hooks
    SKILL.md          # skill: how the agent should present/manage them
```

## Install

Whole repo (all extensions):

```
fir install github.com/kfet/fir-exts
```

Single extension (sparse checkout):

```
fir install github.com/kfet/fir-exts/reminders
```

> **Caveat (fir ≤ current):** subdir installs of *two different* subdirs from the
> same repo collide — the clone dir is keyed on `host/org/repo` only, so the
> second install hits the existing clone, does a plain `git pull`, and never runs
> `git sparse-checkout add`. Result: silent `Discovered: 0 skill(s), 0 extension(s)`.
> Until that is fixed, use one subdir per repo, or install the whole repo.
> Per-package `extensions`/`skills` filters are documented in `docs/packages.md`
> but are **not implemented** — object entries in `settings.json` only have their
> `source` field read.

Then `/reload` (or `!reload` via poe-acp) in the session you are actually talking
to — reload is **session-scoped**, not process-scoped.

## Extensions

### `reminders`

Durable reminders. **jsonl is truth, memory is cache.**

- Tools: `reminder_add`, `reminder_list`, `reminder_done`, `reminder_snooze`
- Formats: `45m`, `1h30m`, `2pm`, `14:00`, `tomorrow 9am`, ISO, epoch
- Store: a **directory** of per-host shards. Precedence:
  1. `$FIR_REMINDERS_STORE` (if it names an existing file or ends in `.jsonl`,
     the old single-file mode is kept, so in-place upgrades never lose items)
  2. `$XDG_STATE_HOME/fir-reminders/` — the default
  No other path is consulted: the extension hardcodes no infrastructure paths
  and probes no well-known directories.
- **Sharded writes, shared reads:** this host appends only to
  `<store>/<shard>.jsonl`, where shard = `$FIR_REMINDERS_SHARD` or the hostname
  (sanitised to `[A-Za-z0-9._-]`). Reads glob every `*.jsonl` in the store and
  reduce the union, ordered by `(at, shard filename, line number)`. Since no two
  hosts write the same file, a folder-syncing tool can host the store without
  ever producing a write conflict — and a `.sync-conflict-*.jsonl` that shows up
  anyway is simply read as one more shard. Half-synced/truncated lines are
  skipped silently.
- **Fleet-wide reminders are deployment config, not a feature.** Point
  `$FIR_REMINDERS_STORE` at a directory that is synced between your machines —
  e.g. a Syncthing or Dropbox folder, or a shared mount — and every host's shard
  lands in the same place, so any host can see and close any reminder:
  ```sh
  export FIR_REMINDERS_STORE=~/some-synced-folder/reminders
  ```
  Leave it unset and everything stays local to this machine.
- **Migrating an old single-file store:** on first run in a directory store,
  if this host has no shard yet, one file is copied in to seed it (source left
  untouched). Sources: `$XDG_STATE_HOME/fir-reminders/reminders.jsonl`, plus an
  optional colon-separated `$FIR_REMINDERS_MIGRATE_FROM` list of extra files.
- `done` is **absorbing**: once a reminder is closed, later `snooze`/`delivered`
  ops for that id are ignored regardless of timestamp, so skewed host clocks
  cannot resurrect it. Delivery is *not* de-duplicated across hosts — the same
  reminder may nag on two machines; closing it anywhere settles it.
- Delivery: sweep at `turn_start`, steered into the live turn. No daemon, no
  in-process timer — conversation sessions get evicted on idle TTL, so a sleeping
  thread cannot be trusted. **Waking up is the trigger.**
- Scope: per-conversation by default (parsed from the session transcript path),
  or `any` for first-waker-wins.

**Deliberate contrast with builtin `schedule.py`:** schedule *skips elapsed wakes*
on restore — correct for "wake me in 45m", fatal for a reminder. This fires **late
and says so** (`overdue by 3h 12m`) rather than dropping silently.

Anti-nag: the delivered tombstone is appended *before* surfacing (a crash mid-inject
cannot re-nag forever), unclosed items re-nag at most every 15 min, and auto-snooze
24h kicks in after 5 surfacings.

**Known limit:** punctuality is bounded by session liveness. A reminder due at 15:00
with no session activity until 18:00 lands at 18:00, labelled overdue. Fleet
targeting is likewise not a feature: express it in the reminder text ("only fire
on kopitwo") and the model reading it honours that.

### `extreload`

One tool, `ext_reload(name)` — hot-reloads a named extension in the live session
via `ctx.reload_extension`. Useful while iterating on another extension without
restarting the session or nuking conversation context.

Note: reload re-launches from the path the extension was **registered** at during
session start. Moving a file on disk and reloading will not re-resolve it — that
needs a full `/reload`.

**Overlap with fir builtins (why this exists):** targeted single-extension reload
is already an SDK API (`ctx.reload_extension`), but no *default-loaded* tool
exposes it. `/reload` reloads everything and is session-scoped; `forge_tool`
reloads only extensions it wrote to the global `extensions/` dir, so it cannot
target a package extension; `reload_ext_demo` in builtin `demo.py` is equivalent
but demo.py is opt-in (`-e demo`). `extreload` is a stopgap until fir exposes
`reload_extension` as a default tool or `/reload <name>` — delete it then.

fir enforces two constraints regardless: builtins cannot be reloaded, and an
extension cannot reload itself (so `extreload` can never reload `extreload`).
