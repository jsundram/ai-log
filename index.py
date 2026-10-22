#!/usr/bin/env python3
"""Build one index over every machine's skeletons, Claude and Codex alike.

    python3 index.py            # writes logs/index/{sessions,prompts,days}.jsonl

The skeletons keep each app's own record format. This reads both and writes
three files in one shape, for github-month-review and anything else that asks
"what was I doing that day":

    sessions.jsonl  one line per session, subagent, or claude.ai or ChatGPT chat: machine, app, title,
                    folder, start/end, typed prompts, hands-on minutes,
                    models, tokens, files changed, artifacts published
    prompts.jsonl   one line per typed prompt, oldest first
    days.jsonl      one line per local day: prompts, hands-on hours,
                    projects, apps, machines

The index is derived; it is rebuilt from scratch each time and never edited.
Run it on one machine at a time (it reads every machine's folder), so two
machines don't write it at once through Dropbox.
"""

import argparse
import collections
import datetime
import glob
import json
import os
import re
import sys
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = zoneinfo.ZoneInfo("America/New_York")
WINDOW = datetime.timedelta(minutes=15)   # each typed prompt counts as 15 hands-on minutes
CODE = os.path.join(os.path.expanduser("~"), "Dropbox", "Code") + os.sep
_REMOTE = re.compile(r"(?:cse_|session_)([A-Za-z0-9]{20,})")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}

_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?")


def parse_ts(t):
    if isinstance(t, (int, float)):   # history files: ms (Claude) or s (Codex) since 1970
        return datetime.datetime.fromtimestamp(t / 1000 if t > 1e11 else t, datetime.timezone.utc)
    m = _TS.match(t or "")
    if not m:
        return None
    frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
    tz = m.group(3) or "Z"
    tz = "+00:00" if tz == "Z" else tz
    return datetime.datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, tz))


def iso(d):
    return d.isoformat().replace("+00:00", "Z") if d else None


def project_of(cwd):
    """~/Dropbox/Code/<repo>/... -> <repo>; anything else as a ~ path."""
    if not cwd:
        return None
    cwd = cwd[7:] if cwd.startswith("file://") else cwd
    if cwd.startswith(CODE):
        return cwd[len(CODE):].split(os.sep)[0]
    home = os.path.expanduser("~")
    return "~" + cwd[len(home):] if cwd.startswith(home) else cwd


def hands_on(times):
    """Union of a 15-minute window after each prompt, in minutes."""
    total = datetime.timedelta()
    end = None
    for t in sorted(times):
        if end is None or t >= end:
            total += WINDOW
        else:
            total += t + WINDOW - end
        end = t + WINDOW
    return round(total.total_seconds() / 60)


def cap(s, n):
    return s if len(s) <= n else s[:n] + "…"


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") in
                   ("text", "input_text", "output_text"))


