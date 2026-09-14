#!/bin/sh
# Offline tests for converge.sh. No fleet, no network — a throwaway $HOME and a
# local git origin. Run: sh fleet/test_converge.sh
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
check() { # name cond-desc
  if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1  $3"; fails=$((fails+1)); fi
}

# a local origin holding fleet/, and a clone standing in for the fir package dir
ORIGIN="$T/origin"; PKG="$T/pkg"; H="$T/home"
mkdir -p "$ORIGIN" "$H"
git init -q "$ORIGIN"; mkdir -p "$ORIGIN/fleet"; cp "$HERE"/*.sh "$HERE"/*.service "$HERE"/*.timer "$ORIGIN/fleet/"
git -C "$ORIGIN" add -A
git -C "$ORIGIN" -c user.email=t@t -c user.name=t commit -qm init
git clone -q "$ORIGIN" "$PKG"

mkdir -p "$H/sync/shared/fleet"
cat >"$H/sync/shared/fleet/env" <<'EOF'
# test env
export FIR_REMINDERS_STORE="$HOME/sync/shared/reminders"
EOF

run() { HOME="$H" XDG_CONFIG_HOME="$H/.config" FIR_FLEET_PKG_DIR="$PKG" \
        FIR_FLEET_DIR="$H/sync/shared/fleet" FIR_CONVERGE_REEXEC=1 \
        sh "$PKG/fleet/converge.sh" 2>&1; }

out1=$(run); rc1=$?
check "first run succeeds" "$rc1" "$out1"

R="$H/.config/environment.d/50-fleet.conf"
[ -f "$R" ]; check "renders environment.d file" $? "$R missing"
grep -q "^FIR_REMINDERS_STORE=$H/sync/shared/reminders$" "$R"
check "render has \$HOME EXPANDED" $? "$(cat "$R" 2>/dev/null)"
grep -q '\$HOME' "$R"; [ $? -ne 0 ]; check "render contains no literal \$HOME" $?
[ "$(grep -c '=' "$R")" = 1 ]; check "render emits only vars the env file sets" $? "$(cat "$R")"

out2=$(run); rc2=$?
check "second run succeeds" "$rc2" "$out2"
[ -z "$out2" ]; check "second run is a silent no-op" $? "got: $out2"

run >/dev/null 2>&1
for rc in .profile .zshenv; do
  n=$(grep -c 'fleet/env' "$H/$rc")
  [ "$n" = 1 ]; check "stanza in $rc exactly once after 3 runs" $? "count=$n"
done

S="$H/sync/shared/fleet/status/$(hostname -s 2>/dev/null || hostname).status"
[ -f "$S" ]; check "status file written" $?
grep -q ' ok$' "$S"; check "status says ok" $? "$(cat "$S" 2>/dev/null)"
grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z ' "$S"; check "status starts with ISO-UTC timestamp" $? "$(cat "$S")"

# legacy cleanup: the raw export we used to append is removed, neighbours survive
printf 'export FIR_REMINDERS_STORE="$HOME/sync/shared/reminders"\nexport KEEP_ME=1\n' >>"$H/.profile"
run >/dev/null 2>&1
grep -q 'export FIR_REMINDERS_STORE' "$H/.profile"; [ $? -ne 0 ]; check "legacy export line removed" $?
grep -q 'export KEEP_ME=1' "$H/.profile"; check "unrelated rc lines survive" $?

# superseded drop-in removed, unrelated drop-in survives
D="$H/.config/systemd/user/poe-acp-test.service.d"
mkdir -p "$D"; : >"$D/reminders-store.conf"; : >"$D/graceful.conf"
run >/dev/null 2>&1
[ ! -f "$D/reminders-store.conf" ]; check "superseded drop-in removed" $?
[ -f "$D/graceful.conf" ]; check "unrelated drop-in (graceful.conf) survives" $?

# concurrent `git fetch --tags origin` — exactly what fir runs on this same clone
# at session start — must not break converge. `git pull` reads FETCH_HEAD, which
# git rewrites non-atomically, and failed ~60% of the time under this contention
# ("Cannot fast-forward to multiple branches" / "no such ref was fetched").
( i=0; while [ $i -lt 200 ]; do git -C "$PKG" fetch --tags origin >/dev/null 2>&1; i=$((i+1)); done ) &
racer=$!
races=0; raced_fail=0
while [ $races -lt 8 ] && kill -0 "$racer" 2>/dev/null; do
  out=$(run) || { raced_fail=$((raced_fail+1)); echo "  race failure: $(echo "$out" | head -1)"; }
  races=$((races+1))
done
kill "$racer" 2>/dev/null; wait "$racer" 2>/dev/null
[ "$raced_fail" = 0 ]; check "survives a concurrent git fetch on the same clone" $? "$raced_fail/$races runs failed"

# and it must not depend on FETCH_HEAD at all: poison it with the exact duplicate
# entries that produced "Cannot fast-forward to multiple branches" in the field
dup=$(git -C "$PKG" rev-parse HEAD)
printf '%s\t\tbranch '\''main'\'' of origin\n%s\t\tbranch '\''main'\'' of origin\n' "$dup" "$dup" \
  >"$PKG/.git/FETCH_HEAD"
out=$(run); check "ignores a poisoned FETCH_HEAD" $? "$out"

# --- package registration (step 2b) -------------------------------------
# The bug this guards: converge pulls the clone but nothing registers it, so
# extensions and skills stay inert. Must also work with NO settings.json.
FS="$H/.config/fir/settings.json"
[ -f "$FS" ]; check "creates settings.json when absent" $? "$FS missing"
python3 -c "
import json,sys
p=json.load(open('$FS')).get('packages',[])
sys.exit(0 if any('github.com/kfet/fir-exts' in str(x) for x in p) else 1)"
check "registers fir-exts when settings.json was absent" $?

# idempotent: three more runs must not duplicate the entry
run >/dev/null 2>&1; run >/dev/null 2>&1; run >/dev/null 2>&1
n=$(python3 -c "
import json
p=json.load(open('$FS')).get('packages',[])
print(sum(1 for x in p if 'github.com/kfet/fir-exts' in str(x)))")
[ "$n" = 1 ]; check "registration is idempotent across runs" $? "found $n entries"

# must PRESERVE unrelated settings rather than rewriting the file wholesale
python3 -c "
import json
p='$FS'; d=json.load(open(p)); d['theme']='mytheme'
d['skills']=['/some/local/skill']; json.dump(d,open(p,'w'),indent=2)"
run >/dev/null 2>&1
python3 -c "
import json,sys
d=json.load(open('$FS'))
sys.exit(0 if d.get('theme')=='mytheme' and d.get('skills')==['/some/local/skill'] else 1)"
check "preserves unrelated settings keys" $?

# a corrupt settings.json must be left ALONE, not clobbered — and not fail the run
cp "$FS" "$T/settings.good"
printf 'this is not json{{{' >"$FS"
out=$(run); rc=$?
[ "$rc" = 0 ]; check "survives an unparseable settings.json" $? "rc=$rc out=$out"
grep -q 'not json{{{' "$FS"; check "does not clobber an unparseable settings.json" $? "$(cat "$FS")"
cp "$T/settings.good" "$FS"

# an existing registration recorded as an OBJECT (not a bare string) counts
python3 -c "
import json
p='$FS'; d=json.load(open(p))
d['packages']=[{'source':'github.com/kfet/fir-exts','scope':'user'}]
json.dump(d,open(p,'w'),indent=2)"
run >/dev/null 2>&1
n=$(python3 -c "
import json
p=json.load(open('$FS')).get('packages',[])
print(sum(1 for x in p if 'github.com/kfet/fir-exts' in str(x)))")
[ "$n" = 1 ]; check "recognises an object-form registration" $? "found $n entries"

# induced failure: unreachable git origin => FAIL status, nonzero exit
git -C "$PKG" remote set-url origin "$T/nope"
out=$(run); rc=$?
[ "$rc" -ne 0 ]; check "unreachable git fails loudly" $? "rc=$rc"
grep -q 'FAIL' "$S"; check "status file written on failure" $? "$(cat "$S")"

echo
if [ "$fails" = 0 ]; then echo "all tests passed"; else echo "$fails test(s) failed"; fi
exit $([ "$fails" = 0 ] && echo 0 || echo 1)
