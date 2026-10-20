#!/usr/bin/env python3
"""Second pass over logs/: find, and optionally blank, things that look like secrets.

    python3 redact.py                 # report matches under logs/, change nothing
    python3 redact.py --apply         # rewrite matching files in place
    python3 redact.py logs/mini/claude/history.jsonl

ailog.py copies logs without redacting; run this when the archive is about to
go somewhere it shouldn't carry a key. Matching runs on parsed JSON strings,
so escapes inside the JSON don't hide anything. Reports never print a match
in full.
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("github", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("bearer", re.compile(r"(?<=Bearer )[A-Za-z0-9._~+/-]{20,}=*")),
]
# KEY=value / "key": "value" where the key names a secret; the key is kept.
ASSIGN = re.compile(
    r"""(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY)[A-Z0-9_]*)"""
    r"""(["']?\s*[:=]\s*["']?)([^\s"'&,;}$]{8,})""")


def redact(s, hits):
    for name, p in PATTERNS:
        def sub(m, name=name):
            hits.append((name, m.group(0)))
            return "[REDACTED]"
        s = p.sub(sub, s)

    def assign(m):
        v = m.group(3)
        # "tokens: 1234567890" is a count, not a secret: require letters and digits.
        if v == "[REDACTED]" or not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
            return m.group(0)
        hits.append(("assignment:" + m.group(1), v))
        return m.group(1) + m.group(2) + "[REDACTED]"
    return ASSIGN.sub(assign, s)


def walk(obj, hits):
    if isinstance(obj, str):
        return redact(obj, hits)
    if isinstance(obj, list):
        return [walk(x, hits) for x in obj]
    if isinstance(obj, dict):
        return {k: walk(v, hits) for k, v in obj.items()}
    return obj


def mask(v):
    v = v.replace("\n", " ")
    return v[:4] + "…" + "(%d chars)" % len(v)


def files(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
        for d, _, names in os.walk(p):
            for n in sorted(names):
                if n.endswith(".jsonl"):
                    yield os.path.join(d, n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[os.path.join(HERE, "logs")])
    ap.add_argument("--apply", action="store_true", help="rewrite files with matches")
    a = ap.parse_args()

    total = 0
    for f in files(a.paths):
        hits, lines, changed = [], [], False
        with open(f, encoding="utf-8", errors="replace") as fin:
            for line in fin:
                try:
                    r = json.loads(line)
                except ValueError:
                    lines.append(line)
                    continue
                n = len(hits)
                r2 = walk(r, hits)
                if len(hits) > n:
                    changed = True
                    line = json.dumps(r2, ensure_ascii=False, separators=(",", ":")) + "\n"
                lines.append(line)
        if not hits:
            continue
        total += len(hits)
        print("%s: %d" % (os.path.relpath(f, HERE), len(hits)))
        for name, v in hits[:10]:
            print("    %-28s %s" % (name, mask(v)))
        if a.apply and changed:
            tmp = f + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fout:
                fout.writelines(lines)
            os.replace(tmp, f)
    print("%d match%s%s" % (total, "" if total == 1 else "es", ", redacted" if a.apply and total else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
