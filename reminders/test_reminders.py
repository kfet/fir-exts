"""Offline tests for the reminders reducer + sweep. No fir runtime needed."""
import importlib.util, json, os, sys, tempfile, time, types

# stub fir_ext so the module imports standalone
stub = types.ModuleType("fir_ext")
stub.on = lambda *a, **k: (lambda f: f)
stub.tool = lambda *a, **k: (lambda f: f)
stub.run = lambda *a, **k: None
sys.modules["fir_ext"] = stub

TMP = tempfile.mkdtemp()
os.environ["FIR_REMINDERS_STORE"] = os.path.join(TMP, "r.jsonl")

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

def reset():
    open(rem.STORE, "w").close()
    rem._state.update({"mtime": -1.0, "recs": {}, "next_due": float("inf")})

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
    rem._state["mtime"] = -1.0
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
    rem._state["mtime"] = -1.0
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

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
