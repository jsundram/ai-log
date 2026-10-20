#!/usr/bin/env python3
"""Fetch Claude Code cloud sessions into ai-log, as the machine "cloud".

    python3 cloud.py --month 2026-09          # cloud sessions with commits that month
    python3 cloud.py --since 2026-06-01       # through today
    python3 cloud.py --month 2026-09 --list   # say what's missing; fetch nothing
    python3 cloud.py --session session_01Ta8YQTWSD7RjQnv5hsUszZ --repo jsundram/AKM

A session run on claude.ai leaves no transcript on any Mac. Its commits name
it, in a `Claude-Session: https://claude.ai/code/session_<id>` line, and
`claude --teleport session_<id>` downloads its transcript. This finds those
ids with a GitHub commit search, skips the ones the index already has, and
teleports each of the rest from an empty git repo whose origin is the
session's repo, kept under ~/.cache/ai-log/repos, so no checkout of yours is
touched and no code is downloaded. It answers the folder-trust prompt, types
/exit once the session has loaded, and presses Escape at any other prompt.

The transcript is filed as if the session had run in ~/Dropbox/Code/<repo>,
minus the records teleport adds, with a first record naming the
cloud session:

    {"type": "cloud-session", "url": "https://claude.ai/code/session_<id>", "repo": ..., "fetched": ...}

The trimmed copies live in ~/.cache/ai-log/cloud/.claude; ailog.py then
archives them under logs/cloud/ and index.py picks them up. Each attempt
is logged to logs/cloud/teleport.jsonl.

It needs `gh` signed in, `claude` signed in to claude.ai, and a terminal of
its own: run it outside the sandbox. Only commits on a repo's default branch
are searchable, so a cloud session whose branch was never merged is missed,
as is one that made no commits.
"""

import argparse
import fcntl
import datetime
import glob
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.expanduser("~/.cache/ai-log")
CLONES = os.path.join(CACHE, "repos")   # one empty repo per GitHub repo, reused
STAGE = os.path.join(CACHE, "cloud", ".claude")
PROJECTS = os.path.expanduser("~/.claude/projects")
CODE = os.path.expanduser("~/Dropbox/Code")
LOG = os.path.join(HERE, "logs", "cloud", "teleport.jsonl")
_ID = re.compile(r"claude\.ai/code/session_([A-Za-z0-9]{20,})")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
# Run from a Claude Code session, the child would inherit its CLAUDE* markers
# (one turns off transcript saving) and any API key; teleport needs the
# claude.ai sign-in.
def child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "ANTHROPIC_API_KEY"}
    env["TERM"] = "xterm-256color"
    return env
