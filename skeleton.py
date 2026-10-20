"""Skeletonizers for AI coding-session logs.

Each function takes one parsed JSONL record and returns a smaller record, or
None to drop it. What survives is what a look back over a month needs: who
said what and when, which project, which tools ran, and what a session was
called. Bulk (tool output, file snapshots, encrypted reasoning, images) goes.

Adapted from agentic-feedback-test's skeletonize.ts, which keeps only
type/message/timestamp. This keeps more per record (cwd, isSidechain, origin,
titles, artifacts, cost) because telling typed prompts from agent traffic
depends on them.
"""

MAX_TEXT = 20000            # a typed prompt or a reply, incl. pasted content
MAX_THINKING = 4000
MAX_TOOL_RESULT_OK = 200
MAX_TOOL_RESULT_ERR = 500
MAX_TOOL_ARG = 500
MAX_OTHER_ARG = 200         # string inputs of tools not listed below

TOOL_INPUT_KEEP = {
    "Bash": ["command", "description", "run_in_background"],
    "Read": ["file_path", "offset", "limit", "pages"],
    "Edit": ["file_path"],
    "Write": ["file_path"],
    "NotebookEdit": ["notebook_path"],
    "Glob": ["pattern", "path"],
    "Grep": ["pattern", "path", "glob", "type"],
    "Agent": ["description", "subagent_type", "prompt", "model"],
    "Task": ["description", "subagent_type", "prompt"],
    "Skill": ["skill", "args"],
    "WebFetch": ["url", "prompt"],
    "WebSearch": ["query"],
    "Artifact": ["action", "file_path", "url", "title", "description"],
    "AskUserQuestion": ["questions", "answers"],
    "ScheduleWakeup": ["delaySeconds", "reason"],
    "Workflow": ["name", "description"],
    "ExitPlanMode": ["plan"],
    "TodoWrite": ["todos"],
    "TaskCreate": ["subject", "description"],
    "TaskUpdate": ["taskId", "status", "subject"],
    "SendMessage": ["to", "message"],
    "SendUserFile": ["file_path", "caption"],
    "PushNotification": ["title", "message"],
    "Monitor": ["description", "command"],
    "CronCreate": ["schedule", "prompt"],
    "RemoteTrigger": ["action", "name", "prompt"],
}
# Inputs that are the person's or the plan's own words: cap at MAX_TEXT.
LONG_INPUTS = {"plan", "message"}

# Top-level fields kept on every Claude user/assistant record (most only
# appear when true or set).
CLAUDE_KEEP_TOP = [
    "type", "timestamp", "isSidechain", "isMeta", "isCompactSummary", "agentId",
    "origin", "promptSource", "attributionSkill", "attributionPlugin",
]
# Kept too, but written as a "context" record only when one changes.
CLAUDE_CONTEXT = ["sessionId", "cwd", "gitBranch", "version", "entrypoint", "permissionMode", "effort"]
# The structured side of a tool result (toolUseResult), where it says more
# than the text: a write that created or updated a file, a git push or PR, a
# command that was interrupted or timed out.
TOOL_RESULT_KEEP = ["userModified", "interrupted", "gitOperation", "returnCodeInterpretation",
                    "timedOutAfterMs", "backgroundTaskId", "dangerouslyDisableSandbox"]
USAGE_KEEP = ["input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens"]

def cap(s, n):
    if not isinstance(s, str) or len(s) <= n:
        return s
    return s[:n] + "... (truncated, %d chars)" % len(s)


def _image_note(block):
    src = block.get("source") or {}
    data = src.get("data") if isinstance(src, dict) else None
    media = src.get("media_type", "unknown") if isinstance(src, dict) else "unknown"
    return {"type": "text", "text": "[%s: %s, %d bytes]" % (
        block.get("type", "image"), media, len(data) if isinstance(data, str) else 0)}