def records(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


class Session:
    def __init__(self, machine, app, path):
        self.machine, self.app, self.path = machine, app, path
        self.id = self.parent = self.title = self.cwd = None
        self.subagent = False
        self.times = []
        self.prompts = []           # (time, kind, text)
        self.models = collections.Counter()
        self.tokens = collections.Counter()
        self.cost = None
        self.files = []
        self.artifacts = []
        self.attachments = []       # chats: names of the files you attached
        self.template = None        # ChatGPT: the project or custom GPT a chat ran in
        self.origin = None          # Claude entrypoint / Codex originator
        self.also = []              # other machines holding an identical copy
        self.git = []               # pushes and pull requests
        self.remote = []            # claude.ai session ids, as in commits' Claude-Session lines

    def stamp(self, r):
        t = parse_ts(r.get("timestamp"))
        if t:
            self.times.append(t)
        return t

    def prompt(self, t, kind, text):
        if t and text.strip():
            self.prompts.append((t, kind, text))

    def attached(self, name):
        if name and name not in self.attachments:
            self.attachments.append(name)

    def changed(self, path):
        if path and path not in self.files:
            self.files.append(path)

    def row(self):
        typed = [t for t, _, _ in self.prompts]
        return {
            "machine": self.machine, "alsoOn": self.also or None, "app": self.app, "id": self.id, "parent": self.parent,
            "kind": "subagent" if self.subagent else "main", "title": self.title,
            "cwd": self.cwd, "project": project_of(self.cwd), "origin": self.origin,
            "start": iso(min(self.times)) if self.times else None,
            "end": iso(max(self.times)) if self.times else None,
            "prompts": len(typed), "handsOnMinutes": hands_on(typed),
            "models": [m for m, _ in self.models.most_common()],
            "tokens": dict(self.tokens) or None, "costUSD": self.cost,
            "filesChanged": self.files, "artifacts": self.artifacts, "attachments": self.attachments,
            "template": self.template, "git": self.git,
            "remoteIds": self.remote,
            "skeleton": os.path.relpath(self.path, os.path.join(HERE, "logs")),
        }


# --- Claude -----------------------------------------------------------------

def claude_session(machine, path):
    s = Session(machine, "claude", path)
    parts = path.split(os.sep)
    s.id = os.path.splitext(parts[-1])[0]
    if "subagents" in parts:   # <session>/subagents/[workflows/wf_*/]agent-*.jsonl
        s.subagent = True
        s.parent = parts[parts.index("subagents") - 1]
    titles = {}
    for r in records(path):
        t = r.get("type")
        ts = s.stamp(r)
        if t == "context":
            s.cwd = s.cwd or r.get("cwd")
            s.origin = s.origin or r.get("entrypoint")
        elif t in ("ai-title", "custom-title"):
            titles[t] = r.get("aiTitle") or r.get("customTitle")
        elif t in ("bridge-session", "attachment:remote_session_change", "cloud-session"):
            m = _REMOTE.search(r.get("bridgeSessionId") or r.get("url") or "")
            if m and m.group(1) not in s.remote:
                s.remote.append(m.group(1))
        elif t == "frame-link":
            s.artifacts.append({"title": r.get("title"), "url": r.get("frameUrl")})
        elif t == "cost-state" and r.get("totalCostUSD") is not None:
            s.cost = round(r["totalCostUSD"], 4)
        elif t == "attachment:queued_command":
            p = r.get("prompt") or ""
            kind = (r.get("origin") or {}).get("kind")
            if not s.subagent and not p.lstrip().startswith("<") and kind in (None, "human"):
                s.prompt(ts, "queued", p)
        elif t in ("user", "assistant"):
            m = r.get("message") or {}
            if t == "assistant":
                if m.get("model") and m["model"] != "<synthetic>":
                    s.models[m["model"]] += 1
                u = m.get("usage") or {}
                for k, n in (("input_tokens", "input"), ("output_tokens", "output"),
                             ("cache_read_input_tokens", "cacheRead"),
                             ("cache_creation_input_tokens", "cacheWrite")):
                    if isinstance(u.get(k), int):
                        s.tokens[n] += u[k]
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                        s.changed((b.get("input") or {}).get(EDIT_TOOLS[b["name"]]))
            elif (r.get("toolUseResult") or {}).get("gitOperation"):
                op = r["toolUseResult"]["gitOperation"]
                if op not in s.git:
                    s.git.append(op)
            elif not (r.get("isSidechain") or r.get("isMeta") or r.get("isCompactSummary")):
                human = (r.get("origin") or {}).get("kind") == "human" or r.get("promptSource") == "typed"
                txt = text_of(m.get("content"))
                if human and not txt.lstrip().startswith("<"):
                    s.prompt(ts, "typed", txt)
    s.title = titles.get("custom-title") or titles.get("ai-title")
    return s


# --- Codex ------------------------------------------------------------------

_GIT = re.compile(r"\b(?:git (push|commit)|gh (pr) create)\b")
_AMBIENT = re.compile(r"<in-app-browser-context\b.*?</in-app-browser-context>\s*", re.S)


def codex_prompt_text(txt):
    """What the person typed, without the context Codex wraps it in; None if
    the message wasn't typed (automation heartbeats, answers to questions)."""
    s = txt.lstrip()
    if s.startswith("<heartbeat>") or s.startswith("<send_user_message_question_reply>"):
        return None
    if "## My request:" in s:   # "# Files mentioned by the user" / "# Browser comments" preambles
        s = s.split("## My request:", 1)[1]
    s = _AMBIENT.sub("", s).strip()
    return None if not s or s.startswith("<") else s


def codex_session(machine, path, names):
    s = Session(machine, "codex", path)
    s.id = os.path.basename(path)[-41:-6]   # rollout-<time>-<id>.jsonl
    last_tokens = None
    for r in records(path):
        t = r.get("type")
        p = r.get("payload") or {}
        ts = s.stamp(r)
        if t == "session_meta":
            s.id = p.get("id") or s.id
            s.cwd = p.get("cwd")
            s.origin = p.get("originator")
            src = p.get("source")
            if isinstance(src, dict) and "subagent" in src:
                s.subagent = True
                s.parent = p.get("parent_thread_id")
        elif t == "turn_context" and p.get("model"):
            s.models[p["model"]] += 1
        elif t == "event_msg" and p.get("type") == "token_count":
            last_tokens = p.get("total_token_usage") or last_tokens
        elif t == "event_msg" and p.get("type") == "item_completed":
            it = p.get("item") or {}
            if it.get("type") == "UserMessage" and not s.subagent:
                txt = codex_prompt_text(text_of(it.get("content")))
                if txt:
                    s.prompt(ts, "typed", txt)
            elif it.get("type") == "FileChange":
                for f in it.get("changes") or {}:
                    s.changed(f)
            elif it.get("type") == "CommandExecution" and it.get("exitCode") == 0:
                m = _GIT.search(it.get("command") or "")
                if m:   # Codex has no structured git record; keep the command
                    s.git.append({m.group(1) or m.group(2): {"command": cap(it["command"], 200)}})
        elif t == "event_msg" and p.get("type") == "user_message" and not s.subagent:
            # Older Codex: no completed items, the typed prompt is this event.
            txt = codex_prompt_text(p.get("message") or "")
            if txt and not any(x[2] == txt for x in s.prompts):
                s.prompt(ts, "typed", txt)
    if last_tokens:
        for k, n in (("input_tokens", "input"), ("output_tokens", "output"),
                     ("cached_input_tokens", "cacheRead"), ("cache_write_input_tokens", "cacheWrite"),
                     ("reasoning_output_tokens", "reasoning")):
            if isinstance(last_tokens.get(k), int):
                s.tokens[n] = last_tokens[k]
    s.title = names.get(s.id)
    return s


# --- prompt logs, for sessions whose transcripts are gone --------------------

# --- claude.ai chats ---------------------------------------------------------

def chat_session(path):
    """A claude.ai chat, from chats.py's copy of the data export."""
    c = json.load(open(path, encoding="utf-8"))
    s = Session("claude.ai", "claude.ai", path)
    s.id, s.title, s.origin = c.get("uuid"), c.get("name") or None, "chat"
    for m in c.get("chat_messages") or []:
        t = parse_ts(m.get("created_at"))
        if t:
            s.times.append(t)
        if m.get("sender") == "human":
            text = m.get("text") or "\n".join(b.get("text") or "" for b in m.get("content") or []
                                               if b.get("type") == "text")
            s.prompt(t, "typed", text)
            for f in (m.get("files") or []) + (m.get("attachments") or []):
                s.attached(f.get("file_name"))
        # Artifacts made in the chat, by the artifacts tool; one entry per id,
        # with its latest title. They have no link: only published ones do.
        for b in m.get("content") or []:
            inp = b.get("input") if b.get("type") == "tool_use" and b.get("name") == "artifacts" else None
            if isinstance(inp, dict) and inp.get("id"):
                a = next((a for a in s.artifacts if a["id"] == inp["id"]), None)
                if a is None:
                    a = {"id": inp["id"], "title": None}
                    s.artifacts.append(a)
                a["title"] = inp.get("title") or a["title"]
    return s


# --- ChatGPT chats ----------------------------------------------------------

def chatgpt_session(path, templates):
    """A ChatGPT chat, from chatgpt.py's copy of the data export."""
    c = json.load(open(path, encoding="utf-8"))
    s = Session("chatgpt", "chatgpt", path)
    s.id, s.title, s.origin = c.get("id"), c.get("title") or None, "chat"
    tid = c.get("conversation_template_id")
    if tid:   # the export has no names for these, only ids
        s.template = {"kind": "project" if tid.startswith("g-p-") else "gpt", "id": tid,
                      "name": (templates.get(tid) or {}).get("name")}   # from chatgpt.py's templates.json
    # mapping holds every branch; a prompt you edited is still one you typed
    for node in (c.get("mapping") or {}).values():
        m = node.get("message") or {}
        t = parse_ts(m.get("create_time"))
        if not t:
            continue
        s.times.append(t)
        role, content = (m.get("author") or {}).get("role"), m.get("content") or {}
        if role == "assistant" and (m.get("metadata") or {}).get("model_slug"):
            s.models[m["metadata"]["model_slug"]] += 1
        if role != "user" or content.get("content_type") not in ("text", "multimodal_text"):
            continue   # user_editable_context is custom instructions, not a prompt
        for f in (m.get("metadata") or {}).get("attachments") or []:
            s.attached(f.get("name"))
        parts = content.get("parts") or []
        spoken = [p.get("text") or "" for p in parts if isinstance(p, dict)
                  and p.get("content_type") == "audio_transcription" and p.get("direction") == "in"]
        typed = "\n".join(p for p in parts if isinstance(p, str))
        s.prompt(t, "voice" if spoken and not typed.strip() else "typed", typed if typed.strip() else "\n".join(spoken))
    return s


def history_prompts(machine, app, path, known):
    out = []
    for r in records(path):
        sid = r.get("sessionId") or r.get("session_id")
        txt = r.get("display") if app == "claude" else r.get("text")
        t = parse_ts(r.get("timestamp") if app == "claude" else r.get("ts"))
        if not t or not isinstance(txt, str) or sid in known:
            continue
        if txt.lstrip().startswith(("/", "<")) or txt.strip() in ("exit", "quit"):
            continue   # slash commands and exits
        out.append({"machine": machine, "app": app, "session": sid,
                    "cwd": r.get("project"), "time": t, "kind": "history", "text": txt})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default=os.path.join(HERE, "logs"))
    a = ap.parse_args()

    sessions, prompts, histories = [], [], []
    for mdir in sorted(glob.glob(os.path.join(a.logs, "*", ""))):
        machine = os.path.basename(mdir.rstrip(os.sep))
        if machine in ("images", "index", "claude.ai", "chatgpt"):
            continue
        for f in glob.glob(os.path.join(mdir, "claude", "projects", "**", "*.jsonl"), recursive=True):
            sessions.append(claude_session(machine, f))
        names = {}
        idx = os.path.join(mdir, "codex", "session_index.jsonl")
        if os.path.exists(idx):
            for r in records(idx):
                names[r.get("id")] = r.get("thread_name")
        for f in glob.glob(os.path.join(mdir, "codex", "sessions", "*", "*.jsonl")):
            sessions.append(codex_session(machine, f, names))
        for app in ("claude", "codex"):
            h = os.path.join(mdir, app, "history.jsonl")
            if os.path.exists(h):
                histories.append((machine, app, h))

    for f in glob.glob(os.path.join(a.logs, "claude.ai", "conversations", "*.json")):
        sessions.append(chat_session(f))
    tl = os.path.join(a.logs, "chatgpt", "templates.json")
    templates = json.load(open(tl, encoding="utf-8")) if os.path.exists(tl) else {}
    for f in glob.glob(os.path.join(a.logs, "chatgpt", "conversations", "*.json")):
        sessions.append(chatgpt_session(f, templates))

    # A session copied between machines (e.g. when ~/.claude was moved) shows up
    # in both folders. Keep the fuller copy and note where else it is.
    best = {}
    for s in sessions:
        k = (s.app, s.id)
        if k not in best:
            best[k] = s
            continue
        keep, drop = sorted([best[k], s], key=lambda x: (len(x.times), max(x.times, default=None) is not None), reverse=True)
        keep.also.append(drop.machine)
        best[k] = keep
    sessions = list(best.values())

    # A forked session starts with a copy of its parent's messages, timestamps
    # and all. Count each prompt once, in the session where it first appears.
    seen = set()
    far = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    for s in sorted(sessions, key=lambda s: min(s.times, default=far)):
        mine = []
        for p in s.prompts:
            k = (s.app, p[0], p[2][:200])
            if k not in seen:
                seen.add(k)
                mine.append(p)
        s.prompts = mine

    known = {s.id for s in sessions}
    for machine, app, h in histories:
        hp = history_prompts(machine, app, h, known)
        prompts += hp
        known |= {p["session"] for p in hp}   # the same prompt log can be on two machines

    for s in sessions:
        for t, kind, txt in s.prompts:
            prompts.append({"machine": s.machine, "app": s.app, "session": s.id, "cwd": s.cwd,
                            "time": t, "kind": kind, "text": txt})
    prompts.sort(key=lambda p: p["time"])

    days = collections.OrderedDict()
    for p in prompts:
        local = p["time"].astimezone(TZ)
        d = days.setdefault(local.date().isoformat(), {
            "times": [], "projects": collections.Counter(), "apps": collections.Counter(),
            "machines": collections.Counter(), "sessions": set(), "first": local, "last": local})
        d["times"].append(p["time"])
        d["projects"][project_of(p["cwd"]) or "?"] += 1
        d["apps"][p["app"]] += 1
        d["machines"][p["machine"]] += 1
        d["sessions"].add(p["session"])
        d["last"] = local

    out = os.path.join(a.logs, "index")
    os.makedirs(out, exist_ok=True)

    def dump(name, rows):
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, os.path.join(out, name))

    sessions.sort(key=lambda s: min(s.times, default=far))
    dump("sessions.jsonl", (s.row() for s in sessions))
    dump("prompts.jsonl", ({"time": iso(p["time"]), "local": p["time"].astimezone(TZ).isoformat(timespec="seconds"),
                            "machine": p["machine"], "app": p["app"], "session": p["session"],
                            "project": project_of(p["cwd"]), "kind": p["kind"], "text": p["text"]}
                           for p in prompts))
    dump("days.jsonl", ({"date": k, "prompts": len(d["times"]), "handsOnHours": round(hands_on(d["times"]) / 60, 1),
                         "first": d["first"].strftime("%H:%M"), "last": d["last"].strftime("%H:%M"),
                         "sessions": len(d["sessions"]), "projects": dict(d["projects"].most_common()),
                         "apps": dict(d["apps"]), "machines": dict(d["machines"])}
                        for k, d in days.items()))
    print("%d sessions, %d prompts, %d days -> %s" % (len(sessions), len(prompts), len(days), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
