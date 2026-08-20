#!/usr/bin/env python3
# ---
# name: reminders
# description: Durable reminders with lazy catch-up delivery (no daemon).
# modes: tui, text, acp
# ---
"""Reminders — jsonl is truth, memory is cache.

Design (see conversation notes):
  * store: a DIRECTORY of per-host shards (sharded writes, shared reads).
    $FIR_REMINDERS_STORE, else $XDG_STATE_HOME/fir-reminders/. This host
    appends ONLY to its own shard <store>/<shard_id>.jsonl; reads glob every
    *.jsonl and reduce the union. No two hosts write the same file, so a
    file-syncing tool cannot produce a write conflict on the store.
  * fleet-wide sharing is DEPLOYMENT CONFIG, not something this extension
    knows about: point $FIR_REMINDERS_STORE at a directory that is synced
    between your machines (a Syncthing/Dropbox folder, a shared mount, …) and
    every host's shard lands in the same place. The extension itself hardcodes
    no infrastructure paths and probes for none.
  * sweep: on session_start (log only) and turn_start (steer into live turn);
    agent_start is kept for other modes but never fires under ACP
  * NO in-process timer: conv sessions are evicted (--session-ttl), so a
    sleeping thread cannot be trusted. Waking up is the trigger.
  * fires LATE and says so — never silently drops an elapsed reminder
    (this is the deliberate inversion of builtin schedule.py semantics).
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import shutil
import socket
import time
import uuid
from typing import Any

import fir_ext

# ---------------------------------------------------------------- layout

# Name of the pre-shard single-file store, looked for inside the XDG state
# dir when seeding a new shard. Not a path to anyone's infrastructure.
LEGACY_BASENAME = "reminders.jsonl"


def _xdg_state() -> str:
    return os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")


def _xdg_store_dir() -> str:
    return os.path.join(_xdg_state(), "fir-reminders")


def _resolve_store() -> tuple[str, str]:
    """Resolve the store. Returns (mode, path); mode is 'dir' or 'file'.

    Order — generic only. This extension hardcodes no infrastructure paths
    and probes no well-known directories:
      1. $FIR_REMINDERS_STORE — a directory, unless it names an existing file
         or ends in .jsonl (legacy single-file mode, kept working).
      2. $XDG_STATE_HOME/fir-reminders/ (a directory, not a file).

    To share reminders across machines, set $FIR_REMINDERS_STORE to a synced
    directory. That is deployment configuration, not a default.
    """
    env = os.environ.get("FIR_REMINDERS_STORE")
    if env:
        p = os.path.expanduser(env)
        if p.endswith(".jsonl") or os.path.isfile(p):
            return ("file", p)
        return ("dir", p)
    return ("dir", _xdg_store_dir())


def _shard_id() -> str:
    raw = os.environ.get("FIR_REMINDERS_SHARD") or socket.gethostname() or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw) or "unknown"


# Module globals, (re)computed by _init_store(). STORE is the ONE file this
# host ever appends to — its own shard in dir mode, the file itself in legacy
# single-file mode.
STORE_MODE = "dir"
STORE_DIR = ""
SHARD_ID = ""
STORE = ""


def _init_store() -> None:
    """(Re)resolve the store. Called at import; tests call it after setenv."""
    global STORE_MODE, STORE_DIR, SHARD_ID, STORE
    STORE_MODE, path = _resolve_store()
    SHARD_ID = _shard_id()
    if STORE_MODE == "file":
        STORE_DIR = os.path.dirname(path)
        STORE = path
    else:
        STORE_DIR = path
        STORE = os.path.join(STORE_DIR, SHARD_ID + ".jsonl")
    _state.update({"key": None, "recs": {}, "next_due": float("inf")})
    if STORE_MODE == "dir":
        try:
            _migrate_legacy()
        except Exception:
            pass


def _migrate_sources() -> list[str]:
    """Candidate legacy single-file stores to seed a new shard from.

    Generic and caller-driven:
      * $XDG_STATE_HOME/fir-reminders/reminders.jsonl — this extension's own
        pre-shard layout, so an in-place upgrade never loses pending items.
      * $FIR_REMINDERS_MIGRATE_FROM — optional colon-separated list of extra
        files, for anyone importing from somewhere else. Deployment supplies
        the paths; the extension knows none of them.
    """
    out = [os.path.join(_xdg_store_dir(), LEGACY_BASENAME)]
    extra = os.environ.get("FIR_REMINDERS_MIGRATE_FROM") or ""
    for part in extra.split(":"):
        part = part.strip()
        if part:
            out.append(os.path.expanduser(part))
    return out


def _migrate_legacy() -> None:
    """One-time: seed our shard from a pre-existing legacy single file.

    Only runs when our shard does not exist yet, and only for sources that
    live OUTSIDE the store dir (anything inside it is already globbed as a
    shard — copying that would double-count its ops). The source file is left
    in place, untouched.
    """
    if os.path.exists(STORE):
        return
    for src in _migrate_sources():
        if not os.path.isfile(src):
            continue
        if os.path.dirname(os.path.abspath(src)) == os.path.abspath(STORE_DIR):
            continue
        os.makedirs(STORE_DIR, exist_ok=True)
        tmp = STORE + ".migrating"
        shutil.copyfile(src, tmp)
        os.replace(tmp, STORE)  # atomic: either seeded or not, never partial
        return


# re-nag interval once a reminder has been surfaced but not completed
RENAG_S = 900
# after this many deliveries, auto-snooze a day so it stops being noise
MAX_DELIVERIES = 5

# "key" is the (path, mtime, size) fingerprint of every shard seen last sweep.
_state: dict[str, Any] = {"key": None, "recs": {}, "next_due": float("inf")}

_init_store()


# ---------------------------------------------------------------- store

# TODO(compaction): the op log is never compacted. If that ever changes, ONLY
# a shard's owning host may rewrite its own shard — rewriting someone else's
# shard is exactly the two-writers case Syncthing turns into a .sync-conflict.


def _shards() -> list[str]:
    """Every file we read. Sorted for a stable, deterministic tie-break.

    NOTE: filenames carry no meaning here. `.sync-conflict-*.jsonl` files may
    appear and are read like any other shard — never parsed, never special.
    """
    if STORE_MODE == "file":
        return [STORE] if os.path.exists(STORE) else []
    try:
        return sorted(_glob.glob(os.path.join(STORE_DIR, "*.jsonl")))
    except OSError:
        return []


def _scan_key() -> tuple:
    """Cache key over all shards: (path, mtime, size) each, re-globbed.

    Size is load-bearing: mtime has 1s granularity on many filesystems, so
    two appends within the same second are invisible to mtime alone.
    """
    out = []
    for p in _shards():
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((p, st.st_mtime, st.st_size))
    return tuple(out)


def _append(op: dict) -> None:
    d = os.path.dirname(STORE)
    if d:
        os.makedirs(d, exist_ok=True)
    op.setdefault("at", int(time.time()))
    line = json.dumps(op, separators=(",", ":")) + "\n"
    fd = os.open(STORE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    _state["key"] = None  # force reload


def _read_ops() -> list[tuple]:
    """Union of every shard's ops, globally ordered.

    Sort key is (at, shard filename, line number within that shard). The line
    number IS the per-shard sequence counter — free, monotonic, and needs no
    `seq` field on the wire. Tolerant: Syncthing ships partial file states, so
    a remote shard's final line can be torn mid-write. Malformed lines are
    skipped silently; a torn read must never raise.
    """
    ops: list[tuple] = []
    for path in _shards():
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        op = json.loads(line)
                    except Exception:
                        continue  # truncated / half-synced line
                    if not isinstance(op, dict) or not op.get("id"):
                        continue
                    try:
                        at = float(op.get("at", 0) or 0)
                    except Exception:
                        at = 0.0
                    ops.append((at, name, lineno, op))
        except OSError:
            continue
    ops.sort(key=lambda t: (t[0], t[1], t[2]))
    return ops


def _load(force: bool = False) -> dict:
    """Reduce the op log into current records. Cheap: stat + compare."""
    key = _scan_key()
    if not key:
        _state["recs"] = {}
        _state["next_due"] = float("inf")
        _state["key"] = None
        return {}
    if not force and key == _state["key"]:
        return _state["recs"]

    recs: dict[str, dict] = {}
    # `done` is ABSORBING: once seen for an id, later snooze/delivered ops for
    # that id are ignored whatever their timestamp. Two hosts with skewed
    # clocks would otherwise resurrect a closed reminder — a stale `delivered`
    # from host B sorting after host A's `done` would flip it back to pending.
    # A repeating reminder re-arms via `delivered`+reset while still pending,
    # so it is unaffected; `done` remains the only thing that ends a repeat.
    done_ids: set[str] = set()
    for _at, _name, _ln, op in _read_ops():
        rid = op["id"]
        kind = op.get("op")
        if kind == "add":
            recs[rid] = {
                "id": rid,
                "text": op.get("text", ""),
                "due": float(op.get("due", 0) or 0),
                "scope": op.get("scope", "any"),
                "repeat": float(op.get("repeat") or 0),
                "created": op.get("at", 0),
                "status": "done" if rid in done_ids else "pending",
                "deliveries": 0,
            }
        elif kind == "done":
            done_ids.add(rid)
            if rid in recs:
                recs[rid]["status"] = "done"
        elif rid in done_ids:
            continue  # absorbed
        elif rid in recs:
            r = recs[rid]
            if kind == "snooze":
                r["due"] = float(op.get("due", r["due"]) or 0)
                r["status"] = "pending"
                if not op.get("auto"):
                    # An explicit deferral is a fresh start, not another
                    # nag. Without this, N snoozes silently walk a
                    # reminder into MAX_DELIVERIES and its cadence
                    # degrades to 24h. The auto-snooze escape hatch is
                    # exempt so it stays terminal.
                    r["deliveries"] = 0
            elif kind == "delivered":
                if op.get("reset"):
                    r["deliveries"] = 0  # repeating: each cycle starts clean
                else:
                    r["deliveries"] += 1
                r["due"] = float(op.get("next_due", r["due"]) or 0)

    _state["recs"] = recs
    _state["key"] = key
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
    if _state["key"] is not None and now < _state["next_due"]:
        # Nothing is due by our own reckoning; only a changed shard set can
        # change that (another host may have added something due already).
        if _scan_key() == _state["key"]:
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
        rep = r.get("repeat") or 0
        n = r["deliveries"] + 1
        if rep:
            # Recurring / poll-until-true: re-arm one interval out and reset
            # the nag counter. It never auto-snoozes; only reminder_done ends
            # it. This is what a conditional check ("am I home yet?") needs.
            _append(
                {
                    "op": "delivered",
                    "id": r["id"],
                    "next_due": int(now + rep),
                    "reset": True,
                }
            )
            when = "due now" if late < 60 else f"overdue by {_ago(late)}"
            lines.append(
                f"- [{r['id']}] {r['text']}  ({when}; repeats every "
                f"{_ago(rep)} — call reminder_done({r['id']}) once it is "
                f"handled or its condition is met, otherwise do nothing "
                f"and it will ask again)"
            )
            continue
        if n >= MAX_DELIVERIES:
            _append({"op": "snooze", "id": r["id"], "due": int(now + 86400), "auto": True})
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
            "repeat": {
                "type": "string",
                "description": "Optional repeat interval ('8h', '1d', '30m'). "
                "The reminder re-arms itself this far out every time it "
                "fires and NEVER auto-expires — it stops only when you call "
                "reminder_done. Use it for recurring nudges, and for "
                "poll-until-true checks where the reminder text tells the "
                "future agent what to test (e.g. run a command; if the "
                "condition does not hold, say nothing and let it re-arm). "
                "Prefer this over done+re-add.",
            },
        },
        "required": ["text", "due"],
    },
)
def reminder_add(args, ctx):
    due = parse_due(str(args["due"]))
    scope = str(args.get("scope") or "any")
    scope = _scope_here(ctx) if scope in ("here", "conv") else "any"
    rep = 0
    if args.get("repeat"):
        raw = str(args["repeat"]).strip()
        m = _REL.match(raw)
        if not m:
            raise ValueError(
                f"repeat must be a relative interval like '8h', '1d30m' — got {raw!r}"
            )
        rep = sum(int(n) * _MULT[u.lower()] for n, u in _PART.findall(m.group(1)))
        if rep <= 0:
            raise ValueError(f"bad repeat interval: {raw!r}")
    rid = "r" + uuid.uuid4().hex[:6]
    _append(
        {
            "op": "add",
            "id": rid,
            "text": str(args["text"]),
            "due": int(due),
            "scope": scope,
            "repeat": rep,
        }
    )
    _load(force=True)
    return (
        f"Reminder [{rid}] set for {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))} "
        f"(in {_ago(due - time.time())}), scope={scope}"
        + (f", repeats every {_ago(rep)}" if rep else "")
        + f": {args['text']}"
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
            f"({r['status']}, scope={r['scope']}"
            + (f", repeats {_ago(r['repeat'])}" if r.get("repeat") else "")
            + f", surfaced {r['deliveries']}x)"
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
