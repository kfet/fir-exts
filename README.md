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

### `quota-watch`

Makes the agent account for what it is spending. Nothing in a session tells an
agent how much provider budget is left, so it happily spends the last of it on a
wasteful plan and the user finds out when a request is refused.

- Tool: `quota_note` — the agent records a reading after checking usage.
  Command: `/quota` — show recorded readings and how they moved.
- **It checks nothing itself.** At `turn_end` it decides a check is due and
  prepends a `[SYS_EXT]` note asking the agent to look up its own budget and
  report back. Cadence and memory here; the lookup belongs to the agent.
- Store: `quota-watch-log.json` in the **global** config dir (a budget is
  account-wide, not per-project). Written atomically via `os.replace`, since
  several sessions share the file and a torn write reads back as invalid JSON
  and discards every reading. Concurrent read-append-write can still drop one
  note; accepted, as locking would cost more than it protects.
- **Numbers are never interpreted.** `_deltas` compares each key against the
  same key in the previous reading and reports per-hour movement, so it works
  for utilization percentages, points, dollars, or whatever a future provider
  reports. This is what makes "climbing too fast" answerable rather than
  guesswork — and why the nudge insists the agent pass `numbers`, not just a
  summary string.
- **Cost is the gate, not the clock.** An idle session consumes no budget and
  is never nudged; a busy one is worth interrupting. A slow ceiling on elapsed
  time (`everyMinutes`) still catches the cheap-but-long session.

Config — `quota-watch.json` in any host config dir (project-local `.fir/` wins);
all keys optional:

```json
{
  "off": false,
  "firstAfterTurns": 2,
  "minMinutes": 20,
  "everyDollars": 2.00,
  "everyMinutes": 90,
  "keepNotes": 20
}
```

**Why it does not poll (learned the hard way):** an earlier version hit
Anthropic's `oauth/usage` endpoint on a timer. Wrong twice over — usage
endpoints are themselves rate-limited, so a fixed short interval times every
concurrent session earns a 429 and a long backoff, leaving the watchdog blind
exactly when it matters; and it hardcoded one vendor's JSON shape, making every
other budget (Poe points, gateway budgets, plain API keys) invisible. Delegating
to the agent means new providers need no edit to this file.

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

### `imagegen`

Image generation as a tool, for text-only agent models. `generate_image(prompt)`
writes a PNG to `~/imagegen-out/` and returns the path; the agent delivers it via
the relay's attach directive (poe-acp `<!--poe-attach ... inline-->`).

- Tools: `generate_image`, `image_models`. Command: `/imagegen`.
- Providers: **OpenRouter** (default) and **Poe**, both via OpenAI-compatible
  `/chat/completions` with `modalities: ["image","text"]`.
- Credentials: `OPENROUTER_API_KEY` / `POE_API_KEY`, else the `openrouter` /
  `poe` slots in `~/.config/fir/auth.json`. Nothing new to provision if fir is
  already logged in to either.

**Why OpenRouter is the default — budget isolation, not price.** Poe and
OpenRouter charge the same for the nano-banana family ($0.00003 / $0.00012 per
image on both). But on a Poe-hosted relay, image spend and the bot's own
survival draw on one points pool: exhausting it takes the *control channel*
offline until the month rolls over, and images are the most points-dense thing
an agent does (~3000 pts ≈ $0.09 per flagship render). Draining OpenRouter only
costs you images. So **Poe is never selected implicitly** — it must be named per
call or pinned in config — and a Poe call preflights the points balance and
refuses below a floor (default 200k).

**Models are not pinned.** Each provider's `/models` is queried live (24h disk
cache) and filtered to what can genuinely emit images:

- OpenRouter — `architecture.output_modalities ∋ image` (~9 models)
- Poe — `supported_endpoints ∋ /v1/chat/completions` **and** `pricing.image` set

That second filter is the real asymmetry: most Poe image bots (flux-2-\*,
gpt-image-2, qwen-image-2, grok-imagine-image) expose *no* OpenAI-compatible
endpoint at all, so the usable Poe set is 2 models against OpenRouter's 9.

Ranking uses per-image price as a capability proxy — flagships cost more.
`quality: "best"` takes the priciest, `"fast"` the cheapest, newest breaks ties.
A new flagship is therefore adopted the day it launches, with no code change and
no fir release. Pinning is opt-in (`/imagegen model <id>`); if a pinned model
vanishes from the catalog the tool **fails loudly with the live list** rather
than substituting, because a different image model is a different product.

Editing works too: pass `image` with a path and the input is sent as an
`image_url` content part.

**Guardrails, in order of what they protect:** Poe opt-in only; Poe balance
preflight (protects the relay); provider/model/price echoed in every result
(spend lands in the transcript); a running per-session count warning — a
**nudge, not a cap**, since a hard stop mid-task is worse than an extra dollar.

**Switching, mobile-first.** `/imagegen provider openrouter|poe` writes
`~/.config/fir/imagegen.json`, inherited by every relay on the host; per-call
`provider`/`model` args cover one-offs ("draw X, use poe"). Deliberately **no
env-var configuration** — that is a shell-only path, useless to a user on a
phone, which is exactly who this is for.

**Sharp edges found the hard way:**
- `gemini-3-pro-image` returns *two* frames — an interim render and the refined
  final. Only the last is kept unless `all_variants: true`.
- Extension tool calls default to a 30s host timeout; image models routinely
  take 30-120s, so the tool declares `timeout=300` **and** beats a
  `ctx.report_progress` heartbeat every 5s during the HTTP wait. fir's
  tool_call deadline is activity-aware — any message from the extension resets
  it — but the host's own keepAlive covers only calls *it* drives (side_query,
  call_tool); an extension blocking in urllib looks dead. The heartbeat is
  therefore both the spinner text and the liveness signal, which turns the
  declared timeout into a floor on silence rather than a ceiling on the render.
- poecdn 403s a bare urllib User-Agent; Poe returns its image as a CDN link in
  markdown rather than as base64, so both a real UA and link-extraction are
  needed.

## Skills (no extension)

### `prestaff`

Renders beginner piano method-book pages — pre-staff finger-number notation
(noteheads and stems, no staff lines, finger numbers, lyrics, hand labels) and
conventional staff systems for the teacher duet — to SVG/PDF/PNG from a small
YAML notation.

- `prestaff.py song.yaml out.svg --png --pdf` — the renderer. Millimetre
  coordinates, page-sized viewBox, so PDFs print at true size.
- `measure.py heads|staff photo.jpg x0,y0,x1,y1 dbg.png` — reads notehead
  positions (and, for real staves, a per-column 5-line comb that tracks the
  curl of a photographed page) so a recreation can be *checked* against the
  original rather than eyeballed.

The notation encodes the rules the books actually follow, which were measured
rather than assumed: stem direction is the hand, the finger number is the
pitch, and **L.H. pitch falls as the finger number rises**.

Deps are ephemeral via `uv run --with pyyaml --with cairosvg` (plus
`pillow scipy numpy` for `measure.py`); nothing is installed globally.
