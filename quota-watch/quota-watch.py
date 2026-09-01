#!/usr/bin/env python3
# ---
# name: quota-watch
# description: Periodically ask the agent to check its own provider quota/budget and warn the user when it is climbing fast or nearing a limit
# ---
"""quota-watch — make the agent account for what it is spending.

Nothing in a session tells the agent how much of its provider budget is
left, so it happily spends the last of it on a wasteful plan and the user
finds out when a request is refused.

This extension deliberately does **not** check any quota itself. An
earlier version polled Anthropic's ``oauth/usage`` endpoint on a timer,
which was wrong twice over:

* **Polling gets you rate-blocked.** The usage endpoints are themselves
  rate-limited, and a fixed short interval (times every concurrent
  session) earns a 429 and a long backoff — so the watchdog goes blind
  exactly when it matters.
* **It hardcoded one provider.** Sessions run on Anthropic
  subscriptions, Poe points, a Bifrost budget, plain API keys, and
  whatever comes next. Baking one vendor's JSON shape into a watchdog
  means every other budget is invisible.

So the extension only handles *cadence and memory*, and delegates the
actual check to the agent — which knows what provider it is on, has
skills for reading those budgets, and adapts when a new one appears
without anyone editing this file.

How it works: at ``turn_end`` it decides whether a check is due (based on
elapsed time and accrued session cost — an idle session burns nothing and
is never nudged). When one is, it prepends a ``[SYS_EXT]`` note asking the
agent to look up its own quota and report back via the ``quota_note``
tool. Recorded notes are kept, so each nudge carries the previous reading
and a per-hour delta — which is what makes "climbing too fast" answerable
rather than guesswork. The numbers are whatever the agent chose to
record, so no provider schema is baked in here.

Config — ``quota-watch.json`` in any host config dir (project-local
``.fir/`` wins over ``~/.config/fir/``); all keys optional::

    {
      "off": false,
      "firstAfterTurns": 2,   // initial check this many turns into a session
      "minMinutes": 20,       // never nudge more often than this
      "everyDollars": 2.00,   // nudge once this much session cost accrues
      "everyMinutes": 90,     // ...or this long, whichever comes first
      "keepNotes": 20         // how many past readings to retain
    }
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import fir_ext

CONFIG_FILENAME = "quota-watch.json"
LOG_FILENAME = "quota-watch-log.json"

DEFAULTS = {
    "firstAfterTurns": 2,
    "minMinutes": 20,
    "everyDollars": 2.00,
    "everyMinutes": 90,
    "keepNotes": 20,
}

# Session state. A fresh process means a fresh session, so re-checking early
# is correct — the account-wide window may already be nearly spent by others.
_last_nudge_at = 0.0
_last_nudge_cost = 0.0
_nudged_once = False
# Our own turn tally, so a host that reports no message counts still gets
# checked rather than silently never being nudged.
_turns_seen = 0


# ---------------------------------------------------------------------------
# Config / storage
# ---------------------------------------------------------------------------


def _config() -> dict:
    with contextlib.suppress(Exception):
        return fir_ext.load_config(CONFIG_FILENAME) or {}
    return {}


def _num(cfg: dict, key: str) -> float:
    try:
        return float(cfg[key])
    except (KeyError, TypeError, ValueError):
        return float(DEFAULTS[key])


def _log_path() -> Path:
    """Global config dir — a budget is account-wide, not per-project."""
    base = Path(fir_ext.config_dirs[-1]) if fir_ext.config_dirs else Path.home() / ".config" / "fir"
    return base.expanduser() / LOG_FILENAME


def _read_notes() -> list[dict]:
    try:
        with open(_log_path()) as f:
            data = json.load(f)
        return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _write_notes(notes: list[dict]) -> None:
    """Replace the log atomically — several sessions share this file, and a
    torn write would read back as invalid JSON and discard every reading.

    Concurrent read-append-write can still drop one note if two sessions
    record in the same instant. That is accepted: losing a single reading is
    harmless, and locking would cost more than it protects.
    """
    path = _log_path()
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(notes, f, indent=1)
        os.replace(tmp_path, str(path))
        tmp_path = None
    except Exception:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_span(seconds: float) -> str:
    total_min = max(0, int(seconds // 60))
    if total_min < 60:
        return f"{total_min}m"
    if total_min < 1440:
        h, m = divmod(total_min, 60)
        return f"{h}h{m:02d}m"
    d, rem = divmod(total_min, 1440)
    return f"{d}d{rem // 60}h"


def _describe(note: dict, now: float) -> str:
    """One recorded reading, with its age."""
    age = _fmt_span(now - float(note.get("at") or now))
    who = f" [{note['provider']}]" if note.get("provider") else ""
    parts = [f"{age} ago{who}: {note.get('summary') or '(no summary)'}"]
    if note.get("headroom"):
        parts.append(f"agent's verdict: {note['headroom']}")
    if note.get("told_user"):
        parts.append("user was warned")
    return " · ".join(parts)


def _deltas(notes: list[dict]) -> list[str]:
    """Per-hour movement in whatever numbers the agent chose to record.

    Keys are never interpreted — only compared against the same key in the
    previous reading — so this works for utilization percentages, points,
    dollars, or anything a future provider reports.
    """
    numeric = [n for n in notes if isinstance(n.get("numbers"), dict) and n.get("at")]
    if len(numeric) < 2:
        return []
    prev, last = numeric[-2], numeric[-1]
    hours = (float(last["at"]) - float(prev["at"])) / 3600.0
    if hours <= 0:
        return []

    out = []
    for key, val in last["numbers"].items():
        old = prev["numbers"].get(key)
        if not isinstance(val, (int, float)) or not isinstance(old, (int, float)):
            continue
        rate = (val - old) / hours
        arrow = "+" if rate > 0 else ""
        out.append(f"{key}: {old:g} → {val:g} ({arrow}{rate:.1f}/h over {_fmt_span(hours * 3600)})")
    return out


# ---------------------------------------------------------------------------
# The nudge
# ---------------------------------------------------------------------------

NUDGE = """QUOTA CHECK DUE — you have spent ${cost:.2f} this session{since} and \
your remaining provider budget is currently unknown to you.

