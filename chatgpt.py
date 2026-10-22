#!/usr/bin/env python3
"""File a ChatGPT data export into logs/chatgpt/.

    python3 chatgpt.py ~/Downloads/<…>.zip

Ask for the export at chatgpt.com → Settings → Data controls → Export data.
The email links to one zip; download it and run this on it. It writes:

    logs/chatgpt/conversations/<id>.json   one chat each, as exported
    logs/chatgpt/files.jsonl               a line per file in the export: its name,
                                           the chats that use it, and where it's kept
    logs/chatgpt/templates.json            each project and custom GPT your chats ran
                                           in, with their titles; fill in "name"
    logs/images/<id[:2]>/<id>.avif         images, as AVIF, in the store full.py uses
    logs/chatgpt/exports/<date>/files.tar.zst
                                           everything else, as exported: PDFs, code,
                                           zips, ChatGPT sites, the library and
                                           share lists

Each export is a full snapshot. A later one replaces the chats it holds and
adds its own archive; nothing is deleted. Images are lossy like full.py's
(AVIF q50, long edge at most 1600 px). Audio and video are listed in
files.jsonl but not kept: they're your own recordings, uploaded from
elsewhere, and one is 200 MB.
Read a file back with `zstd -dc files.tar.zst | tar -xO <member>`. Skipped:
the account file, settings, ads, and chat.html (the same chats as one page).

The export names projects and custom GPTs only by id. templates.json lists
each one's chats, with a link to it and to its latest chat; the import
prints those links for each one without a name. Open one, and write the name
it shows as "name" in templates.json (a retired GPT says "GPT not found"; its
chat's titles are the only clue). Names are kept when it's rewritten, and
index.py uses them.

index.py lists each chat as a session from chatgpt, with what you typed.
"""

import concurrent.futures
import datetime
import glob
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
import full  # noqa: E402

LOGS = os.path.join(HERE, "logs")
OUT = os.path.join(LOGS, "chatgpt")
SKIP = {"user.json", "user_settings.json", "ads.json", "chat.html"}
_ID = re.compile(r"^[0-9a-f-]{36}$")
_CHATS = re.compile(r"^conversations-\d+\.json$")
_FILE = re.compile(r"(file[-_][A-Za-z0-9]+)")
_DATE = re.compile(r"-(\d{4}-\d\d-\d\d)-\d\d-\d\d-\d\d-")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def kind(head):
    if full.image_kind(head)[0] or head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image"
    if head[4:11] == b"ftypM4A" or head[:3] == b"ID3" or head[:4] in (b"OggS", b"fLaC") \
            or head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio"
    if head[4:8] == b"ftyp" or head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video"
    return "file"


def file_chats(z, names):
    """Each chat, filed; returns {file id: [chat ids that point at it]}."""
    uses = {}
    n = 0
    for member in names:
        for c in json.loads(z.read(member)):
            cid = c.get("id") or c.get("conversation_id") or ""
            if not _ID.match(cid):
                continue
            for fid in set(_FILE.findall(json.dumps(c.get("mapping") or {}))):
                uses.setdefault(fid, []).append(cid)
            path = os.path.join(OUT, "conversations", cid + ".json")
            if os.path.exists(path):
                old = json.load(open(path, encoding="utf-8"))
                if (old.get("update_time") or 0) > (c.get("update_time") or 0):
                    continue   # an older export than the one already filed
            write_json(path, c)
            n += 1
    return n, uses


def file_rest(z, archive, uses):
    """Images -> logs/images; audio and video listed only; the rest -> one tar.zst. Returns files.jsonl rows."""
    names = {}
    if "conversation_asset_file_names.json" in z.namelist():
        names = {os.path.splitext(k)[0]: v for k, v in json.loads(z.read("conversation_asset_file_names.json")).items()}
    lib = {}
    if "library_files.json" in z.namelist():
        lib = {l.get("file_id"): l for l in json.loads(z.read("library_files.json"))}
    export = os.path.relpath(archive, LOGS)
    rows, jobs = [], {}
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool, open(archive + ".tmp", "wb") as out:
        zst = subprocess.Popen([full.tool("zstd"), "-19", "--long=27", "-T0", "-q", "-c"],
                               stdin=subprocess.PIPE, stdout=out)
        with tarfile.open(fileobj=zst.stdin, mode="w|") as tar:
            for info in z.infolist():
                name = info.filename
                if info.is_dir() or name.startswith("/") or ".." in name.split("/"):
                    continue
                if name in SKIP or _CHATS.match(name):
                    continue
                with z.open(info) as src:
                    head = src.read(32)
                fid = os.path.splitext(name)[0] if name.endswith(".dat") else None
                k = kind(head) if fid else "file"
                row = None
                if fid:
                    l = lib.get(fid, {})
                    chats = set(uses.get(fid, [])) | ({l["origination_thread_id"]} if l.get("origination_thread_id") else set())
                    row = {"id": fid, "name": names.get(fid) or l.get("file_name"), "kind": k,
                           "uploaded": l.get("created_at"), "chats": sorted(chats),
                           "bytes": info.file_size}
                    rows.append(row)
                if k == "image":
                    raw = z.read(info)
                    sha = hashlib.sha256(raw).hexdigest()
                    path = os.path.join(LOGS, "images", sha[:2], sha[:32] + ".avif")   # full.py's id
                    if path not in jobs.values():   # the same image twice
                        jobs[pool.submit(full.to_avif, raw, path)] = path
                    row.update(sha256=sha, stored={"file": os.path.relpath(path, LOGS)})
                elif k in ("audio", "video"):
                    row["stored"] = None
                else:
                    ti = tarfile.TarInfo(name)
                    ti.size = info.file_size
                    h = hashlib.sha256()
                    with z.open(info) as src:
                        tar.addfile(ti, _Hashing(src, h))
                    if row:
                        row.update(sha256=h.hexdigest(), stored={"export": export, "member": name})
        zst.stdin.close()
        if zst.wait():
            raise RuntimeError("zstd failed")
        for f in concurrent.futures.as_completed(jobs):
            try:
                f.result()
            except subprocess.CalledProcessError:
                stored = os.path.relpath(jobs[f], LOGS)
                for r in rows:
                    if (r.get("stored") or {}).get("file") == stored:
                        print("couldn't convert %s" % r["id"], file=sys.stderr)
                        r["stored"] = None
    os.replace(archive + ".tmp", archive)
    return rows


