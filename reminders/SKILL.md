---
name: reminders
description: Set, surface, and manage durable reminders. Use when the user says "remind me…", "don't let me forget…", "in 20 minutes…", asks what reminders are outstanding, or when a `[reminders]` block appears in the conversation announcing due items.
---

# Reminders

Durable, file-backed reminders delivered by **lazy catch-up** — no daemon, no
timer. The clock does not fire a reminder; **waking up does**.

## Architecture (why it works this way)

- **Store:** a *directory* of append-only op logs (`add` / `done` / `snooze` /
  `delivered`). jsonl is truth, memory is cache. Records replay from the log,
  so new fields degrade safely on old stores.
- **Fleet-wide, sharded writes / shared reads.** If a shared store directory
  exists, reminders are fleet-wide: each host appends **only** to its own shard
  `<store>/<hostname>.jsonl`, and every read globs `*.jsonl` and reduces the
  union. Two hosts never write the same file, so a file-sync tool (Syncthing,
  Dropbox, anything that syncs a folder) cannot produce a conflict on it. A
  reminder set on one host is visible — and closable — from any host.
- **Store precedence:**
  1. `$FIR_REMINDERS_STORE` — the store directory. If it names an existing
     file or ends in `.jsonl`, the old single-file mode is used.
  2. `$FIR_SHARED_DIR/reminders/`, then `~/sync/shared/reminders/` — but
     **only if that directory already exists**. Creating it is the opt-in;
     merely having a synced folder never relocates anyone's reminders.
  3. a pre-existing legacy `~/.local/state/poe-acp/notes/reminders.jsonl`
     (migration path only — never invented for a fresh install).
  4. `$XDG_STATE_HOME/fir-reminders/` — local-only default.
- **Enabling fleet sync:** put a synced folder anywhere, `mkdir` a `reminders/`
  subdir in it, and point `$FIR_SHARED_DIR` at the folder (or use the
  `~/sync/shared` convention and skip the env var entirely). `$FIR_REMINDERS_SHARD`
  overrides this host's shard name; the default is the hostname, sanitised to
  `[A-Za-z0-9._-]`.
- **`done` is absorbing.** Once a reminder is closed anywhere, later
  `snooze`/`delivered` ops for it are ignored no matter what their timestamps
  say — hosts have skewed clocks and a closed reminder must stay closed.
- **Delivery:** the `reminders` extension sweeps on `turn_start` (turn already
  in flight) and steers a `[reminders]` user-role block into the live turn.
  This is the proven injection path; the turn is not aborted.
- **No in-process timer.** `poe-acp --session-ttl` evicts idle conv sessions, so
  a thread sleeping until 3pm dies when you go quiet at 2:30. The file survives.
- **Fires late, and says so.** Deliberately inverts builtin `schedule.py`, which
  silently drops elapsed wakes — correct for "wake me in 45m", fatal for a
  reminder. Late-and-honest beats silent-and-lost.

Consequence to be honest about: a reminder due at 15:00 surfaces on the **first
turn at or after 15:00**. If nobody speaks until 18:00, it arrives at 18:00
labelled `overdue by 3h`. Punctuality would need an external tick pushing at a
live session socket — not built, deliberately.

## Tools

