"""The full archive: every session line, losslessly, with images stored once.

For each session JSONL, writes

    logs/<machine>/full/<rel>.jsonl.zst    every line, each embedded image
                                           replaced by "blob:<id>"
    logs/<machine>/full/<rel>.images.jsonl one line per image: when, where,
                                           which tool call produced it, and
                                           where its pixels are stored
    logs/images/<id[:2]>/<id>.avif         screenshots and pasted images,
                                           shared by all machines
    logs/<machine>/full/<rel>.mp4          for a subagent transcript, its images
                                           as AV1 frames (manuscript crops read
                                           in sequence compress ~3x better as
                                           video than as separate files)

Images are lossy (AVIF q50 / AV1 crf 38, long edge at most 1600 px); text is
not. Needs zstd, ImageMagick and ffmpeg (Homebrew); without them this tier is
skipped and the skeletons still run.
"""

import base64
import binascii
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile

MAX_EDGE = 1600
AVIF_QUALITY = "50"
AV1_CRF = "38"
MIN_FRAMES_FOR_VIDEO = 3

_B64 = re.compile(r"[A-Za-z0-9+/\r\n]+={0,2}")
_DATA_URL = re.compile(r"data:(image/[a-z+.-]+);base64,(.*)", re.S)
_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin"]


def tool(name):
    return shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", "")] + _EXTRA_PATH))


def missing_tools():
    return [t for t in ("zstd", "magick", "ffmpeg") if not tool(t)]


