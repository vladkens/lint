#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""rust-lint: style and safety checks for Rust repositories, on the lines a branch changes.

  rust-lint [--all] [--base REF] [--no-clippy] [-- CARGO_ARGS…]  check
  rust-lint fix [--all] [--base REF]                              add the missing blank lines
  rust-lint modules                                               what each top module uses
  rust-lint hook                                                  Stop hook of Claude Code, Codex

Only the changed lines count, unless `--all`: the ones that differ from the merge base with the
default branch (origin/main or main), uncommitted and untracked files included. On the default
branch that leaves the uncommitted changes; CI on the default branch checks its last commit.

  blank-line    a statement after an if / for / while / loop / match block starts after a blank
                line (error; `fix` adds it)
  dense-block   more than DENSE statements in a row in a function, without a blank line (error)
  long-comment  more than COMMENT comment lines in a row, `//!` aside (warning: locally each one is
                shown once, in CI always)
  long-fn       a function of more than LONG lines of code that the change touches, unless it was
                that long at the base and didn't grow (error)
  dead-code     an allow or expect of dead_code or unused_*, unless a platform or feature has the
                code (error)
  unused-pub    a new pub item that nothing else in the repository names, tests and doc examples
                included; where clippy runs, only in libraries: the compiler finds it in binaries
                (error)
  cycle         a new use between the top modules of a crate that closes a cycle; with --all every
                cycle (warning, as long-comment)
  new-use       the first use of a top module by another (note)
  clippy        `cargo clippy --all-targets CARGO_ARGS`: any warning fails, as with -D warnings,
                and the STRICT lints only count on changed lines outside tests: a function's
                cognitive_complexity counts where it's new
  quick         `.unwrap()`, `dbg!`, `todo!` and `unimplemented!` outside tests, found in the text
                where clippy doesn't run: with --no-clippy and in the hook (error)

rust-lint is `uv run --script https://raw.githubusercontent.com/vladkens/lint/main/rust.py`, the
LINT of a project's Makefile. The hook reads the agent's Stop event on stdin and works in
repositories whose Makefile runs it (mentions vladkens/lint/). It runs the text and quick rules,
and on an error or a warning it hasn't shown it asks the agent to go on; with stop_hook_active it
lets the agent stop. It fails open.
"""

import argparse
import bisect
import collections
import hashlib
import itertools
import json
import os
import posixpath
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DENSE = 12
COMMENT = 3
LONG = 100
# Panics nobody wrote on purpose, debugging leftovers, lint suppressions without a reason and
# functions too tangled to follow.
STRICT = (
    "unwrap_used",
    "string_slice",
    "unchecked_time_subtraction",
    "todo",
    "unimplemented",
    "dbg_macro",
    "allow_attributes",
    "allow_attributes_without_reason",
    "cognitive_complexity",
)
# Text stand-ins for the STRICT lints a slip leaves, for where clippy doesn't run.
QUICK = {
    "unwrap_used": (r"\.unwrap\(\)", '`.unwrap()` panics: handle the error, or `expect("why")`'),
    "dbg_macro": (r"\bdbg!", "a `dbg!` left in"),
    "todo": (r"\b(?:todo|unimplemented)!", "unfinished code"),
}
HOOK_LIMIT = 10
# In a project's Makefile: the hook works there.
OPT_IN = "vladkens/lint/"

CI = bool(os.environ.get("CI"))
GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"

OPEN = re.compile(r"^(?:let\b.*=\s*)?(?:if|for|while|loop|match)\b.*\{$")
CLOSE = re.compile(r"^\}[;,]?$")
NOT_STATEMENT = ("}", ")", "]", ".", "else", "//", "#[")
FN = re.compile(r"\bfn\s+(\w+)")
MOD = re.compile(r"\bmod\s+(\w+)$")
TEST = re.compile(r"#\[(?:cfg\((?:all\(|any\()?test\b|(?:\w+::)*test\b)")
TEST_DIRS = {"tests", "benches", "examples"}
DEAD = re.compile(r"#!?\[.*\b(?:allow|expect)\s*\(.*\b(?:dead_code|unused\w*)\b")
PUB = re.compile(
    r"^\s*pub\s+(?:(?:const|async|unsafe|extern\s*(?:\"\")?)\s+)*"
    r"(?:fn|struct|enum|trait|type|const|static|union)\s+(\w+)"
)
# Code that some platforms or features use: CI on the others reports it if it's dead there too.
PLATFORM = re.compile(r"cfg_attr\s*\(.*\b(?:target_\w+|unix|windows|feature)\b")
DOC = re.compile(r"^\s*//[/!]\s?(.*)")
# Items other languages call, which no Rust code names.
EXPORTED = re.compile(r"no_mangle|export_name|wasm_bindgen|\bextern\b|pyfunction|napi|uniffi")
IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
USE = re.compile(r"\buse\s+([^;]+);")
PATH = re.compile(r"(?<![\w$])\$?(crate|super|self)((?:\s*::\s*\w+)+)")
MOD_DECL = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;")
PATH_ATTR = re.compile(r'#\[path\s*=\s*"([^"]+)"\]')
ROOT = re.compile(r"(?:^|/)src/(?:lib|main|bin/[^/]+|bin/[^/]+/main)\.rs$")
LINKS = re.compile(r"\b(?:crate|super|self)\s*::|\bmod\s|\buse\s")

TOKEN = re.compile(r"""//|/\*|(?<!\w)[bc]?r#*"|"|'""")
BLOCK = re.compile(r"/\*|\*/")
STRING_END = re.compile(r'(?:[^"\\]|\\.)*"', re.S)
CHAR = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^\\'\n])'")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hit:
    path: str
    line: int
    rule: str
    msg: str
    kind: str = "error"  # a warning is shown once locally; a note doesn't count
    key: str = ""

    @property
    def warn(self) -> bool:
        return self.kind == "warning"

    def __str__(self):
        return f"{self.path}:{self.line}: {self.kind}[{self.rule}]: {self.msg}"


