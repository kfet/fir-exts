#!/usr/bin/env python3
# ---
# name: reminders
# description: Durable reminders with lazy catch-up delivery (no daemon).
# modes: tui, acp
# ---
"""Reminders — jsonl is truth, memory is cache.

Design (see conversation notes):
  * store: $FIR_REMINDERS_STORE, else legacy poe-acp path if present, else
    $XDG_STATE_HOME/fir-reminders/reminders.jsonl — append-only ops log
  * sweep: on session_start (log only) and turn_start (steer into live turn);
    agent_start is kept for other modes but never fires under ACP
  * NO in-process timer: conv sessions are evicted (--session-ttl), so a
    sleeping thread cannot be trusted. Waking up is the trigger.
  * fires LATE and says so — never silently drops an elapsed reminder
    (this is the deliberate inversion of builtin schedule.py semantics).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

import fir_ext

def _default_store() -> str:
    """Store path. Host-agnostic: XDG state dir, overridable by env.

    Legacy poe-acp path is honoured if it already exists, so an in-place
    upgrade never loses pending reminders.
    """
    env = os.environ.get("FIR_REMINDERS_STORE")
    if env:
        return os.path.expanduser(env)
    legacy = os.path.expanduser("~/.local/state/poe-acp/notes/reminders.jsonl")
    if os.path.exists(legacy):
        return legacy
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "fir-reminders", "reminders.jsonl")


STORE = _default_store()

# re-nag interval once a reminder has been surfaced but not completed
RENAG_S = 900
# after this many deliveries, auto-snooze a day so it stops being noise
MAX_DELIVERIES = 5

_state: dict[str, Any] = {"mtime": -1.0, "recs": {}, "next_due": float("inf")}


# ---------------------------------------------------------------- store


def _append(op: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    op.setdefault("at", int(time.time()))
    line = json.dumps(op, separators=(",", ":")) + "\n"
    fd = os.open(STORE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    _state["mtime"] = -1.0  # force reload


def _load(force: bool = False) -> dict:
    """Reduce the op log into current records. Cheap: stat + compare."""
    try:
        mtime = os.stat(STORE).st_mtime
    except FileNotFoundError:
        _state["recs"] = {}
        _state["next_due"] = float("inf")
        _state["mtime"] = -1.0
        return {}
    if not force and mtime == _state["mtime"]:
        return _state["recs"]

    recs: dict[str, dict] = {}
    with open(STORE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                op = json.loads(line)
            except Exception:
                continue
            rid = op.get("id")
            if not rid:
                continue
            kind = op.get("op")
            if kind == "add":
                recs[rid] = {
                    "id": rid,
                    "text": op.get("text", ""),
                    "due": float(op.get("due", 0)),
                    "scope": op.get("scope", "any"),
                    "created": op.get("at", 0),
                    "status": "pending",
                    "deliveries": 0,
                }
            elif rid in recs:
                r = recs[rid]
                if kind == "done":
                    r["status"] = "done"
                elif kind == "snooze":
                    r["due"] = float(op.get("due", r["due"]))
                    r["status"] = "pending"
                elif kind == "delivered":
                    r["deliveries"] += 1
                    r["due"] = float(op.get("next_due", r["due"]))

    _state["recs"] = recs
    _state["mtime"] = mtime
    pend = [r["due"] for r in recs.values() if r["status"] == "pending"]
    _state["next_due"] = min(pend) if pend else float("inf")
    return recs


# ---------------------------------------------------------------- time


_REL = re.compile(r"^\s*(?:in\s+)?((?:\d+[dhms])+)\s*$", re.I)
_PART = re.compile(r"(\d+)([dhms])", re.I)
_CLOCK = re.compile(
    r"^\s*(?:(today|tomorrow|tmr)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I
)
_MULT = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_due(s: str, now: float | None = None) -> float:
    """Parse '45m', '1h30m', '2pm', '14:00', 'tomorrow 9am', epoch, ISO."""
    now = now if now is not None else time.time()
    s = (s or "").strip()
    if not s:
        raise ValueError("empty due")

    if re.fullmatch(r"\d{9,}", s):  # epoch seconds
        return float(s)

    m = _REL.match(s)
    if m:
        total = sum(int(n) * _MULT[u.lower()] for n, u in _PART.findall(m.group(1)))
        if total <= 0:
            raise ValueError(f"bad relative time: {s}")
        return now + total

    m = _CLOCK.match(s)
    if m:
        day, hh, mm, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
        if ampm:
            ampm = ampm.lower()
            if ampm == "pm" and hh != 12:
                hh += 12
            elif ampm == "am" and hh == 12:
                hh = 0
        if hh > 23 or mm > 59:
            raise ValueError(f"bad clock time: {s}")
        lt = time.localtime(now)
        target = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1)
        )
        if day and day.lower() in ("tomorrow", "tmr"):
            target += 86400
        elif not day and target <= now:
            target += 86400  # next occurrence
        return target

    try:  # ISO-ish fallback
        import datetime

        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        pass
    raise ValueError(f"cannot parse due time: {s!r} (try '45m', '2pm', '14:00')")


def _ago(delta: float) -> str:
    delta = int(abs(delta))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h {(delta % 3600) // 60}m"
    return f"{delta // 86400}d {(delta % 86400) // 3600}h"


_scope_cache: dict[str, str] = {}


def _scope_here(ctx=None) -> str:
    """Conversation scope.

    The extension process cwd is the *agent* dir (shared across convs), NOT
    the conv dir — so cwd is useless for scoping. The session transcript path
    encodes the session cwd:
      ~/.config/fir/sessions/--home-...-convs-c-<id>--/<ts>_<sid>.jsonl
    Parse the conv id out of that. Falls back to cwd when unavailable.
    """
    if ctx is not None:
        try:
            sfile = ctx.get_session_file() or ""
        except Exception:
            sfile = ""
        if sfile:
            if sfile in _scope_cache:
                return _scope_cache[sfile]
            slug = os.path.basename(os.path.dirname(sfile))
            m = re.search(r"(c-[0-9a-z]{8,})", slug)
            scope = f"conv:{m.group(1)}" if m else f"dir:{slug.strip('-')}"
            _scope_cache[sfile] = scope
            return scope
    base = os.path.basename(os.getcwd().rstrip("/"))
    return f"conv:{base}" if base.startswith("c-") else f"cwd:{base}"


def _matches(rec: dict, here: str) -> bool:
    sc = rec.get("scope", "any")
    return sc == "any" or sc == here


# ---------------------------------------------------------------- sweep


def _sweep(ctx, deliver: bool) -> list[dict]:
    """Return due reminders; when deliver, tombstone-then-inject."""
    now = time.time()
    if _state["mtime"] >= 0 and now < _state["next_due"]:
        try:
            if os.stat(STORE).st_mtime == _state["mtime"]:
                return []
        except FileNotFoundError:
            return []
    recs = _load()
    here = _scope_here(ctx)
    due = [
        r
        for r in recs.values()
        if r["status"] == "pending" and r["due"] <= now and _matches(r, here)
    ]
    if not due or not deliver:
        return due

    lines = []
    for r in sorted(due, key=lambda x: x["due"]):
        late = now - r["due"]
        n = r["deliveries"] + 1
        if n >= MAX_DELIVERIES:
            _append({"op": "snooze", "id": r["id"], "due": int(now + 86400)})
            lines.append(
                f"- [{r['id']}] {r['text']} (overdue {_ago(late)}; "
                f"surfaced {n}x — auto-snoozed 24h, close it with reminder_done)"
            )
            continue
        # tombstone BEFORE surfacing: a crash mid-inject must not re-nag forever
        _append(
            {"op": "delivered", "id": r["id"], "next_due": int(now + RENAG_S)}
        )
        when = "due now" if late < 60 else f"overdue by {_ago(late)}"
        lines.append(f"- [{r['id']}] {r['text']}  ({when})")

    body = (
        "[reminders] "
        + f"{len(lines)} reminder(s) due:\n"
        + "\n".join(lines)
        + "\nSurface these to the user in your reply. "
        + "Use reminder_done(id) when handled, reminder_snooze(id, '1h') to defer."
    )
    try:
        ctx.send_user_message(body, deliver_as="steer")
    except Exception:
        try:
            ctx.send_message("reminder_due", body, display=True)
        except Exception:
            pass
    return due


@fir_ext.on("session_start")
def _on_start(params, ctx):
    _load(force=True)


# NOTE (verified 2026-08-01, remdebug probe): `agent_start` NEVER fires in
# ACP mode on this build — only turn_start / turn_end / message_end /
# session_end do. The original agent_start-only hook was dead code, which is
# why the sweep never delivered. turn_start fires before every model call
# within a turn, so the steer lands mid-turn (proven).
_last_sweep = {"t": 0.0}


def _hook_sweep(params, ctx):
    now = time.time()
    if now - _last_sweep["t"] < 3.0:  # several turn_starts per turn
        return
    _last_sweep["t"] = now
    try:
        _sweep(ctx, deliver=True)
    except Exception:
        pass


fir_ext.on("turn_start")(_hook_sweep)
fir_ext.on("agent_start")(_hook_sweep)


# ---------------------------------------------------------------- tools


@fir_ext.tool(
    "reminder_add",
    "Create a durable reminder. Fires on the next turn at/after its due time "
    "(late delivery is reported as overdue, never dropped).",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind about."},
            "due": {
                "type": "string",
                "description": "When: '45m', '1h30m', '2pm', '14:00', "
                "'tomorrow 9am', ISO datetime, or epoch seconds.",
            },
            "scope": {
                "type": "string",
                "description": "'any' (DEFAULT: fires in the first live "
                "session that wakes at/after the due time, wherever the user "
                "shows up) or 'here' (this conversation ONLY). Use 'here' "
                "only when the reminder is meaningless outside this thread "
                "AND you expect to still be in it. A 'here' reminder is "
                "silently stranded forever if the conversation is deleted or "
                "simply not revisited by the due time.",
            },
        },
        "required": ["text", "due"],
    },
)
def reminder_add(args, ctx):
    due = parse_due(str(args["due"]))
    scope = str(args.get("scope") or "any")
    scope = _scope_here(ctx) if scope in ("here", "conv") else "any"
    rid = "r" + uuid.uuid4().hex[:6]
    _append(
        {
            "op": "add",
            "id": rid,
            "text": str(args["text"]),
            "due": int(due),
            "scope": scope,
        }
    )
    _load(force=True)
    return (
        f"Reminder [{rid}] set for {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))} "
        f"(in {_ago(due - time.time())}), scope={scope}: {args['text']}"
    )


@fir_ext.tool(
    "reminder_list",
    "List reminders. Shows pending ones (due and upcoming) for this "
    "conversation plus any global ones.",
    {
        "type": "object",
        "properties": {
            "all": {
                "type": "boolean",
                "description": "Include done reminders and other scopes.",
            }
        },
    },
)
def reminder_list(args, ctx):
    recs = _load(force=True)
    here = _scope_here(ctx)
    show_all = bool(args.get("all"))
    now = time.time()
    rows = [
        r
        for r in recs.values()
        if show_all or (r["status"] == "pending" and _matches(r, here))
    ]
    if not rows:
        return "No reminders." + ("" if show_all else " (use all=true for done/other scopes)")
    out = []
    for r in sorted(rows, key=lambda x: x["due"]):
        rel = r["due"] - now
        when = f"in {_ago(rel)}" if rel > 0 else f"OVERDUE {_ago(rel)}"
        out.append(
            f"[{r['id']}] {when} — {r['text']}  "
            f"({r['status']}, scope={r['scope']}, surfaced {r['deliveries']}x)"
        )
    return "\n".join(out)


@fir_ext.tool(
    "reminder_done",
    "Mark a reminder handled so it stops surfacing.",
    {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Reminder id."}},
        "required": ["id"],
    },
)
def reminder_done(args, ctx):
    rid = str(args["id"]).strip().strip("[]")
    if rid not in _load(force=True):
        return f"No such reminder: {rid}"
    _append({"op": "done", "id": rid})
    _load(force=True)
    return f"Reminder [{rid}] closed."


@fir_ext.tool(
    "reminder_snooze",
    "Defer a reminder to a later time.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Reminder id."},
            "for": {
                "type": "string",
                "description": "Delay or clock time: '1h', '30m', '9am'.",
            },
        },
        "required": ["id", "for"],
    },
)
def reminder_snooze(args, ctx):
    rid = str(args["id"]).strip().strip("[]")
    if rid not in _load(force=True):
        return f"No such reminder: {rid}"
    due = parse_due(str(args["for"]))
    _append({"op": "snooze", "id": rid, "due": int(due)})
    _load(force=True)
    return (
        f"Reminder [{rid}] snoozed until "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(due))} (in {_ago(due - time.time())})."
    )


fir_ext.run()