def _tool_input(name, raw):
    if not isinstance(raw, dict):
        return None
    fields = TOOL_INPUT_KEEP.get(name)
    if fields is None:   # unknown or MCP tool: short scalar inputs only
        kept = {k: v for k, v in raw.items()
                if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= MAX_OTHER_ARG)}
        return kept or None
    kept = {}
    for f in fields:
        if f in raw:
            v = raw[f]
            n = MAX_TEXT if f in LONG_INPUTS else MAX_TOOL_ARG if f in ("command", "prompt") else 1000
            kept[f] = cap(v, n) if isinstance(v, str) else v
    return kept or None


def _result_content(content, is_error):
    n = MAX_TOOL_RESULT_ERR if is_error else MAX_TOOL_RESULT_OK
    if isinstance(content, str):
        return cap(content, n)
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append({"type": "text", "text": cap(b.get("text", ""), n)})
            elif b.get("type") in ("image", "document"):
                out.append(_image_note(b))
        return out
    return None

# --- Claude Code ------------------------------------------------------------


def _claude_tool_use_result(t):
    if not isinstance(t, dict):
        return None
    out = {k: t[k] for k in TOOL_RESULT_KEEP if t.get(k) not in (None, False, "", [], {})}
    if t.get("type") in ("create", "update"):   # Write
        out["type"] = t["type"]
    patch = t.get("structuredPatch")
    if isinstance(patch, list) and patch:
        lines = [x for h in patch if isinstance(h, dict) for x in h.get("lines") or [] if isinstance(x, str)]
        out["diff"] = cap("\n".join(lines), MAX_TOOL_ARG)
    elif t.get("type") == "create" and isinstance(t.get("content"), str):
        out["diff"] = cap(t["content"], MAX_TOOL_ARG)
    return out or None


def _claude_block(b):
    t = b.get("type")
    if t == "text":
        return {"type": "text", "text": cap(b.get("text", ""), MAX_TEXT)}
    if t == "thinking":
        txt = b.get("thinking") or ""
        return {"type": "thinking", "thinking": cap(txt, MAX_THINKING)} if txt else None
    if t in ("image", "document"):
        return _image_note(b)
    if t == "tool_use":
        out = {"type": "tool_use", "name": b.get("name", ""), "id": b.get("id")}
        inp = _tool_input(b.get("name", ""), b.get("input"))
        if inp:
            out["input"] = inp
        return out
    if t == "tool_result":
        out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id")}
        if b.get("is_error"):
            out["is_error"] = True
        out["content"] = _result_content(b.get("content"), bool(b.get("is_error")))
        return out
    return None   # redacted_thinking, server tool blocks, etc.