class _Hashing:
    """A file object that hashes what's read through it."""

    def __init__(self, f, h):
        self.f, self.h = f, h

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b


def merge_rows(rows):
    jl = os.path.join(OUT, "files.jsonl")
    old = {}
    if os.path.exists(jl):
        for line in open(jl, encoding="utf-8"):
            r = json.loads(line)
            old[r["id"]] = r
    for r in rows:
        old[r["id"]] = r
    with open(jl + ".tmp", "w", encoding="utf-8") as f:
        for r in sorted(old.values(), key=lambda r: (r.get("uploaded") or "", r["id"])):
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(jl + ".tmp", jl)


def list_templates():
    """templates.json: every project and custom GPT the filed chats ran in, keeping names."""
    path = os.path.join(OUT, "templates.json")
    old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    by = {}
    for f in glob.glob(os.path.join(OUT, "conversations", "*.json")):
        c = json.load(open(f, encoding="utf-8"))
        if c.get("conversation_template_id"):
            by.setdefault(c["conversation_template_id"], []).append(c)
    day = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat() if t else None
    out = {}
    for tid, cs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        cs.sort(key=lambda c: c.get("create_time") or 0)
        out[tid] = {"name": (old.get(tid) or {}).get("name"),
                    "kind": "project" if tid.startswith("g-p-") else "gpt", "chats": len(cs),
                    "first": day(cs[0].get("create_time")),
                    "last": day(max(c.get("update_time") or c.get("create_time") or 0 for c in cs)),
                    "url": "https://chatgpt.com/g/%s%s" % (tid, "/project" if tid.startswith("g-p-") else ""),
                    "lastChat": "https://chatgpt.com/c/" + cs[-1]["id"],
                    "titles": [c.get("title") for c in cs]}
    for tid in old.keys() - out.keys():
        out[tid] = old[tid]   # keep a name even if its chats are gone
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(path + ".tmp", path)
    return len(out), [t for t in out.values() if not t.get("name")]


def main():
    if len(sys.argv) != 2 or not sys.argv[1].endswith(".zip"):
        sys.exit(__doc__)
    if full.missing_tools():
        sys.exit("needs %s (brew install zstd imagemagick ffmpeg)" % ", ".join(full.missing_tools()))
    with zipfile.ZipFile(sys.argv[1]) as z:
        m = _DATE.search(os.path.basename(sys.argv[1]))
        date = m.group(1) if m else datetime.date(*max(i.date_time for i in z.infolist())[:3]).isoformat()
        n, uses = file_chats(z, sorted(n for n in z.namelist() if _CHATS.match(n)))
        print("%d chats" % n)
        n, unnamed = list_templates()
        print("%d projects and custom GPTs, %d without a name -> %s" % (n, len(unnamed), os.path.join(OUT, "templates.json")))
        for t in unnamed:
            print("  a %s with %d chats (%s): %s or its latest chat, %s" % (
                t["kind"], t["chats"], t["titles"][-1], t.get("url"), t.get("lastChat")))
        archive = os.path.join(OUT, "exports", date, "files.tar.zst")
        rows = file_rest(z, archive, uses)
        merge_rows(rows)
        count = lambda k: sum(1 for r in rows if r["kind"] == k)
        print("%d images -> logs/images, %d audio and video files not kept, the rest -> %s (%.0f MB)" % (
            count("image"), count("audio") + count("video"), archive, os.path.getsize(archive) / 1e6))
    sys.stdout.flush()   # so the index's line prints last
    subprocess.run([sys.executable, os.path.join(HERE, "index.py")], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
