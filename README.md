# ai-log

An archive of AI coding sessions from every machine, small enough to live in
Dropbox. Each machine copies its Claude Code and Codex logs here, stripped of
bulk, once a day. `github-month-review` reads it to see the work GitHub
doesn't show.

`pipeline.html` explains how it works, with a diagram (open it in a browser).

```
logs/<machine>/claude/projects/<project>/<session>.jsonl     main transcript
logs/<machine>/claude/projects/<project>/<session>/subagents/…  subagent + workflow agents
logs/<machine>/claude/history.jsonl                           prompt log
logs/<machine>/claude/plans, tasks, todos; projects/*/memory  copied as they are
logs/<machine>/codex/sessions/<YYYY-MM>/rollout-….jsonl       sessions and their subagents
logs/<machine>/codex/history.jsonl, session_index.jsonl       prompt log, thread names
logs/<machine>/codex/automations, visualizations              copied as they are
logs/<machine>/full/…                                         the full archive (below)
logs/index/sessions.jsonl, prompts.jsonl, days.jsonl          one index over all of it
```

`logs/` is gitignored. It holds everything you typed, so it never goes to GitHub.

## Running it

```sh
python3 ailog.py                 # this machine; only changed sessions are redone
python3 ailog.py --force         # redo everything (after changing skeleton.py)
python3 index.py                 # rebuild logs/index from every machine's skeletons
./install.sh                     # run ailog.py daily at 9 a.m. with launchd (once per machine)
./install.sh --remove
```

`--machine`, `--claude-dir` and `--codex-dir` point it elsewhere, e.g. at a
backup: `--machine dymaxion --claude-dir ~/Dropbox/Code/settings/dymaxion.backup/.claude --no-codex`.

Nothing in `logs/` is ever deleted. A session that ages out of `~/.claude`, or
a machine that's retired, stays archived.

No dependencies; it runs on macOS's own `/usr/bin/python3`.

## Reading it: start with the index

`index.py` reads both apps' skeletons on every machine and writes three files
in one shape. Most questions about a month can be answered from these alone;
each session row names its skeleton for anything deeper.

**`days.jsonl`**, one line per day in America/New_York:

```json
{"date": "2026-09-27", "prompts": 70, "handsOnHours": 8.6, "first": "00:03", "last": "23:45",
 "sessions": 4, "projects": {"gerard": 61, "mbta-glx": 9},
 "apps": {"claude": 70}, "machines": {"mini": 70}}
```

**`prompts.jsonl`**, one line per prompt you typed, oldest first: `time` (UTC),
`local`, `machine`, `app`, `session`, `project`, `kind`, `text`. `kind` is

- `typed`: a prompt you sent.
- `queued`: a message you sent while the agent was working (Claude only).
- `history`: from a prompt log, for a session whose transcript is gone. This
  covers October 2025 through March 2026, before transcripts were kept.

**`sessions.jsonl`**, one line per session or subagent: `machine`, `app`, `id`,
`parent`, `kind` (`main` or `subagent`), `title`, `cwd`, `project`, `origin`
(the CLI, the desktop app, …), `start`, `end`, `prompts`, `handsOnMinutes`,
`models`, `tokens`, `costUSD` (Claude only), `filesChanged`, `artifacts`,
`git` (commits, pushes, branches and PRs, with PR numbers and URLs), and
`skeleton`, the path of its skeleton under `logs/`.

The index makes three judgment calls. Change them in `index.py`.

- **Hands-on time** is the union of a 15-minute window after each prompt you
  typed. A day's hours are the union across every session, app and machine.
- **`project`** is the folder under `~/Dropbox/Code/`, or a `~` path for
  anything outside it.
- **Duplicates.** A session found on two machines is listed once, with
  `alsoOn` naming the other. A prompt copied into a forked session is counted
  in the session where it first appeared.

The index is derived and rebuilt from scratch in a few seconds. It isn't
rebuilt on a schedule: run `index.py` before reading it, on one machine at a
time, so two machines never write it at once through Dropbox.

## What the skeletons keep

`skeleton.py` keeps what you said, what the agent said, which tools ran on
which files, the session's title, published artifacts, cost, and token usage.
It drops tool output past 200 characters (500 for errors), file snapshots,
images, encrypted reasoning, and system instructions. That's about 1–4% of the
original size: 239 MB → 5 MB for the largest Claude session, 939 MB → 8 MB for
the largest Codex one.

Each app keeps its own record format, so reading a skeleton directly means
knowing both. Where the same thing lives in each:

