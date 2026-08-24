---
name: source-first
description: Read the source before saying what a tool does. Use whenever you are about to run strings, file, nm or objdump on a binary, or state what fir, poe-acp or any fleet tool supports. The source is on the build boxes, not on this host.
---

# Source first

Fleet hosts carry the binary only. That is normal — the source is one ssh away.
Never describe what a tool does from `strings`, from `--help`, or from the shape
of a directory. Read the code.

## Find it

```sh
for h in mikiserver zboxserver kfetairm1; do
  echo "== $h"; ssh -o ConnectTimeout=6 $h 'ls ~/src ~/dev/ai 2>/dev/null'
done
```

Repos live at `~/src/<repo>` (airm1 is still on `~/dev/ai/<repo>`). Probe —
names drift, and a remembered path is usually wrong.

## Read it

ssh in to grep, or copy the file back. Check `git -C <dir> status -sb` before
quoting a clone as current, and check the running binary's `--version`
separately — they often differ.

For fir's own behaviour, the `self` skill is the faster answer; drop to source
when it does not settle the question.
