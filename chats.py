#!/usr/bin/env python3
"""File a claude.ai data export into logs/claude.ai/.

    python3 chats.py ~/Downloads/manifest-<…>.json

Ask for the export at claude.ai → Settings → Privacy → Export data. The
email links to a manifest; download each zip it lists (the links expire after
a day) next to it, then run this on the manifest. It writes:

    logs/claude.ai/conversations/<uuid>.json   one chat each, as exported
    logs/claude.ai/projects/<uuid>.json        claude.ai projects, with their documents
    logs/claude.ai/memories/<uuid>.json        what claude.ai remembers about you
    logs/claude.ai/exports/<date>/frames.tar.zst
                                               every artifact version in the export,
                                               as exported, in one archive
    logs/claude.ai/artifacts.jsonl             a line per artifact version

Each export is a full snapshot. A later one replaces the chats, projects and
memories it holds and adds its own artifact archive; nothing is deleted. The
artifact versions are kept in one archive because they repeat each other: 394
MB of pages compress to 43 MB together, against 225 MB one by one. Read one
back with `zstd -dc frames.tar.zst | tar -xO <member>`. The account and
login-history file is skipped.

index.py lists each chat as a session from claude.ai, with what you typed.
"""

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import artifacts  # noqa: E402

OUT = artifacts.OUT
_UUID = re.compile(r"^[0-9a-f-]{36}$")
_VERSION = re.compile(r"^artifacts/([0-9a-f-]{36})/versions/([0-9]+-[0-9a-f]+)\.html$")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def file_each(z, member, folder):
    """A JSON file of records (or one record) -> folder/<uuid>.json, newest kept."""
    data = json.loads(z.read(member))
    n = 0
    for r in data if isinstance(data, list) else [data]:
        uid = r.get("uuid") or r.get("account_uuid") or os.path.splitext(os.path.basename(member))[0]
        if not _UUID.match(uid):
            continue
        path = os.path.join(OUT, folder, uid + ".json")
        if os.path.exists(path):
            old = json.load(open(path, encoding="utf-8"))
            if (old.get("updated_at") or "") > (r.get("updated_at") or ""):
                continue   # an older export than the one already filed
        write_json(path, r)
        n += 1
    return n


def frames(z, dst):
    """Every file under artifacts/ -> one tar.zst; returns {member: (uuid, version)} for the pages."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    pages = {}
    with open(dst + ".tmp", "wb") as out:
        zst = subprocess.Popen(["zstd", "-19", "--long=27", "-T0", "-q", "-c"], stdin=subprocess.PIPE, stdout=out)
        with tarfile.open(fileobj=zst.stdin, mode="w|") as tar:
            for info in z.infolist():
                name = info.filename
                if info.is_dir() or name.startswith("/") or ".." in name.split("/"):
                    continue
                ti = tarfile.TarInfo(name)
                ti.size = info.file_size
                with z.open(info) as src:
                    tar.addfile(ti, src)
                m = _VERSION.match(name)
                if m:
                    pages[name] = m.groups()
        zst.stdin.close()
        if zst.wait():
            raise RuntimeError("zstd failed")
    os.replace(dst + ".tmp", dst)
    return pages


def artifact_rows(z, pages, archive):
    """Rows for artifacts.jsonl, one per version, merged with the ones already there."""
    jl = os.path.join(OUT, "artifacts.jsonl")
    rows = {}
    if os.path.exists(jl):
        for line in open(jl, encoding="utf-8"):
            r = json.loads(line)
            rows[(r["id"], r["version"])] = r
    # The export names artifacts by uuid; their links use a short id. A version
    # id is the same in both, so a version already saved gives the short id.
    short = {v: aid for aid, v in rows}
    by = artifacts.published_by()
    meta, n = {}, 0
    for info in z.infolist():
        if info.filename.endswith("/artifact.json"):
            a = json.loads(z.read(info))
            meta[a["id"]] = a
    for uid, a in meta.items():
        aid = next((short[v["id"]] for v in a["versions"] if v["id"] in short), None)
        for v in a["versions"]:
            member = "artifacts/%s/versions/%s.html" % (uid, v["id"])
            if member not in pages:
                continue
            raw = z.read(member)
            key = (aid or uid, v["id"])
            project, session = by.get(aid, (None, None))
            rows[key] = dict(rows.get(key, {}), **{
                "id": aid or uid, "uuid": uid, "title": v.get("title"), "description": v.get("description"),
                "url": "https://claude.ai/artifact/" + aid if aid else "https://claude.ai/code/artifact/" + uid,
                "version": v["id"], "published": v.get("created_at"), "active": v["id"] == a.get("active_version"),
                "lastUpdated": (a.get("updated_at") or "")[:10], "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "export": os.path.relpath(archive, OUT), "member": member,
                "project": rows.get(key, {}).get("project", project),
                "session": rows.get(key, {}).get("session", session)})
            n += 1
    with open(jl + ".tmp", "w", encoding="utf-8") as f:
        for r in sorted(rows.values(), key=lambda r: r.get("published") or ""):
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(jl + ".tmp", jl)
    return len(meta), n


def main():
    if len(sys.argv) != 2 or not sys.argv[1].endswith(".json"):
        sys.exit(__doc__)
    manifest = json.load(open(sys.argv[1], encoding="utf-8"))
    where = os.path.dirname(os.path.abspath(sys.argv[1]))
    date = (manifest.get("created_at") or datetime.date.today().isoformat())[:10]
    for f in manifest["data_files"]:
        path = os.path.join(where, os.path.basename(f["filename"]))
        if not os.path.exists(path):
            print("missing %s: download it from the manifest's link" % f["filename"], file=sys.stderr)
            continue
        cat = f["category"]
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".json")]
            if cat == "conversations":
                n = sum(file_each(z, m, "conversations") for m in names)
                print("%d chats" % n)
            elif cat in ("projects", "memories"):
                n = sum(file_each(z, m, cat) for m in names)
                print("%d %s" % (n, cat))
            elif cat == "frames":
                archive = os.path.join(OUT, "exports", date, "frames.tar.zst")
                pages = frames(z, archive)
                n, v = artifact_rows(z, pages, archive)
                print("%d artifacts, %d versions -> %s (%.0f MB)" % (n, v, archive, os.path.getsize(archive) / 1e6))
            else:
                print("skipped %s" % f["filename"])
    subprocess.run([sys.executable, os.path.join(HERE, "index.py")], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
