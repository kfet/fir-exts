# fleet — pull-model host convergence

Reconfiguring the fleet used to mean ssh-ing every host. A box that was offline
silently got nothing. This replaces that with a **pull model**: every host runs
`converge.sh` hourly, and reconfiguration is *one edit to one file*.

**Layering rule:** code & skills → git (this repo). Infra bindings and mutable
data → `~/sync/shared` (Syncthing). No infra paths hardcoded in code.

| thing | lives in | carried by |
|---|---|---|
| `converge.sh`, `bootstrap.sh`, unit files | **this repo** | `git pull` |
| `~/sync/shared/fleet/env` (the env values) | **`~/sync/shared`** | Syncthing |
| `~/sync/shared/fleet/status/<host>.status` | **`~/sync/shared`** | Syncthing |

The commit is the review gate. There is no canary machinery.

## Bootstrap a new host (once, ever)

```sh
curl -sSfL https://raw.githubusercontent.com/kfet/fir-exts/main/fleet/bootstrap.sh | sh
```

Idempotent — safe to re-run on an already-bootstrapped box. It ensures the
fir-exts package clone (adding `fleet/` to a sparse subdir install), installs and
starts the hourly timer (Linux) or LaunchAgent (macOS), and converges once.

## Reconfigure the fleet

Edit `~/sync/shared/fleet/env` on any host:

```sh
export FIR_REMINDERS_STORE="$HOME/sync/shared/reminders"
```

Syncthing carries it; each host applies it on its next run. A box that was off
for a week catches up on boot (`Persistent=true`, `OnBootSec=2min`).

To change *behaviour* rather than values, edit `converge.sh` here and push — the
next pull picks it up, and the script re-execs itself if it changed mid-run.

## What converge.sh does

1. `git pull --ff-only` the package clone; re-exec itself if the script changed.
2. Ensure `[ -f "$HOME/sync/shared/fleet/env" ] && . "$HOME/sync/shared/fleet/env"`
   appears exactly once in `~/.profile` and `~/.zshenv` (creating them if absent),
   and delete the legacy raw `export FIR_REMINDERS_STORE=...` lines it supersedes.
3. Render the env file into expanded `KEY=VALUE` pairs — `sh -c '. env; env'`
   diffed against an empty baseline, so only vars the file actually *sets* are
   emitted, with `$HOME` expanded. This dodges systemd's `EnvironmentFile` gotcha
   (no shell expansion, no `export`) and, because `~/.config/environment.d/`
   applies to **all** user units, **per-unit drop-ins are no longer needed** —
   converge removes the old `poe-acp-*.service.d/reminders-store.conf` files
   (and leaves every other drop-in, e.g. `graceful.conf`, alone).
   Only when the render actually changed: `daemon-reload` and restart the running
   `poe-acp*.service` units (discovered, never hardcoded).
4. Always write `~/sync/shared/fleet/status/<host>.status` (trap, so failures are
   recorded too): `<ISO-UTC> <host> <sha> ok|FAIL <reason>`.

A converged run prints nothing and touches nothing but the status file. A stale
timestamp means a straggler; `FAIL` is visible fleet-wide.

**macOS:** steps 1, 2 and 4 are identical. Step 3 has no systemd — the same
rendered set is applied with `launchctl setenv`, and the shell stanza in
`.zshenv` (the file that matters on macOS) covers interactive use. *Caveat:*
already-running LaunchAgents do not see `launchctl setenv` changes; restart them.

Harmless on a host with no `poe-acp` units — most of the fleet has none.

## Check the fleet

```sh
cat ~/sync/shared/fleet/status/*.status | sort -k2
```

## Tests

```sh
sh fleet/test_converge.sh
```

Runs entirely in a throwaway `$HOME` against a local git origin: idempotency,
`$HOME` expansion, exactly-once stanzas, legacy cleanup, drop-in cleanup
(including that `graceful.conf` survives), and status-on-failure.