@dataclass
class Fn:
    name: str
    line: int  # where `fn` is
    end: int  # the closing brace
    size: int  # lines with code in the body
    test: bool


@dataclass
class Frame:
    fn: tuple[str, int] | None
    test: bool
    macro: bool
    mod: str | None
    line: int


@dataclass
class Scan:
    raws: list[str]
    codes: list[str]  # without comments, with string and char literals emptied
    noted: set[int]  # lines with a comment
    body: list[bool]  # the line starts inside a function body
    test: list[bool]
    mods: list[tuple[str, ...]]  # the inline modules the line is in
    fns: list[Fn]


# MARK: Rules


def strip(text: str) -> tuple[list[str], set[int]]:
    """Each line's code without comments and literal contents, and the lines with a comment."""
    out, noted, pos, line = [], set(), 0, 0
    while m := TOKEN.search(text, pos):
        out.append(text[pos : m.start()])
        line += text.count("\n", pos, m.start())
        tok, end = m[0], m.end()
        if tok == "//":
            end = text.find("\n", end)
            end = len(text) if end < 0 else end
            noted.add(line)
        elif tok == "/*":
            depth = 1
            for b in BLOCK.finditer(text, end):
                depth += 1 if b[0] == "/*" else -1
                if not depth:
                    end = b.end()
                    break
            else:
                end = len(text)

            lines = text.count("\n", m.start(), end)
            noted.update(range(line, line + lines + 1))
            out.append("\n" * lines)
            line += lines
        elif tok == "'":
            c = CHAR.match(text, m.start())
            end = c.end() if c else end
            out.append("''" if c else "'")
        else:
            if "r" in tok:
                close = '"' + "#" * tok.count("#")
                end = text.find(close, end)
                end = len(text) if end < 0 else end + len(close)
            else:
                s = STRING_END.match(text, end)
                end = s.end() if s else len(text)

            lines = text.count("\n", m.start(), end)
            out.append('""' + "\n" * lines)
            line += lines

        pos = end

    out.append(text[pos:])
    return "".join(out).split("\n"), noted


def blocks(codes: list[str]) -> tuple[list[bool], list[bool], list[tuple[str, ...]], list[Fn]]:
    """Per line: whether it starts in a function body outside macros, whether it's test code and
    the inline modules it's in; and the functions."""
    body, test, mods, fns, stack = [], [], [], [], []
    fn, tst = None, False
    for n, code in enumerate(codes):
        s = code.strip()
        body.append(any(f.fn for f in stack) and not any(f.macro for f in stack))
        mods.append(tuple(f.mod for f in stack if f.mod))
        was = tst or any(f.test for f in stack)
        if m := FN.search(s):
            fn = (m[1], n)

        tst = tst or bool(TEST.search(s))
        for i, c in enumerate(code):
            if c == "{":
                head = code[:i].rstrip()
                mod = None if fn else MOD.search(head)
                stack.append(Frame(fn, tst, head.endswith("!"), mod and mod[1], n))
                fn, tst = None, False
            elif c == "}" and stack:
                f = stack.pop()
                if f.fn:
                    size = sum(1 for x in codes[f.line + 1 : n] if x.strip())
                    inside = f.test or any(x.test for x in stack)
                    fns.append(Fn(f.fn[0], f.fn[1], n, size, inside))

        if s.endswith(";"):
            fn, tst = None, False

        test.append(was or tst or any(f.test for f in stack))

    return body, test, mods, fns