TIMEOUT = 180


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(t):
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def mangle(path):
    """Claude Code's folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def months(since, until):
    d = since.replace(day=1)
    while d <= until:
        nxt = (d + datetime.timedelta(days=32)).replace(day=1)
        yield max(d, since), min(nxt - datetime.timedelta(days=1), until)
        d = nxt


def search(since, until):
    """cloud session id -> {repo, commits, first} from Claude-authored commits."""
    owner = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, check=True).stdout.strip()
    found = {}
    for a, b in months(since, until):   # the search returns at most 1,000 per query
        q = '"Claude-Session" user:%s author-date:%s..%s' % (owner, a, b)
        out = subprocess.run(["gh", "api", "-X", "GET", "search/commits", "-f", "q=" + q,
                              "-f", "per_page=100", "--paginate", "--jq",
                              ".items[] | {repo: .repository.full_name, author: .commit.author.name,"
                              " date: .commit.author.date, message: .commit.message} | tojson"],
                             capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            c = json.loads(line)
            if c["author"] != "Claude":   # your own commits come from local sessions
                continue
            for sid in set(_ID.findall(c["message"])):
                s = found.setdefault(sid, {"repos": {}, "first": c["date"]})
                s["repos"][c["repo"]] = s["repos"].get(c["repo"], 0) + 1
                s["first"] = min(s["first"], c["date"])
    return {sid: {"repo": max(s["repos"], key=s["repos"].get), "commits": sum(s["repos"].values()),
                  "first": s["first"]} for sid, s in found.items()}


def known():
    """Session ids the archive already has, local or cloud."""
    subprocess.run([sys.executable, os.path.join(HERE, "index.py")], check=True, stdout=subprocess.DEVNULL)
    ids = set()
    for line in open(os.path.join(HERE, "logs", "index", "sessions.jsonl"), encoding="utf-8"):
        ids.update(json.loads(line).get("remoteIds") or [])
    return ids


def stub(repo):
    """An empty git repo whose origin is `repo`, under CLONES.

    Teleport wants to run in a checkout of the session's repo, but only to
    match it by origin and to switch to the session's branch; the transcript
    comes from claude.ai. With no files there is nothing to download, nothing
    to leave half switched, and no stash prompt.
    """
    path = os.path.join(CLONES, repo)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(path, exist_ok=True)
        subprocess.run(["git", "init", "-q", path], check=True)
        subprocess.run(["git", "-C", path, "remote", "add", "origin",
                        "https://github.com/%s.git" % repo], check=True)
    return path


def arrived(cwd, since):
    """The transcript teleport wrote: one written after `since` whose session ran in `cwd`."""
    mark = '"cwd":%s' % json.dumps(cwd)
    for f in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) >= since and mark in open(f, encoding="utf-8").read():
                return f
        except OSError:
            pass
    return None


def teleport(sid, cwd, log):
    """Run `claude --teleport` in a terminal of its own; return the transcript path."""
    env = child_env()
    start = time.time()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        os.execvpe("claude", ["claude", "--teleport", "session_" + sid], env)
    screen, trusted, exits, question = "", False, 0, None
    next_try = last_out = 0

    def send(keys):
        log.write(b"\n<<%.1fs sent %r>>\n" % (time.time() - start, keys))
        os.write(fd, keys)
    try:
        while time.time() - start < TIMEOUT:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break   # the child closed the terminal
                if not data:
                    break
                log.write(data)
                screen = (screen + _ANSI.sub("", data.decode("utf-8", "replace")))[-4000:]
                last_out = time.time()
            if os.waitpid(pid, os.WNOHANG)[0]:
                pid = 0
                break
            # The folder-trust prompt for the new, empty folder. Its
            # default is "No, exit", and it redraws (resetting to "No") just
            # after it appears. So act only once the screen is still, on what
            # the newest frame shows selected: move down to "Yes", confirm it.
            # (A miss is harmless: "No" just ends the attempt.)
            flat = screen.replace(" ", "")
            quiet = time.time() - last_out
            if not trusted and "Itrustthisfolder" in flat:
                if quiet > 1 and time.time() >= next_try:
                    if flat.rfind("\u276fYes") > flat.rfind("\u276fNo"):
                        send(b"\r")
                        trusted, screen = True, ""
                    else:
                        send(b"\x1b[B")   # down arrow
                    next_try = time.time() + 1.5
                continue
            # Teleport has drawn the session and gone still: leave. The
            # transcript is written as the session closes. /exit, then Escape
            # and Ctrl-C twice in case a dialog has the keyboard.
            if not screen or quiet < 5 or time.time() < next_try:
                continue
            # Any other prompt waiting for Enter isn't ours to answer: cancel it.
            asking = "Entertoconfirm" in flat[-600:]
            if asking and not question:
                question = flat[max(0, flat.rfind("Entertoconfirm") - 300):].split("\u276f")[0][-200:]
            keys = [b"/exit", b"\r"] if exits < 3 and not asking else [b"\x1b", b"\x03", b"\x03"]
            for k in keys:
                send(k)
                time.sleep(0.3)
            exits += 1
            next_try = time.time() + 5
    finally:
        if pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
                time.sleep(2)
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
        os.close(fd)
    time.sleep(1)
    return arrived(cwd, start), start, question


def stage(sid, repo, src, clone_dir, start):
    """Copy the transcript to the cloud machine's folder: trimmed, cwd mapped, tagged."""
    local = os.path.join(CODE, repo.split("/")[-1])
    cut = datetime.datetime.fromtimestamp(start - 60, datetime.timezone.utc)
    kept = []
    for line in open(src, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = parse_ts(r.get("timestamp"))
        if t and t >= cut:
            continue   # teleport's own records: mostly at the end, sometimes first
        if isinstance(r.get("cwd"), str) and r["cwd"].startswith(clone_dir):
            r["cwd"] = local + r["cwd"][len(clone_dir):]
        kept.append(r)
    first = next((r["timestamp"] for r in kept if r.get("timestamp")), iso(cut))
    head = {"type": "cloud-session", "timestamp": first, "url": "https://claude.ai/code/session_" + sid,
            "repo": repo, "fetched": iso(now())}
    dst = os.path.join(STAGE, "projects", mangle(local), "session_%s.jsonl" % sid)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst + ".tmp", "w", encoding="utf-8") as f:
        for r in [head] + kept:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(dst + ".tmp", dst)
    return dst, sum(1 for r in kept if r.get("type") == "user" and (r.get("origin") or {}).get("kind") == "human")


