---
name: reminders
description: Set, surface, and manage durable reminders. Use when the user says "remind me…", "don't let me forget…", "in 20 minutes…", asks what reminders are outstanding, or when a `[reminders]` block appears in the conversation announcing due items.
---

# Reminders

Durable, file-backed reminders delivered by **lazy catch-up** — no daemon, no
timer. The clock does not fire a reminder; **waking up does**.

## Architecture (why it works this way)

- **Store:** `~/.local/state/poe-acp/notes/reminders.jsonl` — append-only op log
  (`add` / `done` / `snooze` / `delivered`). jsonl is truth, memory is cache.
- **Delivery:** the `reminders` extension sweeps on `agent_start` (turn already
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
| `reminder_add(text, due, scope)` | `due`: `45m`, `1h30m`, `2pm`, `14:00`, `tomorrow 9am`, ISO, epoch. `scope`: `here` (default, this conversation) or `any` (first live session that wakes). |
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

## Creating one

Prefer explicit scope. Default `here` keeps a reminder inside the conversation
that created it; `any` is for things that matter regardless of where the user
next shows up — and fires in the **first** session that wakes, exactly once.

Confirm back with the resolved absolute time, not just the relative one — "in
90m" is ambiguous to a user on a different clock; "2026-08-01 04:56 (in 1h 30m)"
is not.