def scan(text: str) -> Scan:
    codes, noted = strip(text)
    return Scan(text.split("\n"), codes, noted, *blocks(codes))


def blank_lines(sc: Scan) -> list[int]:
    """Indexes of the lines that need a blank line above them."""
    opened, out = [], []
    for i, (raw, code) in enumerate(zip(sc.raws, sc.codes)):
        text = code.strip()
        indent = len(raw) - len(raw.lstrip())
        if OPEN.match(text):
            opened.append(indent)
            continue

        if not (opened and indent == opened[-1] and CLOSE.match(text)):
            continue

        opened.pop()
        if i + 1 >= len(sc.raws):
            continue

        after = sc.raws[i + 1]
        if (
            after.strip()
            and len(after) - len(after.lstrip()) == indent
            and not after.strip().startswith(NOT_STATEMENT)
        ):
            out.append(i + 1)

    return out


def dense_blocks(sc: Scan) -> list[tuple[int, int, int]]:
    """Runs of more than DENSE statements without a blank line: first and last index, count."""
    out, run, start, end = [], 0, 0, 0
    for i, (raw, code) in enumerate(zip(sc.raws + [""], sc.codes + [""])):
        s = code.strip()
        if not raw.strip() or not (i < len(sc.body) and sc.body[i]):
            if run > DENSE:
                out.append((start, end, run))

            run = 0
            continue

        if s.endswith((";", "{")) and not s.startswith("}"):
            start = i if not run else start
            run, end = run + 1, i

    return out


def long_comments(sc: Scan) -> list[tuple[int, int]]:
    """Runs of more than COMMENT comment lines: first and last index."""
    out, start = [], None
    for i, (raw, code) in enumerate(zip(sc.raws + [""], sc.codes + [""])):
        text = raw.strip()
        if i in sc.noted and text and not code.strip() and not text.startswith(("//!", "// MARK")):
            start = i if start is None else start
            continue

        if start is not None and i - start > COMMENT:
            out.append((start, i - 1))

        start = None

    return out


def touches(lines: set[int] | None, a: int, b: int) -> bool:
    """Whether the changed line numbers (None: all) meet the line indexes a to b."""
    return lines is None or not lines.isdisjoint(range(a + 1, b + 2))


def hits(path: str, sc: Scan, lines: set[int] | None, quick: bool = False) -> list[Hit]:
    """What the text rules find in a file, limited to the changed lines (None: all of them)."""

    def near(a: int, b: int) -> bool:
        return touches(lines, a, b)

    out = [
        Hit(path, i + 1, "blank-line", "a statement after a block starts after a blank line")
        for i in blank_lines(sc)
        if near(i - 1, i)
    ]
    out += [
        Hit(path, a + 1, "dense-block", f"{n} statements without a blank line; split the steps")
        for a, b, n in dense_blocks(sc)
        if near(a, b)
    ]
    for a, b in long_comments(sc):
        if not near(a, b):
            continue

        text = "\n".join(x.strip() for x in sc.raws[a : b + 1])
        key = hashlib.sha1(f"{path}\n{text}".encode()).hexdigest()[:16]
        msg = f"{b - a + 1} comment lines; say only what the code can't, or keep it if it's needed"
        out.append(Hit(path, a + 1, "long-comment", msg, "warning", key))

    if testing(path):
        return out

    msg = (
        "the compiler is told not to report unused code: delete it or use it; a field kept for its "
        "Drop or an FFI layout takes a name that starts with `_`"
    )
    out += [
        Hit(path, i + 1, "dead-code", msg)
        for i, code in enumerate(sc.codes)
        if not sc.test[i] and near(i, i) and DEAD.search(code) and not PLATFORM.search(code)
    ]
    if quick:
        out += [
            Hit(path, i + 1, rule, msg)
            for i, code in enumerate(sc.codes)
            for rule, (rx, msg) in QUICK.items()
            if not sc.test[i] and near(i, i) and re.search(rx, code)
        ]

    return out


# MARK: Functions, dead code, modules