def note(**kw):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(time=iso(now()), **kw)) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", help="YYYY-MM")
    ap.add_argument("--since", help="YYYY-MM-DD")
    ap.add_argument("--until", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--session", help="one session id (with --repo), skipping the search")
    ap.add_argument("--repo", help="owner/name; with --month or --since, only this repo")
    ap.add_argument("--list", action="store_true", help="list the missing sessions and stop")
    ap.add_argument("--limit", type=int, help="fetch at most this many")
    a = ap.parse_args()

    if a.session:
        if not a.repo:
            ap.error("--session needs --repo")
        todo = {a.session.split("_")[-1]: {"repo": a.repo, "commits": None, "first": None}}
    else:
        if a.month:
            since = datetime.date.fromisoformat(a.month + "-01")
            until = (since + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        elif a.since:
            since = datetime.date.fromisoformat(a.since)
            until = datetime.date.fromisoformat(a.until) if a.until else datetime.date.today()
        else:
            ap.error("give --month, --since or --session")
        have = known()
        todo = {sid: s for sid, s in search(since, until).items()
                if sid not in have and (not a.repo or s["repo"] == a.repo)}
    todo = dict(sorted(todo.items(), key=lambda kv: kv[1]["first"] or ""))
    print("%d cloud sessions to fetch" % len(todo), flush=True)
    for sid, s in todo.items():
        print("  %s  %-28s %s commits  session_%s" % ((s["first"] or "")[:10], s["repo"], s["commits"], sid))
    if a.list or not todo:
        return 0

    done = failed = 0
    for sid, s in list(todo.items())[:a.limit]:
        repo = s["repo"]
        try:
            cdir = stub(repo)
            os.makedirs(os.path.join(CACHE, "logs"), exist_ok=True)
            with open(os.path.join(CACHE, "logs", "session_%s.log" % sid), "wb") as log:
                src, start, question = teleport(sid, cdir, log)
            if not src:
                raise RuntimeError("no transcript arrived%s; see %s" % (
                    " (stopped at a prompt: %s)" % question if question else "",
                    os.path.join(CACHE, "logs", "session_%s.log" % sid)))
            dst, prompts = stage(sid, repo, src, cdir, start)
            # The download now lives in the archive; don't let the daily run
            # file it a second time under the empty repo's folder.
            subprocess.run(["trash", src] + glob.glob(os.path.splitext(src)[0]), check=False)
            try:
                os.rmdir(os.path.dirname(src))   # only if now empty
            except OSError:
                pass
            note(session=sid, repo=repo, status="ok", prompts=prompts, file=dst)
            print("ok      session_%s  %s  %d prompts" % (sid, repo, prompts), flush=True)
            done += 1
        except Exception as e:   # one bad session shouldn't stop the run
            note(session=sid, repo=repo, status="failed", error=str(e))
            print("FAILED  session_%s  %s: %s" % (sid, repo, e), file=sys.stderr)
            failed += 1

    if done:
        subprocess.run([sys.executable, os.path.join(HERE, "ailog.py"), "--machine", "cloud",
                        "--claude-dir", STAGE, "--no-codex", "-q"], check=False)
        subprocess.run([sys.executable, os.path.join(HERE, "index.py")], check=False)
    print("%d fetched, %d failed" % (done, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
