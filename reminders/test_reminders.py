"""Offline tests for the reminders reducer + sweep. No fir runtime needed."""
import glob, importlib.util, json, os, shutil, sys, tempfile, time, types

# stub fir_ext so the module imports standalone
stub = types.ModuleType("fir_ext")
stub.on = lambda *a, **k: (lambda f: f)
stub.tool = lambda *a, **k: (lambda f: f)
stub.run = lambda *a, **k: None
sys.modules["fir_ext"] = stub

TMP = tempfile.mkdtemp()
os.environ["FIR_REMINDERS_STORE"] = os.path.join(TMP, "r.jsonl")
os.environ["FIR_REMINDERS_SHARD"] = "hostA"

spec = importlib.util.spec_from_file_location("rem", "reminders.py")
rem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rem)

class Ctx:
    def __init__(self): self.msgs = []
    def get_session_file(self): return ""
    def send_user_message(self, body, deliver_as=None): self.msgs.append(body)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(extra)))
    if not cond: fails.append(name)

def use_store(path, shard="hostA"):
    """Point the module at a store dir/file and re-resolve."""
    os.environ["FIR_REMINDERS_STORE"] = path
    os.environ["FIR_REMINDERS_SHARD"] = shard
    rem._init_store()

def reset():
    for p in rem._shards():
        os.remove(p)
    d = os.path.dirname(rem.STORE)
    if d:
        os.makedirs(d, exist_ok=True)
    open(rem.STORE, "w").close()
    rem._state.update({"key": None, "recs": {}, "next_due": float("inf")})

def write_shard(d, name, ops):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as fh:
        for op in ops:
            fh.write(json.dumps(op) + "\n")

# ======================================================================
# legacy single-file mode (back-compat) — the original suite, unchanged
# ======================================================================
use_store(os.path.join(TMP, "r.jsonl"))
check("env .jsonl path stays single-file mode", rem.STORE_MODE == "file")
check("single-file store is the env path", rem.STORE == os.path.join(TMP, "r.jsonl"))

# --- 1. explicit snooze resets the delivery counter -------------------
reset()
rem._append({"op": "add", "id": "r1", "text": "t", "due": int(time.time()) - 10})
for _ in range(3):
    rem._append({"op": "delivered", "id": "r1", "next_due": int(time.time()) - 5})
check("counter increments", rem._load(force=True)["r1"]["deliveries"] == 3)
rem._append({"op": "snooze", "id": "r1", "due": int(time.time()) + 60})
check("explicit snooze resets counter", rem._load(force=True)["r1"]["deliveries"] == 0)
rem._append({"op": "snooze", "id": "r1", "due": int(time.time()) + 60, "auto": True})
check("auto-snooze does NOT reset", rem._load(force=True)["r1"]["deliveries"] == 0)

# auto-snooze must stay terminal: counter at MAX survives it
reset()
rem._append({"op": "add", "id": "r2", "text": "t", "due": 1})
for _ in range(5):
    rem._append({"op": "delivered", "id": "r2", "next_due": 1})
rem._append({"op": "snooze", "id": "r2", "due": 1, "auto": True})
check("auto-snooze keeps count at cap", rem._load(force=True)["r2"]["deliveries"] == 5)

# --- 2. non-repeating hits the cap and auto-snoozes 24h ---------------
reset()
now = time.time()
rem._append({"op": "add", "id": "r3", "text": "plain", "due": int(now) - 10})
ctx = Ctx()
for i in range(6):
    # rewind due without touching the counter (auto-snooze is exempt),
    # standing in for wall-clock time passing between turns
    rem._append({"op": "snooze", "id": "r3", "due": int(time.time()) - 5, "auto": True})
    rem._state["key"] = None
    rem._sweep(ctx, deliver=True)
rec = rem._load(force=True)["r3"]
check("plain reminder auto-snoozes ~24h", rec["due"] - time.time() > 80000, rec)

# --- 3. repeating reminder re-arms forever, never auto-snoozes --------
reset()
now = time.time()
rem._append({"op": "add", "id": "r4", "text": "poll", "due": int(now) - 10, "repeat": 8 * 3600})
ctx = Ctx()
for i in range(12):
    recs = rem._load(force=True)
    rem._append({"op": "snooze", "id": "r4", "due": int(time.time()) - 5})
    rem._state["key"] = None
    rem._sweep(ctx, deliver=True)
rec = rem._load(force=True)["r4"]
check("repeat: counter stays 0", rec["deliveries"] == 0, rec)
check("repeat: still pending after 12 fires", rec["status"] == "pending")
gap = rec["due"] - time.time()
check("repeat: re-armed ~8h out, not 24h", 8 * 3600 - 120 < gap < 8 * 3600 + 120, gap)
check("repeat: fired every time", len(ctx.msgs) == 12, len(ctx.msgs))
check("repeat: tells agent how to stop", "reminder_done" in ctx.msgs[-1])
check("repeat: no auto-snooze wording", "auto-snoozed" not in ctx.msgs[-1])