def long_fns(
    top: Path, since: str | None, path: str, sc: Scan, lines: set[int] | None
) -> list[Hit]:
    """Functions over LONG lines that the change touches, unless they were that long at the base
    and didn't grow."""
    found = [f for f in sc.fns if f.size > LONG and not f.test and touches(lines, f.line, f.end)]
    if not found or testing(path):
        return []

    old = git(top, "show", f"{since}:{path}") if since and lines is not None else None
    before = collections.defaultdict(int)
    for f in scan(old).fns if old else []:
        before[f.name] = max(before[f.name], f.size)

    return [
        Hit(path, f.line + 1, "long-fn", f"`{f.name}` has {f.size} lines of code; split it")
        for f in found
        if before[f.name] <= LONG or f.size > before[f.name]
    ]


def package(path: str) -> str | None:
    """The directory of the package a file under src/ is in."""
    parts = path.split("/")[:-1]
    if "src" not in parts:
        return None

    return "/".join(parts[: len(parts) - 1 - parts[::-1].index("src")])


def unused_pubs(scans: "Scans", scope: dict | None, quick: bool) -> list[Hit]:
    """New pub items whose name appears nowhere else. Where clippy runs, only in libraries: in a
    binary the compiler reports them itself."""
    libs = {package(x) for x in scans.names if x.endswith("src/lib.rs")}
    found = []
    for path in scans.names if scope is None else [x for x in scans.names if x in scope]:
        if testing(path) or not (quick or package(path) in libs):
            continue

        sc, lines = scans[path], None if scope is None else scope[path]
        for i, code in enumerate(sc.codes):
            if sc.test[i] or (lines is not None and i + 1 not in lines):
                continue

            m = PUB.match(code)
            name = m and m[1]
            if not name or name == "main":
                continue

            if not any(EXPORTED.search(x) for x in [code, *sc.raws[max(0, i - 3) : i]]):
                found.append((path, i, name))

    if not found:
        return []

    seen = collections.Counter(
        w for x in scans.names for code in uses(scans[x]) for w in IDENT.findall(code)
    )
    msg = "is used nowhere, not even in a test: use it, test it or delete it"
    return [
        Hit(path, i + 1, "unused-pub", f"`{name}` {msg}")
        for path, i, name in found
        if seen[name] < 2
    ]


def uses(sc: Scan) -> list[str]:
    """The code of a file, the examples in its doc comments, which run as tests, and its attributes,
    which name functions in strings: `#[serde(default = "port")]`."""
    out, fenced = list(sc.codes), False
    for raw in sc.raws:
        doc = DOC.match(raw)
        if doc and doc[1].startswith("```"):
            fenced = not fenced
        elif doc and fenced:
            out.append(doc[1])
        elif not doc:
            fenced = False
            if raw.lstrip().startswith("#["):
                out.append(raw)

    return out


def modules(scans: "Scans") -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each file of a crate: its crate root and module path, from the roots' `mod x;` lines."""
    out = {}
    for root in sorted(x for x in scans.names if ROOT.search(x)):
        todo = [(root, ())]
        while todo:
            path, mod = todo.pop()
            if path in out:
                continue

            out[path] = (root, mod)
            sc, parent, stem = scans[path], posixpath.dirname(path), posixpath.basename(path)[:-3]
            here = parent if path == root or stem == "mod" else posixpath.join(parent, stem)
            for i, code in enumerate(sc.codes):
                if not (m := MOD_DECL.match(code)):
                    continue

                attr = [a[1] for x in sc.raws[max(0, i - 3) : i] if (a := PATH_ATTR.search(x))]
                d = posixpath.join(here, *sc.mods[i])
                cands = [posixpath.join(parent, attr[0])] if attr else []
                cands += [posixpath.join(d, m[1] + ".rs"), posixpath.join(d, m[1], "mod.rs")]
                child = next(
                    (posixpath.normpath(x) for x in cands if posixpath.normpath(x) in scans.names),
                    None,
                )
                if child:
                    todo.append((child, mod + sc.mods[i] + (m[1],)))

    return out


def expand(tree: str) -> list[list[str]]:
    """The paths a use tree names: `a::{b, c::d as e}` gives a::b and a::c::d."""
    out = []

    def go(prefix: list[str], s: str):
        if s.endswith("}") and "{" in s:
            i, depth, part = s.index("{"), 0, ""
            head = [x for x in s[:i].split("::") if x]
            for c in s[i + 1 : -1] + ",":
                depth += (c == "{") - (c == "}")
                if c == "," and not depth:
                    go(prefix + head, part)
                    part = ""
                else:
                    part += c

        elif s:
            out.append(prefix + [x for x in s.split(" as ")[0].split("::") if x])

    go([], re.sub(r"\s*(::|[{},])\s*", r"\1", " ".join(tree.split())))
    return out


