#!/usr/bin/env python3
"""Profile / track GS1 coverage over the corpus.

Extracts NPC script bodies (between `NPC ...` and `NPCEND`) from every .nw under
the corpus dir and reports command/function frequency. As the lexer and parser
come online this same harness will report parse-rate (the regression metric).

Usage:
    python tools/gs1_corpus_profile.py [corpus_dir]   # default: tests/gs1_corpus
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CORPUS = os.path.normpath(os.path.join(HERE, "..", "tests", "gs1_corpus"))

CALL_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
LEAD_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)")


def iter_npc_scripts(corpus_dir):
    """Yield (filename, script_text) for each NPC block in the corpus."""
    for fn in sorted(os.listdir(corpus_dir)):
        if not fn.endswith(".nw"):
            continue
        try:
            txt = open(os.path.join(corpus_dir, fn), encoding="latin-1").read()
        except OSError:
            continue
        buf, inblk = [], False
        for line in txt.splitlines():
            s = line.strip()
            if s == "NPC" or s.startswith("NPC "):
                buf, inblk = [], True
                continue
            if s == "NPCEND":
                if inblk:
                    yield fn, "\n".join(buf)
                inblk = False
                continue
            if inblk:
                buf.append(line)


def main():
    corpus = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CORPUS
    cmd, fn_calls = collections.Counter(), collections.Counter()
    nblocks = nlines = 0
    for _fname, script in iter_npc_scripts(corpus):
        nblocks += 1
        for line in script.splitlines():
            s = line.strip()
            if not s:
                continue
            nlines += 1
            m = LEAD_RE.match(s)
            if m:
                cmd[m.group(1)] += 1
            for name in CALL_RE.findall(s):
                fn_calls[name] += 1

    print(f"corpus={corpus}")
    print(f"npc_blocks={nblocks}  code_lines={nlines}")
    print("\ntop 40 leading tokens (commands/keywords):")
    for k, v in cmd.most_common(40):
        print(f"  {v:7d}  {k}")
    print("\ntop 30 call names (functions/user-funcs):")
    for k, v in fn_calls.most_common(30):
        print(f"  {v:7d}  {k}")


if __name__ == "__main__":
    main()
