#!/usr/bin/env python3
# ---
# name: repo-skills
# description: repo-skills extension (forged in-session)
# ---
# repo-skills: when a tool touches a path in a git repo not yet seen this
# session, inject a short note listing that repo's AGENTS.md and skills.
import os, re, sys, shlex
import fir_ext

SEEN = set()      # repo roots already announced
RESOLVED = {}     # dir -> root or None
LOG = os.path.expanduser("~/.cache/fir/repo-skills.log")
HOME = os.path.realpath(os.path.expanduser("~"))
SKILL_DIRS = (".fir/skills", "skills", ".claude/skills")
MAX_SKILLS = 15


def loaded_names():
    """Skill names fir already shows the agent (builtin + global dirs)."""
    names = set()
    for base in (os.path.expanduser("~/.cache/fir/builtin-skills"),):
        if os.path.isdir(base):
            for h in os.listdir(base):
                d = os.path.join(base, h)
                if os.path.isdir(d):
                    names.update(os.listdir(d))
    for base in (os.path.expanduser("~/.config/fir/skills"),):
        if os.path.isdir(base):
            names.update(os.listdir(base))
    return names


LOADED = loaded_names()


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def paths_from(params, cwd):
    out = []
    for k in ("path", "cwd", "file_path", "directory"):
        v = params.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    cmd = params.get("command")
    if isinstance(cmd, str):
        for m in re.finditer(r"(?:^|[;&|\s])cd\s+([^\s;&|]+)", cmd):
            out.append(m.group(1))
        for m in re.finditer(r"(?<![\w/])(~?/[\w.\-/]+)", cmd):
            out.append(m.group(1))
    res = []
    for p in out:
        p = os.path.expanduser(p.strip("'\""))
        if not os.path.isabs(p):
            p = os.path.join(cwd, p)
        res.append(p)
    return res


def repo_root(p):
    p = os.path.realpath(p)
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    chain = []
    cur = p
    while True:
        if cur in RESOLVED:
            root = RESOLVED[cur]
            break
        chain.append(cur)
        if os.path.exists(os.path.join(cur, ".git")):  # dir or worktree file
            root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            root = None
            break
        cur = parent
    for c in chain:
        RESOLVED[c] = root
    return root


def frontmatter(path):
    name = desc = ""
    try:
        with open(path) as f:
            lines = f.read(4000).splitlines()
    except OSError:
        return name, desc
    if not lines or lines[0].strip() != "---":
        return name, desc
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ln.startswith("name:"):
            name = ln[5:].strip().strip("'\"")
        elif ln.startswith("description:"):
            desc = ln[12:].strip().strip("'\"")
    return name, desc


def describe(root):
    lines = [f"[repo-skills] Repo touched: {root}"]
    if not root.startswith(HOME + "/"):
        lines.append("(outside $HOME: untrusted, listed only)")
    for a in ("AGENTS.md", "CLAUDE.md"):
        if os.path.isfile(os.path.join(root, a)):
            lines.append(f"- Read first: {os.path.join(root, a)}")
    skills = []
    skipped = []
    for d in SKILL_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for ent in sorted(os.listdir(base)):
            sk = os.path.join(base, ent, "SKILL.md")
            if os.path.isfile(sk):
                n, ds = frontmatter(sk)
                n = n or ent
                if n in LOADED:
                    skipped.append(n)
                    continue
                skills.append(f"- {n}: {ds[:120]} ({sk})")
    if skills:
        lines.append("Repo skills (load the SKILL.md when the task matches):")
        extra = len(skills) - MAX_SKILLS
        lines += skills[:MAX_SKILLS]
        if extra > 0:
            lines.append(f"- +{extra} more in the skill dirs above")
    if skipped:
        lines.append("Same name as a loaded skill (repo copy may differ): "
                     + ", ".join(f"{n}" for n in skipped[:20]))
    if len(lines) == 1:
        return None
    return "\n".join(lines)


@fir_ext.on("hook/tool_call")
def on_tool_call(params, ctx):
    try:
        args = params.get("params") or {}
        cwd = os.getcwd()
        for p in paths_from(args, cwd):
            root = repo_root(p)
            if not root or root in SEEN:
                continue
            SEEN.add(root)
            note = describe(root)
            log(f"tool={params.get('tool_name')} path={p} root={root} note={'yes' if note else 'no'}")
            if note:
                ctx.send_message("repo_skills", note, display=True, deliver_as="steer")
    except Exception as e:  # never block a tool
        log(f"error: {e!r}")
    return None


@fir_ext.on("session_start")
def on_start(params, ctx):
    # The start cwd is already loaded by fir: mark it seen.
    r = repo_root(os.getcwd())
    if r:
        SEEN.add(r)


if __name__ == "__main__":
    fir_ext.run()