def resolve(cur: tuple[str, ...], segs: list[str]) -> tuple[str, ...] | None:
    """The absolute path that a crate::, self:: or super:: path names in module cur."""
    if not segs or segs[0] not in ("crate", "self", "super"):
        return None

    path, i = ([] if segs[0] == "crate" else list(cur)), int(segs[0] != "super")
    while i < len(segs) and segs[i] == "super":
        if not path:
            return None

        path.pop()
        i += 1

    return tuple(path + segs[i:])


def links(scans: "Scans") -> dict[tuple[str, str, str], list[tuple[str, int]]]:
    """Uses between the top modules of each crate, outside tests and crate roots: (root, from, to)
    and the lines."""
    tree = modules(scans)
    tops = collections.defaultdict(set)
    for root, mod in tree.values():
        if mod:
            tops[root].add(mod[0])

    out = collections.defaultdict(list)
    for path, (root, mod) in sorted(tree.items()):
        if not mod or testing(path):
            continue

        sc = scans[path]
        text = "\n".join(sc.codes)
        starts = list(itertools.accumulate((len(x) + 1 for x in sc.codes[:-1]), initial=0))
        uses = [(m.start(), m.end(), expand(m[1])) for m in USE.finditer(text)]
        refs = [(a, x) for a, _, xs in uses for x in xs]
        refs += [
            (m.start(), [m[1], *re.split(r"\s*::\s*", m[2].strip())[1:]])
            for m in PATH.finditer(text)
            if not any(a <= m.start() < b for a, b, _ in uses)
        ]
        for pos, segs in refs:
            i = bisect.bisect_right(starts, pos) - 1
            to = None if sc.test[i] else resolve(mod + sc.mods[i], segs)
            if to and to[0] in tops[root] and to[0] != mod[0]:
                at = out[(root, mod[0], to[0])]
                if (path, i + 1) not in at:
                    at.append((path, i + 1))

    return out


def route(graph: dict[str, set[str]], a: str, b: str) -> list[str] | None:
    """The shortest way from module a to b."""
    prev, todo = {a: a}, [a]
    for x in todo:
        if x == b:
            out = [b]
            while out[-1] != a:
                out.append(prev[out[-1]])

            return out[::-1]

        for y in sorted(graph.get(x, ())):
            if y not in prev:
                prev[y] = x
                todo.append(y)

    return None


def graphs(found: dict) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(set)
    )
    for root, a, b in found:
        out[root][a].add(b)

    return out


def reachable(graph: dict[str, set[str]], a: str) -> set[str]:
    todo = [a]
    for x in todo:
        todo += [y for y in sorted(graph.get(x, ())) if y not in todo]

    return set(todo)


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """The groups of modules that reach each other."""
    nodes = sorted(set(graph) | {y for ys in graph.values() for y in ys})
    reach = {x: reachable(graph, x) for x in nodes}
    out, done = [], set()
    for x in nodes:
        group = [y for y in nodes if y in reach[x] and x in reach[y]]
        if x not in done and len(group) > 1:
            out.append(group)

        done.update(group)

    return out