ACTION, before continuing with anything expensive:

1. Work out which budget actually applies to you right now. You are on \
provider `{provider}`, model `{model}`. That may be a subscription with \
rolling rate-limit windows, a prepaid point balance, a gateway budget, a \
metered API key, or something else — and there may be more than one worth \
checking. Decide for yourself; do not assume.
2. Check it using whatever skill or tool fits that provider. Check ONCE. \
Usage endpoints are themselves rate-limited, so repeated polling earns a \
long backoff and leaves you blind — if a check fails or is throttled, note \
that and move on rather than retrying in a loop.
3. Record what you found with `quota_note` — and include `numbers` (e.g. \
`{{"5h_pct": 18, "7d_pct": 5}}`, or points, or dollars). A summary string \
alone cannot be compared against the next reading, so without numbers no \
trend is ever computable and "climbing too fast" stays unanswerable. Reuse \
the same key names each time.
4. If anything is near a limit, or climbing fast enough to run out before it \
resets, TELL THE USER in your next reply — plainly, with the numbers, and \
with real options (cheaper model, less context, batching, or stopping until \
the window resets). If it is all healthy, say nothing beyond the record.
{history}"""


def _nudge_text(info: dict, cost: float, now: float) -> str:
    model = (info.get("model") or {}) if isinstance(info, dict) else {}
    notes = _read_notes()

    if notes:
        recent = "\n".join(f"  - {_describe(n, now)}" for n in notes[-3:])
        history = f"\nPreviously recorded, for comparison:\n{recent}"
        if deltas := _deltas(notes):
            listed = "\n".join(f"  - {d}" for d in deltas)
            history += f"\nMovement since the previous reading:\n{listed}"
    else:
        history = "\nNo previous reading on record — this is the first check."

    since = ""
    if _last_nudge_at:
        spent = cost - _last_nudge_cost
        since = f" (${spent:.2f} of it since the last check, {_fmt_span(now - _last_nudge_at)} ago)"

    return NUDGE.format(
        cost=cost,
        since=since,
        provider=model.get("provider") or "unknown",
        model=model.get("id") or "unknown",
        history=history,
    )


def _due(cfg: dict, turns: int, cost: float, now: float) -> bool:
    """Whether a check is due.

    Cost is the gate rather than the clock: an idle session consumes no
    budget and has nothing to report, while a busy one is worth interrupting.
    A slow ceiling on elapsed time still catches the cheap-but-long session.
    """
    if not _nudged_once:
        return turns >= _num(cfg, "firstAfterTurns")

    if now - _last_nudge_at < _num(cfg, "minMinutes") * 60:
        return False
    if cost - _last_nudge_cost >= _num(cfg, "everyDollars"):
        return True
    return now - _last_nudge_at >= _num(cfg, "everyMinutes") * 60


def _maybe_nudge(ctx: fir_ext.Context) -> None:
    global _last_nudge_at, _last_nudge_cost, _nudged_once, _turns_seen

    cfg = _config()
    if cfg.get("off"):
        return

    _turns_seen += 1

    info = ctx.agent_info() or {}
    cost = float(info.get("cost") or 0.0)
    turns = max(int(((info.get("messages") or {}).get("assistant")) or 0), _turns_seen)
    now = time.time()

    if not _due(cfg, turns, cost, now):
        return

    ctx.prepend(_nudge_text(info, cost, now))
    _last_nudge_at = now
    _last_nudge_cost = cost
    _nudged_once = True


# ---------------------------------------------------------------------------
# Tool / command
# ---------------------------------------------------------------------------


@fir_ext.tool(
    name="quota_note",
    description=(
        "Record what you found when you checked your provider quota or budget, "
        "so later checks have something to compare against. Call this after "
        "checking usage — whatever the provider and whatever the units. "
        "Returns the previous reading and the movement since it, if any."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Short human-readable state, including reset times if the "
                    "provider has them, e.g. '7d 74% (resets 4h13m), 5h 22%' or "
                    "'Poe 1.2M points left' or 'Bifrost $41 of $200 this month'."
                ),
            },
            "provider": {
                "type": "string",
                "description": "Which budget this reading is for, e.g. 'anthropic-sub', 'poe', 'bifrost'.",
            },
            "headroom": {
                "type": "string",
                "enum": ["ok", "tight", "critical", "unknown"],
                "description": "Your own verdict on how much room is left.",
            },
            "numbers": {
                "type": "object",
                "description": (
                    "Optional metric->number map for trend tracking, e.g. "
                    "{'7d_pct': 74, '5h_pct': 22} or {'points': 1200000}. Reuse the "
                    "same key names on later checks so movement can be computed."
                ),
                "additionalProperties": {"type": "number"},
            },
            "told_user": {
                "type": "boolean",
                "description": "Whether you warned the user about this reading.",
            },
        },
        "required": ["summary"],
    },
)
def quota_note(params, ctx):
    summary = (params or {}).get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise fir_ext.ToolError("summary is required")

    now = time.time()
    notes = _read_notes()
    previous = notes[-1] if notes else None

    note = {"at": now, "iso": datetime.now().astimezone().isoformat(), "summary": summary.strip()}
    for key in ("provider", "headroom"):
        if isinstance((params or {}).get(key), str):
            note[key] = params[key]
    if isinstance((params or {}).get("told_user"), bool):
        note["told_user"] = params["told_user"]
    numbers = (params or {}).get("numbers")
    if isinstance(numbers, dict):
        clean = {k: v for k, v in numbers.items() if isinstance(v, (int, float))}
        if clean:
            note["numbers"] = clean

    notes.append(note)
    _write_notes(notes[-int(_num(_config(), "keepNotes")) :])

    lines = ["Recorded."]
    if previous:
        lines.append(f"Previous — {_describe(previous, now)}")
    if deltas := _deltas(notes):
        lines.append("Movement: " + "; ".join(deltas))
    else:
        lines.append("No comparable numbers yet — record `numbers` again next time for a trend.")
    return {"content": [{"type": "text", "text": "\n".join(lines)}], "is_error": False}


@fir_ext.command(
    name="quota",
    description="Show recorded quota/budget readings and how they have moved.",
)
def cmd_quota(args: list, ctx: fir_ext.Context) -> dict:
    notes = _read_notes()
    if not notes:
        return {"message": "No quota readings recorded yet.", "print_response": False}

    now = time.time()
    lines = [_describe(n, now) for n in notes[-10:]]
    if deltas := _deltas(notes):
        lines += ["", "Movement since previous reading:"] + [f"  {d}" for d in deltas]
    return {"message": "\n".join(lines), "print_response": False}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@fir_ext.on("turn_end")
def on_turn_end(params: dict, ctx: fir_ext.Context) -> None:
    """Decide whether a check is due. Failures never break the session."""
    with contextlib.suppress(Exception):
        _maybe_nudge(ctx)


fir_ext.run(name="quota-watch")
