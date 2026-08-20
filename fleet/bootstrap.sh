#!/bin/sh
# One-time per-host bootstrap for pull-model fleet convergence. Idempotent.
#   - ensure fir-exts is installed as a fir package (with fleet/ checked out)
#   - install + enable + start the hourly timer (Linux) or LaunchAgent (macOS)
#   - run converge.sh once
# curl-able on a brand-new box:
#   curl -sSfL https://raw.githubusercontent.com/kfet/fir-exts/main/fleet/bootstrap.sh | sh
set -eu

REPO="${FIR_FLEET_REPO:-https://github.com/kfet/fir-exts}"
PKG_DIR="${FIR_FLEET_PKG_DIR:-$HOME/.config/fir/packages/git/github.com/kfet/fir-exts}"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"

# 1. package clone. fir keys the clone on host/org/repo, so reuse it if present.
if [ -d "$PKG_DIR/.git" ]; then
  git -C "$PKG_DIR" pull --ff-only --quiet || true
else
  mkdir -p "$(dirname "$PKG_DIR")"
  git clone --quiet "$REPO" "$PKG_DIR"
fi
# a sparse (subdir-installed) clone will not have fleet/ — add it
if [ "$(git -C "$PKG_DIR" config --get core.sparseCheckout || echo false)" = true ]; then
  git -C "$PKG_DIR" sparse-checkout add /fleet/ 2>/dev/null \
    || printf '/fleet/\n' >>"$PKG_DIR/.git/info/sparse-checkout"
  git -C "$PKG_DIR" checkout --quiet .
fi
chmod +x "$PKG_DIR/fleet/converge.sh"

# 2. register the package with fir so its extensions/skills load (idempotent)
S="$CFG/fir/settings.json"
if [ -f "$S" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$S" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
pkgs = d.setdefault("packages", [])
def src(x): return x.get("source") if isinstance(x, dict) else x
if not any((src(x) or "").startswith("github.com/kfet/fir-exts") for x in pkgs):
    pkgs.append("github.com/kfet/fir-exts")
    json.dump(d, open(p, "w"), indent=2)
    print("registered github.com/kfet/fir-exts in settings.json")
PY
fi

# 3. timer / agent
if [ "$(uname -s)" = Darwin ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  sed "s|PKGDIR|$PKG_DIR|" "$PKG_DIR/fleet/io.kfet.fir-converge.plist" \
    >"$HOME/Library/LaunchAgents/io.kfet.fir-converge.plist"
  launchctl unload "$HOME/Library/LaunchAgents/io.kfet.fir-converge.plist" 2>/dev/null || true
  launchctl load "$HOME/Library/LaunchAgents/io.kfet.fir-converge.plist"
else
  mkdir -p "$CFG/systemd/user"
  cp "$PKG_DIR/fleet/fir-converge.service" "$PKG_DIR/fleet/fir-converge.timer" "$CFG/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable --now fir-converge.timer
  loginctl enable-linger "$(id -un)" 2>/dev/null || true
fi

# 4. converge now
FIR_CONVERGE_REEXEC=1 "$PKG_DIR/fleet/converge.sh"
echo "bootstrap complete on $(hostname -s 2>/dev/null || hostname)"