| | Claude | Codex |
|---|---|---|
| Session id, folder, version | `{"type": "context"}` record, written when one changes; carry it forward | `session_meta`: `id`, `cwd`, `cli_version`, `originator` |
| Model, effort, permissions | `context.effort`, `context.permissionMode`; `message.model` on replies | `turn_context`: `model`, `effort`, `approval_policy`, `sandbox_policy` |
| Title | `ai-title` / `custom-title`, written when it changes | `thread_name` in `codex/session_index.jsonl`, by session id |
| A prompt you typed | `type: "user"`, `origin.kind == "human"` or `promptSource == "typed"`, not `isSidechain`, text not starting `<` | `item_completed` with `item.type == "UserMessage"`, in a session that isn't a subagent. Codex wraps it in browser context or attached-file lists; the text after `## My request:` is what you typed. `<heartbeat>` messages are automations |
| Sent while the agent worked | `attachment:queued_command` | — |
| Agent reply | `type: "assistant"`, text blocks | `response_item` `message`, `role: "assistant"`; `phase` is `commentary` or `final_answer` |
| Thinking | `thinking` blocks, 4,000 characters | `reasoning` summaries, 4,000 characters |
| Tool call and result | `tool_use` / `tool_result` blocks | `function_call` / `custom_tool_call` and their `_output` |
| Shell command | `Bash` tool call; its result's `toolUseResult` has `interrupted`, `timedOutAfterMs`, `returnCodeInterpretation` | `item.type == "CommandExecution"`: `command`, `cwd`, `exitCode`, `durationMs` |
| Git push, commit, PR | `toolUseResult.gitOperation`, e.g. `{"pr": {"number": 26, "url": …, "action": "created"}}` | only in the command text |
| File changed | `Edit` / `Write` tool calls, `input.file_path`; the result's `toolUseResult` has `type` (`create` / `update`), `userModified` and the first 500 characters of the `diff` | `item.type == "FileChange"`: `changes` by path, each `add` or `update` with the first 500 characters of the diff |
| Skill behind a reply | `attributionSkill`, `attributionPlugin` | — |
| Web search | `WebSearch` / `WebFetch` tool calls | `item.type == "Extension"`, `kind: "web.search"` |
| Image looked at | `Read` of an image file | `item.type == "ImageView"`: `path` |
| Subagent | file under `<session>/subagents/`; records have `isSidechain` | its own rollout; `session_meta.source.subagent` and `parent_thread_id` |
| What started a turn | — | `task_started.turn_attribution` (`guardian_review`, `app_update_resume`, …) |
| Turn length | `system` `turn_duration` | `task_complete.duration_ms` |
| Tokens | `message.usage` on a reply's first record | `token_count` just before each `task_complete`, a running total |
| Cost | `cost-state.totalCostUSD` | — |
| Published artifacts | `frame-link` | — |

## The full archive

Skeletons are lossy on purpose. Next to them, `full.py` keeps everything:

```
logs/<machine>/full/<path>.jsonl.zst     every line of the session, text lossless;
                                         each embedded image replaced by "blob:<id>"
logs/<machine>/full/<path>.images.jsonl  one line per image: time, app (claude or codex),
                                         project, session, folder,
                                         pasted or from a tool, the tool call that
                                         produced it (e.g. Read of out/board.png),
                                         original size, and where it is stored
logs/images/<id[:2]>/<id>.avif           screenshots and pasted images, stored once
                                         across all machines
logs/<machine>/full/<path>.mp4           a subagent's images as AV1 frames, in order;
                                         `stored.frame` in the index is the frame
```

Images are lossy: AVIF quality 50 or AV1 crf 38, long edge at most 1600 px.
Subagents get video because the /encode readers look at hundreds of near-identical
manuscript crops, which compress about three times better as frames than as files.
Read text back with `zstd -dc`; pull frame N with
`ffmpeg -i x.mp4 -vf "select=eq(n\,N)" -frames:v 1 out.png`.

Needs `zstd`, ImageMagick and `ffmpeg` (`brew install zstd imagemagick ffmpeg`).
Without them the full archive is skipped and the skeletons still run;
`--no-full` skips it on purpose.

## Redaction

Not done. `ailog.py` copies everything as it appears. `redact.py` is an
unreviewed first draft, known to have false positives; nothing runs it.

## Not covered

- Large tool outputs that Claude Code saves to side files
  (`<session>/tool-results/`), and its file-edit backups.
- Plans and memory are copied as they are now; earlier versions aren't kept.
- Codex's goals and memories databases. Both are empty so far.
- claude.ai and ChatGPT chats live on the providers' servers (the ChatGPT Mac
  app's local copy is encrypted). Both offer a data export; an importer for
  those would go here.
- Codex sessions on this Mac go back to September 2026, plus one from May.