def tangles(top: Path, since: str | None, scope: dict | None, scans: "Scans") -> list[Hit]:
    """Cycles between modules: with --all every one, else those the change closes, as warnings,
    and the first uses of a module by another, as notes."""
    if scope is not None and not any(
        LINKS.search(raw)
        for x in scans.names
        if x in scope
        for n, raw in enumerate(scans[x].raws, 1)
        if scope[x] is None or n in scope[x]
    ):
        return []

    now, out = links(scans), []
    graph = graphs(now)
    if scope is None:
        for root in sorted(graph):
            for group in cycles(graph[root]):
                path, line = min(
                    r
                    for (rt, a, b), rs in now.items()
                    if rt == root and {a, b} <= {*group}
                    for r in rs
                )
                key = hashlib.sha1(f"cycle\n{root}\n{group}".encode()).hexdigest()[:16]
                msg = f"modules {', '.join(group)} use each other in a cycle"
                out.append(Hit(path, line, "cycle", msg, "warning", key))

        return out

    old = {}
    if since:
        same = {x for x in scans.names if x not in scope}
        before = Scans(top, snapshot(top, since, same))
        before.update({x: scans[x] for x in before.names if x in same})
        old = links(before)

    done = set()
    for (root, a, b), refs in sorted(now.items()):
        if (root, a, b) in old or (root, a) in done:
            continue

        path, line = next(
            ((p, n) for p, n in refs if p in scope and (scope[p] is None or n in scope[p])),
            refs[0],
        )
        back = route(graph[root], b, a)
        if not back:
            out.append(Hit(path, line, "new-use", f"`{a}` uses `{b}` for the first time", "note"))
            continue

        # One warning for each tangle: the other new uses in it close the same cycles.
        done.update((root, x) for x in reachable(graph[root], a) if a in reachable(graph[root], x))
        key = hashlib.sha1(f"cycle\n{root}\n{a}\n{b}".encode()).hexdigest()[:16]
        msg = (
            f"`{a}` now uses `{b}`, which uses it back: {' -> '.join([a, *back])}; move what both "
            "need into a module of their own, or keep it if it's wanted"
        )
        out.append(Hit(path, line, "cycle", msg, "warning", key))

    return out


# MARK: Changed lines


def git(top: Path, *args: str) -> str | None:
    rep = subprocess.run(
        ["git", *args], cwd=top, capture_output=True, encoding="utf-8", errors="replace"
    )
    return rep.stdout.strip() if rep.returncode == 0 else None


