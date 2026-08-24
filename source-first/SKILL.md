---
name: source-first
description: Read the source before you say what a tool does or supports. Use when you are about to run strings, file, nm or objdump on a binary, or describe a tool from --help or memory.
---

# Source first

Read the code, not the binary or `--help`. Look on this host first (`~/src`, `~/dev`), then on hosts in `~/.ssh/config`. Check the clone's `git status -sb` against the binary's `--version`. Quote file:line.