# --- 4. reminder_done ends a repeating reminder -----------------------
rem._append({"op": "done", "id": "r4"})
check("done stops repeat", rem._load(force=True)["r4"]["status"] == "done")

# --- 5. back-compat: records with no repeat field ---------------------
reset()
rem._append({"op": "add", "id": "r5", "text": "legacy", "due": 1})
check("legacy add gets repeat=0", rem._load(force=True)["r5"]["repeat"] == 0)

# ======================================================================
# sharded mode
# ======================================================================

# --- 6. resolution order ----------------------------------------------
D = os.path.join(TMP, "store6")
use_store(D, shard="kopitwo")
check("dir env -> dir mode", rem.STORE_MODE == "dir", rem.STORE_MODE)
check("own shard is <dir>/<shard>.jsonl",
      rem.STORE == os.path.join(D, "kopitwo.jsonl"), rem.STORE)
use_store(D, shard="bot two/weird:name")
check("shard id sanitised", os.path.basename(rem.STORE) == "bot_two_weird_name.jsonl",
      rem.STORE)

# existing file (not ending .jsonl) still resolves to legacy file mode
f = os.path.join(TMP, "plainfile")
open(f, "w").close()
use_store(f)
check("existing file -> file mode", rem.STORE_MODE == "file" and rem.STORE == f)

# --- 7. multi-shard union + reduce ------------------------------------
D = os.path.join(TMP, "store7")
use_store(D, shard="hostA")
write_shard(D, "hostA.jsonl", [
    {"op": "add", "id": "a1", "text": "from A", "due": 100, "at": 10},
    {"op": "delivered", "id": "a1", "next_due": 200, "at": 30},
])
write_shard(D, "hostB.jsonl", [
    {"op": "add", "id": "b1", "text": "from B", "due": 150, "at": 20},
    {"op": "snooze", "id": "a1", "due": 999, "at": 40},
])
recs = rem._load(force=True)
check("union sees both shards", set(recs) == {"a1", "b1"}, set(recs))
check("cross-shard snooze applies", recs["a1"]["due"] == 999, recs["a1"])
check("cross-shard snooze reset counter", recs["a1"]["deliveries"] == 0)
check("our shard is hostA", os.path.basename(rem.STORE) == "hostA.jsonl")
rem._append({"op": "add", "id": "a2", "text": "new", "due": 1})
lines_b = open(os.path.join(D, "hostB.jsonl")).read()
check("append touches only our shard", "a2" not in lines_b)
check("appended op is visible", "a2" in rem._load(force=True))

# a .sync-conflict file is just another shard, never special-cased
write_shard(D, "hostB.sync-conflict-20260820-120000-ABCDEFG.jsonl", [
    {"op": "add", "id": "c1", "text": "conflicted", "due": 5, "at": 50},
])
check("sync-conflict shard is read like any other", "c1" in rem._load(force=True))

# --- 8. ordering within a shard is line order, not timestamp ----------
D = os.path.join(TMP, "store8")
use_store(D, shard="hostA")
write_shard(D, "hostA.jsonl", [
    {"op": "add", "id": "x", "text": "x", "due": 1, "at": 100},
    {"op": "snooze", "id": "x", "due": 500, "at": 100},
    {"op": "snooze", "id": "x", "due": 700, "at": 100},  # same `at`: line wins
])
check("same-timestamp ops keep line order", rem._load(force=True)["x"]["due"] == 700)

# --- 9. done is absorbing under out-of-order timestamps ---------------
D = os.path.join(TMP, "store9")
use_store(D, shard="hostA")
write_shard(D, "hostA.jsonl", [
    {"op": "add", "id": "d1", "text": "d", "due": 10, "at": 10},
    {"op": "done", "id": "d1", "at": 20},
])
write_shard(D, "hostB.jsonl", [  # host B's clock runs ahead
    {"op": "delivered", "id": "d1", "next_due": 999, "at": 900},
    {"op": "snooze", "id": "d1", "due": 999, "at": 950},
])
rec = rem._load(force=True)["d1"]
check("done absorbs later delivered/snooze", rec["status"] == "done", rec)
check("absorbed ops do not move due", rec["due"] == 10, rec)
check("absorbed ops do not bump counter", rec["deliveries"] == 0, rec)

# and when the skew puts `done` before the `add` itself
D = os.path.join(TMP, "store9b")
use_store(D, shard="hostA")
write_shard(D, "hostA.jsonl", [{"op": "add", "id": "d2", "text": "d", "due": 10, "at": 500}])
write_shard(D, "hostB.jsonl", [{"op": "done", "id": "d2", "at": 100}])
check("done sorting before add still wins",
      rem._load(force=True)["d2"]["status"] == "done")

