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
- Store: `$FIR_REMINDERS_STORE`, else `$XDG_STATE_HOME/fir-reminders/reminders.jsonl`
  (a pre-existing legacy `~/.local/state/poe-acp/notes/reminders.jsonl` is honoured
  so in-place upgrades never lose pending items)
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
with no session activity until 18:00 lands at 18:00, labelled overdue.

### `extreload`

One tool, `ext_reload(name)` — hot-reloads a named extension in the live session
via `ctx.reload_extension`. Useful while iterating on another extension without
restarting the session or nuking conversation context.

Note: reload re-launches from the path the extension was **registered** at during
session start. Moving a file on disk and reloading will not re-resolve it — that
needs a full `/reload`.