class Claude:
    """One Claude Code transcript. Stateful, so make one per file.

    Fields that rarely change (CLAUDE_CONTEXT) are written once, as a
    {"type": "context"} record, each time one of them changes; readers carry
    them forward. Titles are written only when they change, and a message's
    token usage only on its first record.
    """

    def __init__(self):
        self.ctx = {}
        self.titles = {}
        self.usage_seen = set()

    def __call__(self, r):
        t = r.get("type")
        if t in ("user", "assistant"):
            out = []
            ctx = {k: r[k] for k in CLAUDE_CONTEXT if r.get(k)}
            if ctx and any(self.ctx.get(k) != v for k, v in ctx.items()):
                self.ctx.update(ctx)
                out.append(dict(type="context", timestamp=r.get("timestamp"), **self.ctx))
            rec = {k: r[k] for k in CLAUDE_KEEP_TOP if r.get(k) not in (None, False, "")}
            m = r.get("message") or {}
            msg = {k: m[k] for k in ("role", "model") if k in m}
            c = m.get("content")
            if isinstance(c, str):
                msg["content"] = cap(c, MAX_TEXT)
            elif isinstance(c, list):
                msg["content"] = [x for x in (_claude_block(b) for b in c if isinstance(b, dict)) if x]
            u = m.get("usage")
            if isinstance(u, dict) and m.get("id") not in self.usage_seen:
                self.usage_seen.add(m.get("id"))
                msg["usage"] = {k: u[k] for k in USAGE_KEEP if k in u}
            rec["message"] = msg
            tur = _claude_tool_use_result(r.get("toolUseResult"))
            if tur:
                rec["toolUseResult"] = tur
            out.append(rec)
            return out
        if t in ("ai-title", "custom-title"):
            v = r.get("aiTitle") or r.get("customTitle")
            if v is None or self.titles.get(t) == v:
                return None
            self.titles[t] = v
            return {k: x for k, x in r.items() if isinstance(x, str)}
        if t == "summary":
            return {k: v for k, v in r.items() if isinstance(v, str)}
        if t == "frame-link":
            return {k: r.get(k) for k in ("type", "timestamp", "title", "frameUrl", "path")}
        if t == "cost-state":
            return {k: v for k, v in r.items() if not isinstance(v, (list, dict)) or k == "modelUsage"}
        if t == "queue-operation" and r.get("operation") == "enqueue":
            c = r.get("content")
            if not isinstance(c, str) or c.lstrip().startswith("<"):
                return None   # task notifications and other automated traffic
            return {"type": t, "timestamp": r.get("timestamp"), "content": cap(c, MAX_TEXT)}
        if t == "pr-link":
            return {k: v for k, v in r.items() if not isinstance(v, (list, dict))}
        if t == "attachment":
            return _claude_attachment(r.get("attachment") or {}, r.get("timestamp"))
        if t == "system":
            st = r.get("subtype")
            if st in ("compact_boundary", "away_summary", "bridge_status", "api_error"):
                return {k: cap(r.get(k), MAX_TOOL_RESULT_ERR) if k == "content" else r.get(k)
                        for k in ("type", "subtype", "timestamp", "content", "url") if k in r}
            if st == "turn_duration":
                return {k: r.get(k) for k in ("type", "subtype", "timestamp", "durationMs", "messageCount")}
            return None
        if t == "bridge-session" and not self.titles.get(t):
            self.titles[t] = True   # once per file: the session was reachable remotely
            return {"type": t, "bridgeSessionId": r.get("bridgeSessionId")}
        return None


def _claude_attachment(a, ts):
    """Attachments that say what the person did, compactly; the rest is harness."""
    at = a.get("type")
    base = {"type": "attachment:" + str(at), "timestamp": ts or a.get("timestamp")}
    if at == "queued_command":
        # A message typed while the agent was busy. It never becomes a user
        # record of its own, so it is a typed prompt in its own right.
        p = a.get("prompt")
        base.update(prompt=cap(p if isinstance(p, str) else str(p), MAX_TEXT),
                    origin=a.get("origin"), commandMode=a.get("commandMode"))
        return base
    if at == "edited_text_file":   # the person edited a file outside the session
        base.update(filename=a.get("filename"), snippet=cap(a.get("snippet"), MAX_TOOL_RESULT_ERR))
        return base
    if at in ("file", "compact_file_reference", "directory", "pdf_reference", "selected_lines_in_ide",
              "opened_file_in_ide", "plan_file_reference"):
        base.update({k: a.get(k) for k in ("filename", "displayPath", "path", "lineStart", "lineEnd")
                     if a.get(k) is not None})
        return base
    if at == "invoked_skills":
        base["skills"] = [s.get("name") for s in a.get("skills") or [] if isinstance(s, dict)]
        return base
    if at == "task_status":
        sh = a.get("shell") or {}
        base.update(description=a.get("description"), status=a.get("status"), taskType=a.get("taskType"),
                    command=cap(sh.get("command"), MAX_TOOL_ARG) if sh.get("command") else None)
        return base
    if at in ("todo", "todo_reminder", "task_reminder"):
        items = a.get("content") or a.get("todos") or a.get("tasks")
        if isinstance(items, list):
            base["items"] = [{k: i.get(k) for k in ("content", "subject", "status") if k in i}
                             for i in items if isinstance(i, dict)]
            return base
    return None