def base(top: Path, ref: str | None) -> str | None:
    """The commit the changed lines are counted from; None when there is nothing to compare to."""
    if ref:
        found = git(top, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if not found:
            sys.exit(f"rust-lint: unknown base {ref}")

        return found

    if CI and git(top, "rev-parse", "--is-shallow-repository") == "true":
        git(top, "fetch", "--no-tags", "--quiet", "--unshallow", "origin")

    names = ["origin/HEAD", "origin/main", "origin/master", "main", "master"]
    if os.environ.get("GITHUB_BASE_REF"):
        names.insert(0, "origin/" + os.environ["GITHUB_BASE_REF"])

    head = git(top, "rev-parse", "--verify", "--quiet", "HEAD")
    found = next((x for x in (git(top, "merge-base", "HEAD", n) for n in names) if x), None)
    if not found:
        return head

    if CI and found == head:
        return git(top, "rev-parse", "--verify", "--quiet", "HEAD~1") or found

    return found


def changed(top: Path, since: str) -> dict[str, set[int] | None]:
    """Changed .rs files and their changed line numbers; None for an untracked file."""
    diff = git(
        top,
        *("-c", "core.quotePath=false", "diff", "-U0", "--no-color", "--no-ext-diff"),
        *("--src-prefix=a/", "--dst-prefix=b/", since, "--", "*.rs"),
    )
    out: dict[str, set[int]] = {}
    path = None
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (m := HUNK.match(line)):
            a, n = int(m[1]), int(m[2] or 1)
            # A deletion touches the lines around it: it can remove a needed blank line.
            out.setdefault(path, set()).update(range(a, a + n) if n else (a, a + 1))

    new = git(top, "ls-files", "--others", "--exclude-standard", "--", "*.rs")
    return {**out, **dict.fromkeys((new or "").splitlines())}


def targets(top: Path, scope: dict | None) -> list[tuple[str, set[int] | None]]:
    if scope is None:
        names = git(top, "ls-files", "-co", "--exclude-standard", "--", "*.rs") or ""
        scope = dict.fromkeys(names.splitlines())

    return [(x, lines) for x, lines in sorted(scope.items()) if (top / x).is_file()]


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


class Scans(dict):
    """The scans of a repository's .rs files, made when first needed, from the given texts or the
    working tree."""

    def __init__(self, top: Path, texts: Mapping[str, str | None] | None = None):
        super().__init__()
        self.top, self.texts = top, texts
        self.names = sorted(texts) if texts is not None else [x for x, _ in targets(top, None)]

    def __missing__(self, path: str) -> Scan:
        text = self.texts.get(path) if self.texts is not None else None
        self[path] = scan(read(self.top / path) if text is None else text)
        return self[path]


def snapshot(top: Path, rev: str, same: set[str]) -> dict[str, str | None]:
    """The .rs files of a commit; None for those in same, as the working tree has them."""
    names = (git(top, "ls-tree", "-r", "-z", "--name-only", rev) or "").split("\0")
    out = {x: None for x in names if x.endswith(".rs")}
    need = [x for x in out if x not in same]
    batch = "".join(f"{rev}:{x}\n" for x in need).encode()
    data = subprocess.run(["git", "cat-file", "--batch"], cwd=top, input=batch, capture_output=True)
    pos, data = 0, data.stdout
    for x in need:
        end = data.find(b"\n", pos)
        head, pos = data[pos:end].split(), end + 1
        if len(head) == 3 and head[1] == b"blob":
            out[x] = data[pos : pos + int(head[2])].decode("utf-8", "replace")
            pos += int(head[2]) + 1

    return out


def findings(top: Path, scope: dict | None, since: str | None, quick: bool = False) -> list[Hit]:
    scans, out = Scans(top), []
    for x in scans.names:
        if scope is None or x in scope:
            lines = None if scope is None else scope[x]
            out += hits(x, scans[x], lines, quick) + long_fns(top, since, x, scans[x], lines)

    return out + unused_pubs(scans, scope, quick) + tangles(top, since, scope, scans)


def unseen(top: Path, warns: list[Hit]) -> list[Hit]:
    """The warnings not shown in this repository before, remembered as shown from now on."""
    path = top / (git(top, "rev-parse", "--git-common-dir") or ".git") / "rust-lint-seen"
    seen = set(read(path).split()) if path.is_file() else set()
    new = [x for x in warns if x.key not in seen]
    if new:
        path.write_text("".join(f"{x}\n" for x in sorted(seen | {x.key for x in new})))

    return new


# MARK: Clippy


def clippy(top: Path, scope: dict | None, args: list[str]) -> int:
    """Runs clippy and prints what fails; returns how many messages failed."""
    i = args.index("--") if "--" in args else len(args)
    fmt = "json-diagnostic-rendered-ansi" if sys.stdout.isatty() else "json"
    strict = [f"-Wclippy::{x}" for x in STRICT]
    cmd = ["cargo", "clippy", "--all-targets", f"--message-format={fmt}", *args[:i]]
    rep = subprocess.run(
        [*cmd, "--", *strict, *args[i + 1 :]],
        stdout=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    failed = report(top, scope, rep.stdout)
    # A build that failed without a diagnostic has said why on stderr.
    return failed or int(rep.returncode != 0)


def report(top: Path, scope: dict | None, out: str) -> int:
    seen, scans, failed = set(), {}, 0
    for line in out.splitlines():
        msg = json.loads(line) if line.startswith("{") else {}
        m = msg.get("message") if msg.get("reason") == "compiler-message" else None
        if not m or m["level"] not in ("warning", "error") or not m["spans"]:
            continue

        span = next((x for x in m["spans"] if x["is_primary"]), m["spans"][0])
        file = (Path.cwd() / span["file_name"]).resolve()
        path = file.relative_to(top).as_posix() if file.is_relative_to(top) else str(file)
        code = (m["code"] or {}).get("code") or ""
        key = (code, path, span["line_start"], span["column_start"], m["message"])
        if key in seen:
            continue

        seen.add(key)
        if m["level"] == "warning" and code.removeprefix("clippy::") in STRICT:
            if path not in scans and file.is_file():
                scans[path] = scan(read(file)).test

            a, b = span["line_start"], span["line_end"]
            flags = scans.get(path, [])
            lines = scope.get(path, set()) if scope is not None else None
            if testing(path) or (a <= len(flags) and flags[a - 1]):
                continue

            if lines is not None and lines.isdisjoint(range(a, b + 1)):
                continue

        failed += 1
        sys.stdout.write(m["rendered"])
        if GITHUB:
            annotate("error", path, span["line_start"], code or "rustc", m["message"])

    return failed


def testing(path: str) -> bool:
    """Whether the whole file is tests, examples, benchmarks or a build script."""
    parts = Path(path).parts
    return bool(TEST_DIRS & set(parts[:-1])) or parts[-1] in ("build.rs", "tests.rs")


def annotate(kind: str, path: str, line: int, title: str, msg: str):
    def esc(x: str, prop: bool = False) -> str:
        x = x.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return x.replace(":", "%3A").replace(",", "%2C") if prop else x

    print(f"::{kind} file={esc(path, True)},line={line},title={esc(title, True)}::{esc(msg)}")


# MARK: Commands


def check(top: Path, scope: dict | None, since: str | None, opts) -> int:
    found = findings(top, scope, since, quick=opts.no_clippy)
    errors = [x for x in found if x.kind == "error"]
    warns = [x for x in found if x.warn]
    notes = [x for x in found if x.kind == "note"]
    shown = warns if CI or scope is None else unseen(top, warns)
    for h in errors + shown + notes:
        print(h)
        if GITHUB:
            annotate("notice" if h.kind == "note" else h.kind, h.path, h.line, h.rule, h.msg)

    failed = len(errors) + (0 if opts.no_clippy else clippy(top, scope, opts.cargo))
    where = f"the lines changed since {since[:7]}" if since else "all lines"
    said = [count(failed, "error")] if failed or not shown else []
    said += [count(len(shown), "warning")] if shown else []
    said += [count(len(notes), "note")] if notes else []
    hidden = len(warns) - len(shown)
    if hidden:
        it = "it" if hidden == 1 else "them"
        where += f" ({count(hidden, 'warning')} shown before, `--all` lists {it})"

    print(f"rust-lint: {', '.join(said)} in {where}")
    return 1 if failed else 0


def outline(top: Path) -> int:
    """Prints what each top module of each crate uses, in how many places, and the cycles."""
    found = links(Scans(top))
    for root, graph in sorted(graphs(found).items()):
        print(root)
        for a in sorted(graph):
            print(
                f"  {a} -> " + ", ".join(f"{b} {len(found[root, a, b])}" for b in sorted(graph[a]))
            )

        for group in cycles(graph):
            print(f"  cycle: {', '.join(group)}")

    return 0


def count(n: int, word: str) -> str:
    return f"{n or 'no'} {word}{'' if n == 1 else 's'}"


def fix(top: Path, scope: dict | None) -> int:
    added = 0
    for x, lines in targets(top, scope):
        text = read(top / x)
        sc = scan(text)
        need = [h.line - 1 for h in hits(x, sc, lines) if h.rule == "blank-line"]
        if not need:
            continue

        eol = "\r" if "\r\n" in text else ""
        for i in reversed(need):
            sc.raws.insert(i, eol)

        (top / x).write_bytes("\n".join(sc.raws).encode())
        added += len(need)

    if added:
        print(f"rust-lint: added {count(added, 'blank line')}")

    return 0


def hook() -> int:
    try:
        pld = json.load(sys.stdin)
        top = git(Path(pld.get("cwd") or "."), "rev-parse", "--show-toplevel")
        make = Path(top or ".", "Makefile")
        if pld.get("stop_hook_active") or not top or not make.is_file():
            return 0

        top = Path(top)
        since = base(top, None) if OPT_IN in read(make) else None
        if not since:
            return 0

        found = findings(top, changed(top, since), since, quick=True)
        errors = [x for x in found if x.kind == "error"]
        new = unseen(top, [x for x in found if x.warn])
        if not errors and not new:
            return 0

        shown = errors + new
        why = ["rust-lint found this in the Rust lines the branch changes:"]
        why += [str(x) for x in shown[:HOOK_LIMIT]]
        if len(shown) > HOOK_LIMIT:
            why.append(f"…and {len(shown) - HOOK_LIMIT} more; `make check` lists them all.")

        if errors:
            why.append("Fix the errors before you finish; `make prepare` adds the blank lines.")

        if new:
            why.append(
                "A warning comes up once: shorten a long comment to what the reader needs, "
                "untangle a cycle, or leave it if it's needed."
            )

        print(json.dumps({"decision": "block", "reason": "\n".join(why)}))
    except Exception:
        pass

    return 0


def main() -> int:
    argv = sys.argv[1:]
    i = argv.index("--") if "--" in argv else len(argv)
    cli = argparse.ArgumentParser(
        prog="rust-lint", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    cli.add_argument("cmd", nargs="?", choices=("check", "fix", "modules", "hook"), default="check")
    cli.add_argument("--all", action="store_true", help="every line counts, not only changed ones")
    cli.add_argument("--base", help="count the changes from this commit")
    cli.add_argument("--no-clippy", action="store_true", help="only the text rules")
    opts = cli.parse_args(argv[:i])
    opts.cargo = argv[i + 1 :]
    if opts.cmd == "hook":
        return hook()

    top = git(Path.cwd(), "rev-parse", "--show-toplevel")
    if not top:
        sys.exit("rust-lint: not in a git repository")

    top = Path(top)
    if opts.cmd == "modules":
        return outline(top)

    since = None if opts.all else base(top, opts.base)
    scope = changed(top, since) if since else None
    return fix(top, scope) if opts.cmd == "fix" else check(top, scope, since, opts)


if __name__ == "__main__":
    sys.exit(main())
