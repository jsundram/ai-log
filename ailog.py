#!/usr/bin/env python3
"""Copy this machine's AI session logs into ai-log, skeletonized.

    python3 ailog.py                       # this machine, ~/.claude and ~/.codex
    python3 ailog.py --machine dymaxion --claude-dir ~/Dropbox/Code/settings/dymaxion.backup/.claude --no-codex

Writes logs/<machine>/claude/... and logs/<machine>/codex/... next to this
script. Only sources whose size or mtime changed since the last run are
reprocessed. Nothing in logs/ is ever deleted: a session that ages out of
~/.claude stays archived here. Secrets are not redacted here; see redact.py.
No dependencies; runs on macOS's /usr/bin/python3.
"""

import argparse
import concurrent.futures
import fcntl
import glob
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)   # so `python3 -I ailog.py` still finds skeleton.py
import full  # noqa: E402
import skeleton  # noqa: E402


def sources(claude_dir, codex_dir):
    """Yield (source, output path under the machine dir, pass class or None to copy)."""
    if claude_dir:
        proj = os.path.join(claude_dir, "projects")
        # cloud.py's teleport folders: it files those sessions itself, as "cloud".
        skip = lambda f: "--cache-ai-log-" in f
        for f in glob.glob(os.path.join(proj, "*", "*.jsonl")):
            if not skip(f):
                yield f, os.path.join("claude", "projects", os.path.relpath(f, proj)), skeleton.Claude
        # Subagent and workflow-agent transcripts, plus their small metadata
        # (agent descriptions, workflow scripts), which are copied as-is.
        for f in glob.glob(os.path.join(proj, "*", "*", "**", "*"), recursive=True):
            if skip(f):
                continue
            rel = os.path.join("claude", "projects", os.path.relpath(f, proj))
            if f.endswith(".jsonl"):
                yield f, rel, skeleton.Claude
            elif f.endswith(".meta.json") or any(os.sep + d + os.sep in f for d in ("workflows", "memory")):
                if os.path.isfile(f) and "tool-results" not in f:
                    yield f, rel, None
        # Plans, task and todo lists: small, and the plainest statements of intent.
        for sub in ("plans", "tasks", "todos"):
            for f in glob.glob(os.path.join(claude_dir, sub, "**", "*"), recursive=True):
                if os.path.isfile(f):
                    yield f, os.path.join("claude", os.path.relpath(f, claude_dir)), None
        h = os.path.join(claude_dir, "history.jsonl")
        if os.path.exists(h):
            yield h, os.path.join("claude", "history.jsonl"), skeleton.ClaudeHistory
    if codex_dir:
        # Archiving moves a rollout from sessions/ to archived_sessions/; file
        # names are unique, so key by name to keep one copy.
        for sub in ("sessions", "archived_sessions"):
            for f in glob.glob(os.path.join(codex_dir, sub, "**", "rollout-*.jsonl*"), recursive=True):
                name = os.path.basename(f)
                if name.endswith(".zst"):   # Codex compresses older rollouts
                    name = name[:-4]
                elif not name.endswith(".jsonl"):
                    continue
                yield f, os.path.join("codex", "sessions", name[8:15], name), skeleton.Codex
        for name in ("history.jsonl", "session_index.jsonl"):
            h = os.path.join(codex_dir, name)
            if os.path.exists(h):
                yield h, os.path.join("codex", name), skeleton.Passthrough
        # Automation definitions and the pages and charts Codex generated.
        for sub in ("automations", "visualizations"):
            for f in glob.glob(os.path.join(codex_dir, sub, "**", "*"), recursive=True):
                if os.path.isfile(f):
                    yield f, os.path.join("codex", os.path.relpath(f, codex_dir)), None


