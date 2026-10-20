#!/usr/bin/env python3
"""File claude.ai artifacts into logs/claude.ai/artifacts/.

    python3 artifacts.py <manifest.tsv> <saved-files-dir>

Artifacts live on claude.ai, and only a Claude session can list and read
them (a headless `claude -p` run with an API key can't). So capture is two
steps: in a Claude session, ask Claude to list your artifacts and read each
one's index.html into a scratch folder, writing a manifest with one line per
artifact:

    <id> TAB <title> TAB <last updated> TAB <saved folder> TAB <version>

then run this. Each version is kept, so re-running after a page changes adds
the new version beside the old one:

    logs/claude.ai/artifacts/<id>/<version>.html
    logs/claude.ai/artifacts.jsonl     one line per artifact version

A version id starts with the time it was published (seconds since 1970).
`session` names the local session that published it, from the index, when
one did; artifacts with none came from claude.ai chats or cloud sessions.
"""

import datetime
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "claude.ai")


def published_by():
    """artifact id -> (project, session id) from the index, if it has been built."""
    found = {}
    path = os.path.join(HERE, "logs", "index", "sessions.jsonl")
    if not os.path.exists(path):
        return found
    for line in open(path, encoding="utf-8"):
        s = json.loads(line)
        for a in s.get("artifacts") or []:
            aid = (a.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            found.setdefault(aid, (s.get("project"), s.get("id")))
    return found


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    manifest, saved = sys.argv[1], sys.argv[2]
    rows = {}
    jl = os.path.join(OUT, "artifacts.jsonl")
    if os.path.exists(jl):
        for line in open(jl, encoding="utf-8"):
            r = json.loads(line)
            rows[(r["id"], r["version"])] = r
    by = published_by()
    added = 0
    for line in open(manifest, encoding="utf-8"):
        if not line.strip():
            continue
        aid, title, updated, folder, version = line.rstrip("\n").split("\t")
        src = os.path.join(saved, folder, "index.html")
        dst = os.path.join(OUT, "artifacts", aid, version + ".html")
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            added += 1
        raw = open(dst, "rb").read()
        project, session = by.get(aid, (None, None))
        rows[(aid, version)] = {
            "id": aid, "title": title, "url": "https://claude.ai/artifact/" + aid,
            "version": version,
            "published": datetime.datetime.fromtimestamp(int(version.split("-")[0]),
                                                         datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "lastUpdated": updated, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "file": os.path.relpath(dst, OUT), "project": project, "session": session}
    os.makedirs(OUT, exist_ok=True)
    with open(jl + ".tmp", "w", encoding="utf-8") as f:
        for r in sorted(rows.values(), key=lambda r: r["published"]):
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(jl + ".tmp", jl)
    print("%d artifact versions (%d new) -> %s" % (len(rows), added, OUT))


if __name__ == "__main__":
    main()