| Tool | Use |
|---|---|
| `reminder_add(text, due, scope, repeat)` | `due`: `45m`, `1h30m`, `2pm`, `14:00`, `tomorrow 9am`, ISO, epoch. `scope`: `any` (**default**, first live session that wakes) or `here` (this conversation only — see the dead-letter warning below). `repeat`: optional interval (`8h`, `1d`) — see [Recurring and conditional reminders](#recurring-and-conditional-reminders). |
| `reminder_list(all)` | Pending for this conversation; `all=true` for done + other scopes. |
| `reminder_done(id)` | Close it. Stops surfacing. |
| `reminder_snooze(id, for)` | Defer: `1h`, `30m`, `9am`. |

## When a `[reminders]` block appears mid-turn

It is injected machinery, not the user talking. Handle it like this:

1. **Surface it in your reply**, at the top, before answering their actual
   question — brief, not a wall. Mark overdue items as overdue.
2. **Do not silently swallow it.** If it arrived, the user asked for it.
3. **Answer their real question too.** The reminder is an interrupt, not a
   replacement for the turn.
4. **Close the loop** — call `reminder_done` if the reminder is plainly
   handled by this exchange, or `reminder_snooze` if the user defers. Leave it
   pending only if genuinely still outstanding.
5. **A reminder may surface on more than one host** — the store is fleet-wide
   but delivery is not de-duplicated. If the user has already dealt with it,
   just mark it done and move on; do not treat the repeat as significant.

### Surfacing style

```
⏰ **Reminder** (overdue 3h 12m): check the triage ledger
```

Batch if more than three: one line each, no commentary per item.

## Anti-nag contract

A surfaced-but-unclosed reminder re-nags at most every **15 minutes**, and after
**5 surfacings** auto-snoozes 24h with a note. This is deliberate: a reminder
you learn to ignore is worse than no reminder. If something has been surfaced
repeatedly, say so and ask whether to close it.

An **explicit** `reminder_snooze` resets that surfacing counter — deferring
something is a fresh start, not another nag, so a repeatedly-deferred reminder
keeps its cadence instead of silently decaying into the 24h floor. The
automatic 24h snooze is exempt, so the escape hatch stays terminal.

A reminder with `repeat` set is outside this contract entirely: it re-arms one
interval out every time it fires, resets its counter, and never auto-snoozes.
It ends only at `reminder_done`.

## Recurring and conditional reminders

`repeat` turns a reminder into a self-re-arming loop. Two uses:

**Recurring nudge** — `repeat: "1d"` for something that should keep asking
until it is actually done.

**Poll until a condition holds** — the pattern to reach for when the trigger is
an *event you cannot subscribe to* (the user gets home, a release lands, a host
comes back up). Do not build a daemon for this. Write the check into the
reminder text and let the future agent run it:

```
reminder_add(
  due="8h", repeat="8h", scope="any",
  text="CHECK: run `tailscale status --json` and look at kfetphoneair. "
       "HOME if CurAddr is on 192.168.50.x or relay is `sea`. "
       "IF NOT HOME: say nothing to the user, do not call reminder_done — "
       "this re-arms itself in 8h. "
       "IF HOME: deliver <payload> and call reminder_done(<id>).")
```

Why this beats a background watcher: the condition is **persistent**, not a
transient event. "Is the user home?" is still true the next time anyone looks,
and since you only ever act on the user's turn, nothing could consume an
arrival event sooner anyway. Continuous capture buys nothing and costs a
process to maintain.

Two things the reminder text must always carry, because the agent that reads it
has none of this conversation:

1. the **exact command** to run and how to read its output, and
2. explicit instructions for **both** branches — including "stay silent and let
   it re-arm" for the not-yet case, so a future agent does not surface a
   half-check as noise or close the loop early.

Never emulate this with `done` + re-`add` by hand: that was the old workaround
for the counter bug above, and it loses the id across cycles.

## Creating one

**Default scope is `any`, and that is usually right.** `any` fires in the first
session that wakes at or after the due time, wherever the user shows up, exactly
once.

**`here` is a dead-letter risk — reach for it deliberately, never by habit.** A
`here` reminder is matched by conversation id, so it is delivered *only* by a
turn in that same conversation. If the user deletes the chat, or simply does not
come back to it before the due time, the reminder sits `pending` forever: no
delivery, no error, no notice. The record survives on disk and helps nobody.

Use `here` only when both hold:

1. the reminder is meaningless outside this thread (it refers to "the branch we
   just discussed", with no self-contained context), **and**
2. you have positive reason to expect the user back in this thread by then.

Otherwise use `any` — and **write the text to stand alone**. It may surface in a
conversation with none of this history, so include the repo, branch, sha, file
path, or command needed to act on it. "Check on that thing" is useless in a
fresh chat; "check go-sdk for v1.7.1; parked work on branch X @ sha Y" is not.

Rule of thumb: if the text would still make sense pasted into a brand-new chat,
it wants `any`.

Confirm back with the resolved absolute time, not just the relative one — "in
90m" is ambiguous to a user on a different clock; "2026-08-01 04:56 (in 1h 30m)"
is not. Say the scope too, so a `here` reminder's narrower reach is never a
surprise.

Also be honest about the structural limit when it matters: delivery is lazy
(nothing pushes — see Architecture). Whether the store is **fleet-wide or
local** depends on the precedence above: with a shared store dir, a reminder
set here can fire through a bot on any other synced host, and a `repeat` poll
may run its check from a host that is not this one; without one, everything
stays on this machine.

**Host targeting lives in the reminder TEXT, not in `scope`.** There is no
host field. If a reminder only makes sense on one machine, say so in the text
— "only fire on kopitwo", "only in bot-two", "run this on zbox" — and the
agent reading it is expected to honour that and otherwise stay silent and let
it re-arm. Same rule for a `repeat` poll whose command only works on one box:
name the host in the text.