def write(f, out):
    if out is None:
        return 0
    for o in out if isinstance(out, list) else [out]:
        f.write(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(out) if isinstance(out, list) else 1


def open_text(src):
    if not src.endswith(".zst"):
        return open(src, encoding="utf-8", errors="replace")
    # launchd's PATH is minimal, so look where Homebrew puts zstd too.
    zstd = shutil.which("zstd", path=os.pathsep.join(
        [os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]))
    if not zstd:
        raise RuntimeError("zstd not found; brew install zstd")
    out = subprocess.run([zstd, "-dc", src], check=True, stdout=subprocess.PIPE).stdout
    return io.StringIO(out.decode("utf-8", errors="replace"))


def convert(src, dst, cls):
    if cls is None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return 1
    fn = cls()
    tmp = dst + ".tmp"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    kept = 0
    with open_text(src) as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            kept += write(fout, fn(r))
        kept += write(fout, getattr(fn, "flush", lambda: None)())
    os.replace(tmp, dst)
    return kept


def note_timezone(root):
    """Append this machine's timezone to timezone.jsonl when it changes. Nothing
    else records where a machine was; transcripts keep only UTC times."""
    zone = os.path.realpath("/etc/localtime").partition("zoneinfo/")[2] or None
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "zone": zone, "offset": time.strftime("%z")}
    path = os.path.join(root, "timezone.jsonl")
    last = None
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                last = json.loads(line)
    if not last or (last.get("zone"), last.get("offset")) != (row["zone"], row["offset"]):
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=0, sort_keys=True)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--machine", default=socket.gethostname().split(".")[0])
    ap.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"))
    ap.add_argument("--codex-dir", default=os.path.expanduser("~/.codex"))
    ap.add_argument("--no-claude", action="store_true")
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "logs"))
    ap.add_argument("--force", action="store_true", help="reprocess everything")
    ap.add_argument("--no-full", action="store_true", help="skeletons only; skip the full archive")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    claude_dir = None if a.no_claude else os.path.expanduser(a.claude_dir)
    codex_dir = None if a.no_codex or not os.path.isdir(os.path.expanduser(a.codex_dir)) \
        else os.path.expanduser(a.codex_dir)
    root = os.path.join(a.out, a.machine)
    os.makedirs(root, exist_ok=True)

    lock = open(os.path.join(root, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another ailog run holds %s; exiting" % root, file=sys.stderr)
        return 0

    if a.machine == socket.gethostname().split(".")[0]:   # not when reading a backup
        note_timezone(root)

    state_path = os.path.join(root, ".state.json")
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}

    do_full = not a.no_full
    if do_full and full.missing_tools():
        print("full archive skipped: missing %s (brew install zstd imagemagick ffmpeg)"
              % ", ".join(full.missing_tools()), file=sys.stderr)
        do_full = False
    images_dir = os.path.join(a.out, "images")   # shared by every machine
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
    last_save = time.time()
    full_done = 0

    t0 = time.time()
    done = skipped = failed = 0
    bytes_in = bytes_out = 0
    for src, rel, fn in sources(claude_dir, codex_dir):
        try:
            st = os.stat(src)
        except OSError:
            continue
        sig = [st.st_size, st.st_mtime_ns]
        dst = os.path.join(root, rel)
        if a.force or state.get(rel) != sig or not os.path.exists(dst):
            try:
                convert(src, dst, fn)
                state[rel] = sig
                done += 1
                bytes_in += st.st_size
                bytes_out += os.path.getsize(dst)
                if not a.quiet:
                    print("%8.1f MB -> %7.2f MB  %s" % (st.st_size / 1e6, os.path.getsize(dst) / 1e6, rel))
            except Exception as e:   # one bad file shouldn't stop the run
                failed += 1
                print("FAILED %s: %s" % (src, e), file=sys.stderr)
        else:
            skipped += 1

        # The full archive: every line kept, images stored once (see full.py).
        key = "full/" + rel
        if not do_full or fn is None or (not a.force and state.get(key) == sig):
            continue
        base = os.path.join(root, "full", re.sub(r"\.jsonl$", "", rel))
        app = rel.split(os.sep)[0]   # "claude" or "codex"
        project = rel.split(os.sep)[2] if rel.startswith(os.path.join("claude", "projects")) else app
        try:
            n = full.archive(src, open_text, base, images_dir, project, app,
                             os.sep + "subagents" + os.sep in rel, pool)
            state[key] = sig
            full_done += 1
            if not a.quiet and n:
                print("           %5d images  %s" % (n, rel))
        except Exception as e:
            failed += 1
            print("FAILED full %s: %s" % (src, e), file=sys.stderr)
        if time.time() - last_save > 60:   # long first runs survive an interruption
            save_state(state_path, state)
            last_save = time.time()

    pool.shutdown()
    save_state(state_path, state)
    print("%s: %d updated, %d unchanged, %d failed; %.1f MB -> %.1f MB in %.0fs; full archive: %d updated" % (
        a.machine, done, skipped, failed, bytes_in / 1e6, bytes_out / 1e6, time.time() - t0, full_done))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