# a repeating reminder still re-arms (its delivered ops precede any done)
D = os.path.join(TMP, "store9c")
use_store(D, shard="hostA")
reset()
rem._append({"op": "add", "id": "d3", "text": "poll", "due": int(time.time()) - 5,
             "repeat": 3600})
ctx = Ctx()
rem._sweep(ctx, deliver=True)
rec = rem._load(force=True)["d3"]
check("repeat re-arms under absorption rules",
      rec["status"] == "pending" and rec["due"] > time.time() + 3000, rec)
rem._append({"op": "done", "id": "d3"})
check("done still ends a repeat", rem._load(force=True)["d3"]["status"] == "done")

# --- 10. truncated final line tolerance -------------------------------
D = os.path.join(TMP, "store10")
use_store(D, shard="hostA")
write_shard(D, "hostA.jsonl", [{"op": "add", "id": "t1", "text": "ok", "due": 1, "at": 1}])
os.makedirs(D, exist_ok=True)
with open(os.path.join(D, "hostB.jsonl"), "w") as fh:
    fh.write(json.dumps({"op": "add", "id": "t2", "text": "also ok", "due": 1, "at": 2}) + "\n")
    fh.write('{"op":"add","id":"t3","text":"tor')  # torn mid-write by Syncthing
try:
    recs = rem._load(force=True)
    ok = set(recs) == {"t1", "t2"}
except Exception as e:
    ok, recs = False, e
check("truncated final line skipped, no raise", ok, recs)

# garbage / non-dict / id-less lines are equally harmless
with open(os.path.join(D, "hostC.jsonl"), "w") as fh:
    fh.write("not json at all\n[1,2,3]\n{}\n\n")
    fh.write(json.dumps({"op": "add", "id": "t4", "text": "fine", "due": 1, "at": 3}) + "\n")
check("garbage lines skipped", set(rem._load(force=True)) == {"t1", "t2", "t4"})

# --- 11. cache key includes size (append within the same second) ------
D = os.path.join(TMP, "store11")
use_store(D, shard="hostA")
write_shard(D, "hostA.jsonl", [{"op": "add", "id": "s1", "text": "a", "due": 1, "at": 1}])
rem._load(force=True)
key1 = rem._state["key"]
other = os.path.join(D, "hostB.jsonl")
with open(other, "w") as fh:
    fh.write(json.dumps({"op": "add", "id": "s2", "text": "b", "due": 1, "at": 1}) + "\n")
# same second, new shard: only re-globbing + size can notice
check("new shard invalidates cache", "s2" in rem._load(force=False))
mt = os.stat(other).st_mtime
with open(other, "a") as fh:
    fh.write(json.dumps({"op": "add", "id": "s3", "text": "c", "due": 1, "at": 1}) + "\n")
os.utime(other, (mt, mt))  # pin mtime: size is the only signal left
check("size-only change invalidates cache", "s3" in rem._load(force=False))
check("cache key is (path, mtime, size) per shard",
      all(len(t) == 3 for t in key1) and len(key1) == 1, key1)

# --- 12. one-time migration from a legacy single file -----------------
legacy = os.path.join(TMP, "legacy", "reminders.jsonl")
os.makedirs(os.path.dirname(legacy), exist_ok=True)
with open(legacy, "w") as fh:
    fh.write(json.dumps({"op": "add", "id": "m1", "text": "old", "due": 42, "at": 1}) + "\n")
rem.LEGACY_POE_ACP = legacy
D = os.path.join(TMP, "store12")
use_store(D, shard="hostA")
check("migration seeds our shard", os.path.exists(os.path.join(D, "hostA.jsonl")))
check("migrated record readable", "m1" in rem._load(force=True))
check("legacy file left in place", os.path.exists(legacy))
# second init must NOT re-copy (shard already exists) and must not duplicate
rem._append({"op": "snooze", "id": "m1", "due": 77})
use_store(D, shard="hostA")
recs = rem._load(force=True)
check("migration is one-time", recs["m1"]["due"] == 77, recs["m1"])
n = open(os.path.join(D, "hostA.jsonl")).read().count('"m1"')
check("no duplicated migration lines", n == 2, n)
# a different host on the same shared dir does not re-migrate over the top
use_store(D, shard="hostB")
check("other host gets its own shard path",
      os.path.basename(rem.STORE) == "hostB.jsonl")
check("other host still sees m1 via union", "m1" in rem._load(force=True))
rem.LEGACY_POE_ACP = "~/.local/state/poe-acp/notes/reminders.jsonl"

# --- 13. legacy single-file mode does not glob siblings ---------------
D = os.path.join(TMP, "store13")
os.makedirs(D, exist_ok=True)
write_shard(D, "other.jsonl", [{"op": "add", "id": "n1", "text": "nope", "due": 1, "at": 1}])
use_store(os.path.join(D, "mine.jsonl"))
rem._append({"op": "add", "id": "n2", "text": "mine", "due": 1})
check("file mode reads only its own file", set(rem._load(force=True)) == {"n2"},
      set(rem._load(force=True)))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