def image_kind(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", struct.unpack(">II", raw[16:24])
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            if raw[i + 1] in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return "jpeg", (w, h)
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
        return "jpeg", (0, 0)
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", (0, 0)
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", struct.unpack("<HH", raw[6:10])
    return None, (0, 0)


def decode_image(s):
    """The image bytes inside a string, if the string is one; else None."""
    if len(s) < 1000:
        return None
    m = _DATA_URL.match(s)
    body = m.group(2) if m else s
    if not m and (s[:4] not in ("iVBO", "/9j/", "UklG", "R0lG") or not _B64.fullmatch(s[:4096])):
        return None
    try:
        raw = base64.b64decode(body)
    except (binascii.Error, ValueError):
        return None
    return raw if image_kind(raw)[0] else None


class Session:
    """Rewrites one session's lines, collecting the images it holds."""

    def __init__(self, project, app):
        self.project = project
        self.app = app        # "claude" or "codex"
        self.images = {}      # id -> raw bytes
        self.index = []       # one entry per reference
        self.calls = {}       # tool_use id -> (name, short input)
        self.ctx = {}
        self.seen_here = set()

    def line(self, r):
        self.note(r)
        msg = r.get("message") if isinstance(r.get("message"), dict) else None
        # A tool result's image can appear twice: in message.content and again
        # in toolUseResult. Both belong to the record's tool call.
        call = next((b.get("tool_use_id") for b in (msg or {}).get("content") or []
                     if isinstance(b, dict) and b.get("type") == "tool_result"), None) \
            if isinstance((msg or {}).get("content"), list) else None
        self.seen_here = set()
        return self._walk(r, r, call)

    def note(self, r):
        for k in ("sessionId", "cwd"):
            if r.get(k):
                self.ctx[k] = r[k]
        p = r.get("payload") if isinstance(r.get("payload"), dict) else None
        if p and r.get("type") in ("session_meta", "turn_context"):   # Codex keeps them here
            if r.get("type") == "session_meta" and p.get("id"):
                self.ctx["sessionId"] = p["id"]
            if p.get("cwd"):
                self.ctx["cwd"] = p["cwd"]
        msg = r.get("message") if isinstance(r.get("message"), dict) else None
        for b in (msg or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                short = {k: v for k, v in inp.items() if isinstance(v, str) and len(v) <= 300}
                self.calls[b.get("id")] = (b.get("name"), short)
        if p and p.get("type") in ("function_call", "custom_tool_call"):
            self.calls[p.get("call_id")] = (p.get("name"), {"input": str(p.get("arguments") or p.get("input"))[:300]})

    def _walk(self, o, rec, call):
        if isinstance(o, dict):
            call = o.get("tool_use_id") or o.get("call_id") or call
            return {k: self._walk(v, rec, call) for k, v in o.items()}
        if isinstance(o, list):
            return [self._walk(v, rec, call) for v in o]
        if isinstance(o, str):
            raw = decode_image(o)
            if raw is not None:
                return self._blob(raw, rec, call)
        return o

    def _blob(self, raw, rec, call):
        bid = hashlib.sha256(raw).hexdigest()[:32]
        fmt, (w, h) = image_kind(raw)
        self.images.setdefault(bid, raw)
        if bid in self.seen_here:   # same image twice in one record
            return "blob:" + bid
        self.seen_here.add(bid)
        name, inp = self.calls.get(call, (None, None))
        typ = rec.get("type")
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        pasted = (typ == "user" and call is None) or (p.get("type") == "message" and p.get("role") == "user")
        self.index.append({
            "id": bid, "timestamp": rec.get("timestamp"), "app": self.app, "project": self.project,
            "sessionId": self.ctx.get("sessionId"), "cwd": self.ctx.get("cwd"),
            "source": "pasted" if pasted else "tool", "tool": name, "toolInput": inp,
            "format": fmt, "width": w, "height": h, "bytes": len(raw)})
        return "blob:" + bid


def zstd_write(text_path, dst):
    subprocess.run([tool("zstd"), "-q", "-f", "-T0", "-19", "--long=27", text_path, "-o", dst], check=True)


def to_avif(raw, dst):
    if os.path.exists(dst):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp.avif"
    subprocess.run([tool("magick"), "-", "-resize", "%dx%d>" % (MAX_EDGE, MAX_EDGE),
                    "-quality", AVIF_QUALITY, tmp], input=raw, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.replace(tmp, dst)


def to_video(images, dst, workdir):
    """images: [(id, raw)] in order. Frames are padded to one canvas, top-left."""
    sizes = []
    for i, (_, raw) in enumerate(images):
        f = os.path.join(workdir, "%05d.png" % i)
        subprocess.run([tool("magick"), "-", "-resize", "%dx%d>" % (MAX_EDGE, MAX_EDGE), f],
                       input=raw, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out = subprocess.run([tool("magick"), "identify", "-format", "%w %h", f],
                             check=True, stdout=subprocess.PIPE).stdout.split()
        sizes.append((int(out[0]), int(out[1])))
    W = max(w for w, _ in sizes) + 1 & ~1
    H = max(h for _, h in sizes) + 1 & ~1
    for i in range(len(images)):
        f = os.path.join(workdir, "%05d.png" % i)
        subprocess.run([tool("magick"), f, "-background", "white", "-gravity", "northwest",
                        "-extent", "%dx%d" % (W, H), f], check=True)
    tmp = dst + ".tmp.mp4"
    subprocess.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
                    "-i", os.path.join(workdir, "%05d.png"), "-c:v", "libsvtav1", "-crf", AV1_CRF,
                    "-preset", "6", "-g", "240", "-svtav1-params", "progress=0",
                    "-pix_fmt", "yuv420p10le", tmp], check=True,
                   env=dict(os.environ, SVT_LOG="1"))   # the encoder's own log: errors only
    os.replace(tmp, dst)
    return sizes


def archive(src, open_text, dst_base, images_dir, project, app, as_video, pool):
    """Write <dst_base>.jsonl.zst, <dst_base>.images.jsonl and, if as_video, <dst_base>.mp4."""
    os.makedirs(os.path.dirname(dst_base), exist_ok=True)
    s = Session(project, app)
    fd, text_tmp = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as out, open_text(src) as fin:
        for line in fin:
            if "base64" not in line:
                out.write(line if line.endswith("\n") else line + "\n")
                if any(k in line for k in ('"tool_use"', "function_call", "custom_tool_call",
                                           '"session_meta"', '"turn_context"')):
                    try:
                        s.note(json.loads(line))   # calls that later return images; Codex context
                    except ValueError:
                        pass
                continue
            try:
                r = json.loads(line)
            except ValueError:
                out.write(line if line.endswith("\n") else line + "\n")
                continue
            out.write(json.dumps(s.line(r), ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        zstd_write(text_tmp, dst_base + ".jsonl.zst")
    finally:
        os.unlink(text_tmp)

    video = as_video and len(s.images) >= MIN_FRAMES_FOR_VIDEO
    stored = {}
    if video:
        order = list(s.images.items())
        with tempfile.TemporaryDirectory() as wd:
            sizes = to_video(order, dst_base + ".mp4", wd)
        name = os.path.basename(dst_base) + ".mp4"
        for n, ((bid, _), (w, h)) in enumerate(zip(order, sizes)):
            stored[bid] = {"video": name, "frame": n, "frameWidth": w, "frameHeight": h}
    else:
        jobs = {}
        for bid, raw in s.images.items():
            path = os.path.join(images_dir, bid[:2], bid + ".avif")
            jobs[pool.submit(to_avif, raw, path)] = bid
            stored[bid] = {"file": os.path.relpath(path, os.path.dirname(images_dir))}
        for f in concurrent.futures.as_completed(jobs):
            f.result()

    with open(dst_base + ".images.jsonl.tmp", "w", encoding="utf-8") as f:
        for e in s.index:
            e["stored"] = stored.get(e["id"])
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(dst_base + ".images.jsonl.tmp", dst_base + ".images.jsonl")
    if not s.index:
        os.unlink(dst_base + ".images.jsonl")
    return len(s.images)