def claude_history(r):
    """One line of ~/.claude/history.jsonl: the prompt log."""
    out = {k: r.get(k) for k in ("display", "timestamp", "project", "sessionId") if k in r}
    out["display"] = cap(out.get("display"), MAX_TEXT)
    pc = r.get("pastedContents")
    if isinstance(pc, dict) and pc:
        out["pastedContents"] = {k: cap((v or {}).get("content") if isinstance(v, dict) else v, 2000)
                                 for k, v in pc.items()}
    return out

# --- Codex ------------------------------------------------------------------

CODEX_META_KEEP = ["id", "session_id", "timestamp", "cwd", "originator", "cli_version",
                   "source", "model_provider", "git", "thread_name", "thread_source",
                   "parent_thread_id"]
CODEX_EVENTS = {"task_started", "task_complete", "turn_aborted", "user_message",
                "agent_message", "token_count", "item_completed"}
# Small structured fields kept on events: what started a turn, why it failed.
CODEX_EVENT_DICTS = {"turn_attribution", "error"}


def _ms(d):
    """Codex durations are {"secs", "nanos"}; return milliseconds."""
    if isinstance(d, dict) and "secs" in d:
        return int(d["secs"] * 1000 + d.get("nanos", 0) // 1000000)
    return d


def _codex_item(it):
    """A completed item. Newer Codex reports what happened in a turn this way.
    Messages, reasoning and compactions repeat response_items and are dropped;
    the rest say what was typed, run, changed, searched or viewed."""
    t = it.get("type")
    out = {"type": t}
    if t == "UserMessage":   # what the person typed, wrapped in any ambient context
        out["content"] = _codex_text_blocks(it.get("content"))
        return out
    if t == "FileChange":
        out["status"] = it.get("status")
        out["changes"] = {path: {"type": c.get("type"),
                                 "diff": cap(c.get("unified_diff") or c.get("content"), MAX_TOOL_ARG)}
                          for path, c in (it.get("changes") or {}).items() if isinstance(c, dict)}
        return out
    if t == "CommandExecution":
        cmd = it.get("command")
        if isinstance(cmd, list):   # ["/bin/zsh", "-lc", "<the command>"]
            cmd = cmd[-1] if len(cmd) == 3 and cmd[1] in ("-lc", "-c") else " ".join(map(str, cmd))
        out.update(command=cap(cmd, MAX_TOOL_ARG), cwd=it.get("cwd"), exitCode=it.get("exit_code"),
                   status=it.get("status"), durationMs=_ms(it.get("duration")))
        return out
    if t == "McpToolCall":
        out.update(server=it.get("server"), tool=it.get("tool"), status=it.get("status"),
                   durationMs=_ms(it.get("duration")), arguments=_tool_input(None, it.get("arguments")))
        return out
    if t == "Extension":   # web search, image generation, sleep
        a = it.get("action") if isinstance(it.get("action"), dict) else {}
        out.update(kind=it.get("kind"), status=it.get("status"), query=cap(it.get("query"), MAX_TOOL_ARG),
                   action={k: cap(v, MAX_TOOL_ARG) for k, v in a.items() if isinstance(v, str)} or None,
                   savedPath=it.get("savedPath"), revisedPrompt=cap(it.get("revisedPrompt"), MAX_TOOL_ARG))
        return {k: v for k, v in out.items() if v is not None}
    if t == "ImageView":
        out["path"] = it.get("path")
        return out
    return None


def _codex_text_blocks(content):
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("input_text", "output_text", "text"):
            out.append({"type": b["type"], "text": cap(b.get("text", ""), MAX_TEXT)})
        elif "image" in str(b.get("type")):
            out.append({"type": "text", "text": "[%s%s]" % (b.get("type"), ": " + b["path"] if b.get("path") else "")})
    return out


class Codex:
    """One Codex rollout. Token counts arrive after every model call; only the
    last one before each task_complete (and at end of file) is kept."""

    def __init__(self):
        self.tokens = None

    def __call__(self, r):
        out = _codex(r)
        if out is None:
            return None
        et = (out.get("payload") or {}).get("type")
        if et == "token_count":
            self.tokens = out
            return None
        if et == "task_complete" and self.tokens:
            out, self.tokens = [self.tokens, out], None
        return out

    def flush(self):
        t, self.tokens = self.tokens, None
        return t


def _codex(r):
    t = r.get("type")
    p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
    base = {"type": t, "timestamp": r.get("timestamp")}
    if t == "session_meta":
        base["payload"] = {k: p[k] for k in CODEX_META_KEEP if k in p}
        return base
    if t == "turn_context":
        q = {k: p[k] for k in ("turn_id", "cwd", "model", "effort", "approval_policy") if k in p}
        for k in ("sandbox_policy", "collaboration_mode"):
            v = p.get(k)
            q[k] = (v.get("type") or v.get("mode")) if isinstance(v, dict) else v
        base["payload"] = {k: v for k, v in q.items() if v is not None}
        return base
    if t == "compacted":
        base["payload"] = {"message": cap(p.get("message"), MAX_TEXT)} if p.get("message") else {}
        return base
    if t == "event_msg":
        et = p.get("type")
        if et not in CODEX_EVENTS:
            return None
        if et == "token_count":
            info = p.get("info") or {}
            base["payload"] = {"type": et, "total_token_usage": info.get("total_token_usage")}
        elif et == "item_completed":
            item = _codex_item(p.get("item") or {})
            if item is None:
                return None
            base["payload"] = {"type": et, "turn_id": p.get("turn_id"), "item": item}
        else:
            base["payload"] = {k: (cap(v, MAX_TEXT) if isinstance(v, str) else v)
                               for k, v in p.items() if not isinstance(v, (list, dict)) or k in CODEX_EVENT_DICTS}
        return base
    if t != "response_item":
        return None
    it = p.get("type")
    if it == "message":
        if p.get("role") not in ("user", "assistant"):
            return None   # developer/system instructions
        base["payload"] = {"type": it, "role": p["role"], "content": _codex_text_blocks(p.get("content"))}
        if p.get("phase"):   # commentary (progress notes) or final_answer
            base["payload"]["phase"] = p["phase"]
        return base
    if it == "reasoning":
        summ = [s.get("text") for s in p.get("summary") or [] if isinstance(s, dict) and s.get("text")]
        if not summ:
            return None
        base["payload"] = {"type": it, "summary": [cap(s, MAX_THINKING) for s in summ]}
        return base
    if it in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
        q = {k: p[k] for k in ("type", "name", "namespace", "call_id", "status") if k in p}
        for k in ("arguments", "input"):
            if k in p:
                q[k] = cap(p[k] if isinstance(p[k], str) else str(p[k]), MAX_TOOL_ARG)
        if "action" in p:
            q["action"] = cap(str(p["action"]), MAX_TOOL_ARG)
        base["payload"] = q
        return base
    if it in ("function_call_output", "custom_tool_call_output"):
        out = p.get("output")
        if isinstance(out, list):
            out = _result_content([{"type": "text", "text": b.get("text", "")} for b in out
                                   if isinstance(b, dict)], False)
        else:
            out = cap(out if isinstance(out, str) else str(out), MAX_TOOL_RESULT_OK)
        base["payload"] = {"type": it, "call_id": p.get("call_id"), "output": out}
        return base
    if it == "compaction":
        base["payload"] = {"type": it}
        return base
    return None


class Passthrough:
    def __call__(self, r):
        return r


class ClaudeHistory:
    def __call__(self, r):
        return claude_history(r)
