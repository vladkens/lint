#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///

"""Tests for rust.py: the rules on fixtures marked with `// expect: <rule>`, and the changed
lines, fix, clippy filtering and the Stop hook in a scratch git repository."""

import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import rust as rl

rl.CI = rl.GITHUB = False

BLANK = """
fn main() {
    let x = 1;
    if x > 0 {
        println!("{x}");
    }
    let y = 2; // expect: blank-line
    if y > 0 { // a trailing comment
        println!("{y}");
    }
    run(y); // expect: blank-line

    let v = match x {
        1 => "one",
        _ => "many",
    };
    println!("{v}"); // expect: blank-line
    if x > 1 {
        println!("big");
    } else {
        println!("small");
    }
    // a comment may follow a block
    let s = "if x {";
    for c in s.chars() {
        let _ = c;
    }
}
"""

DENSE = """
fn dense() {
    let a = 1; // expect: dense-block
    let b = 2;
    let c = 3;
    let d = 4;
    let e = 5;
    let f = 6;
    let g = 7;
    let h = 8;
    let i = 9;
    let j = 10;
    let k = 11;
    let l = 12;
    let m = 13;
}

fn twelve() {
    let a = 1;
    let b = 2;
    let c = 3;
    let d = 4;
    let e = 5;
    let f = 6;

    let g = 7;
    let h = 8;
    let i = 9;
    let j = 10;
    let k = 11;
    let l = 12;
    let m = 13;
}

struct Fields {
    a: u8, b: u8, c: u8, d: u8, e: u8, f: u8, g: u8,
    h: u8, i: u8, j: u8, k: u8, l: u8, m: u8, n: u8,
}

fn page() -> Markup {
    html! {
        div { "a" }
        div { "b" }
        div { "c" }
        div { "d" }
        div { "e" }
        div { "f" }
        div { "g" }
        div { "h" }
        div { "i" }
        div { "j" }
        div { "k" }
        div { "l" }
        div { "m" }
    }
}
"""

COMMENTS = """
//! A module doc can be long:
//! it is the module's readme,
//! with examples
//! and links.

// Four lines of comment // expect: long-comment
// are more than
// a reader needs
// before the code.
fn a() {}

// Three lines
// are fine
// as they are.
fn b() {}

// MARK: Group
// The mark is not
// part of the
// comment.
fn c() {}

/// A doc comment // expect: long-comment
/// counts
/// the same
/// way.
fn d() {}

/* A block // expect: long-comment
   comment
   counts
   too. */
fn e() {}
"""

PANICS = """
fn a(x: Option<u8>) -> u8 {
    let s = "x.unwrap()";
    dbg!(x); // expect: dbg_macro
    x.unwrap_or(0);
    x.unwrap() // expect: unwrap_used
}

#[cfg(test)]
mod tests {
    fn t(x: Option<u8>) {
        x.unwrap();
        todo!();
    }
}
"""


DEAD = """
#![allow(unused_imports)] // expect: dead-code
#[allow(dead_code)] // expect: dead-code
fn a() {}
#[expect(dead_code, reason = "later")] // expect: dead-code
fn b() {}
#[cfg_attr(windows, allow(dead_code))]
fn c() {}
#[cfg_attr(not(test), allow(dead_code))] // expect: dead-code
fn d() {}
#[allow(clippy::too_many_arguments, reason = "the API")]
fn e() {}

#[cfg(test)]
mod tests {
    #[allow(dead_code)]
    fn t() {}
}
"""


def expected(text: str) -> list[tuple[int, str]]:
    return [
        (i + 1, x.split("// expect: ")[1].strip())
        for i, x in enumerate(text.split("\n"))
        if "// expect: " in x
    ]


def found(text: str, quick: bool = False, path: str = "a.rs") -> list[tuple[int, str]]:
    return sorted((x.line, x.rule) for x in rl.hits(path, rl.scan(text), None, quick))


class Rules(unittest.TestCase):
    def test_fixtures(self):
        for text in (BLANK, DENSE, COMMENTS, DEAD):
            self.assertEqual(found(text), expected(text))

    def test_quick(self):
        self.assertEqual(found(PANICS, quick=True), expected(PANICS))
        self.assertEqual(found(PANICS), [])
        self.assertEqual(found(PANICS, quick=True, path="tests/a.rs"), [])

    def test_long_comment_is_a_warning(self):
        self.assertTrue(all(x.warn for x in rl.hits("a.rs", rl.scan(COMMENTS), None)))

    def test_strip(self):
        cases = {
            'let s = "a // b { /*"; // c': 'let s = ""; ',
            'let r = r#"x "q" {"#; let b = br"}";': 'let r = ""; let b = "";',
            "let c = ['{', '\"', '\\'', '\\u{7b}'];": "let c = ['', '', '', ''];",
            "fn f<'a>(x: &'a str) -> &'a str { x }": "fn f<'a>(x: &'a str) -> &'a str { x }",
            "/* a /* b */ c */ x": " x",
        }
        for src, code in cases.items():
            self.assertEqual(rl.strip(src)[0], [code], src)

        codes, noted = rl.strip('let s = "a\nb";\n/* x\ny */\nz')
        self.assertEqual(codes, ['let s = ""', ";", "", "", "z"])
        self.assertEqual(noted, {2, 3})

    def test_test_code(self):
        text = textwrap.dedent("""
            fn a() {}
            #[cfg(test)]
            mod tests {
                #[test]
                fn t() {}
            }
            fn b() {}
            #[tokio::test]
            async fn c() {
                run();
            }
            #[cfg(test)]
            use x;
            fn d() {}
        """)
        flags = rl.scan(text).test
        self.assertEqual(
            [i + 1 for i, x in enumerate(flags) if x], [3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14]
        )

    def test_long_fn(self):
        def fn(name: str, n: int, attr: str = "") -> str:
            return f"{attr}fn {name}() {{\n" + "    x();\n\n" * n + "}\n"

        text = fn("short", 100) + fn("long", 101) + fn("t", 120, "#[test]\n")
        hits = rl.long_fns(Path("."), None, "a.rs", rl.scan(text), None)
        self.assertEqual(
            [str(x) for x in hits],
            ["a.rs:203: error[long-fn]: `long` has 101 lines of code; split it"],
        )
        self.assertEqual(rl.long_fns(Path("."), None, "tests/a.rs", rl.scan(text), None), [])

    def test_unused_pub(self):
        texts = {
            "src/lib.rs": "pub mod a;\nmod b;\n",
            "src/a.rs": textwrap.dedent("""
                pub fn used() {}
                pub fn unused() {}
                pub(crate) fn inner() {}
                pub struct S {
                    pub field: u8,
                }
                #[no_mangle]
                pub extern "C" fn ffi() {}
                /// ```
                /// lib::a::documented();
                /// ```
                pub fn documented() {}
                pub fn port() -> u16 {
                    1
                }
                #[cfg(test)]
                mod tests {
                    pub fn helper() {}
                }
            """),
            "src/b.rs": textwrap.dedent("""
                fn f(s: crate::a::S) {
                    crate::a::used();
                }
                #[derive(Deserialize)]
                struct Cfg {
                    #[serde(default = "crate::a::port")]
                    p: u16,
                }
            """),
            "app/src/main.rs": "pub fn main() {}\npub fn lonely() {}\n",
        }
        scans = rl.Scans(Path("."), texts)

        def names(quick: bool, scope: dict | None = None) -> list[str]:
            return [x.msg.split("`")[1] for x in rl.unused_pubs(scans, scope, quick)]

        self.assertEqual(names(False), ["unused"])
        self.assertEqual(names(True), ["lonely", "unused"])
        self.assertEqual(names(True, {"src/a.rs": {2}}), [])

    def test_expand(self):
        self.assertEqual(
            rl.expand("crate::a::{b, c::{d as e, f}, self}"),
            [
                ["crate", "a", "b"],
                ["crate", "a", "c", "d"],
                ["crate", "a", "c", "f"],
                ["crate", "a", "self"],
            ],
        )
        self.assertEqual(rl.expand("super :: x as y"), [["super", "x"]])

    def test_resolve(self):
        cur = ("a", "b")
        self.assertEqual(rl.resolve(cur, ["crate", "z", "w"]), ("z", "w"))
        self.assertEqual(rl.resolve(cur, ["self", "y"]), ("a", "b", "y"))
        self.assertEqual(rl.resolve(cur, ["super", "super", "x"]), ("x",))
        self.assertIsNone(rl.resolve(cur, ["super", "super", "super", "x"]))
        self.assertIsNone(rl.resolve(cur, ["std", "fmt"]))

    def test_modules(self):
        texts = {
            "src/main.rs": 'mod a;\nmod b;\n#[path = "gen/c.rs"]\nmod c;\nuse crate::a::x;\n',
            "src/a.rs": textwrap.dedent("""
                use crate::b::{self, y};
                mod inner {
                    use super::super::c::z;
                    fn f() {
                        super::x();
                    }
                }
                #[cfg(test)]
                mod tests {
                    use crate::c::w;
                }
            """),
            "src/b/mod.rs": "pub mod deep;\n",
            "src/b/deep.rs": "fn f() {\n    crate::a::x();\n}\n",
            "src/gen/c.rs": "",
            "src/orphan.rs": "use crate::a::x;\n",
        }
        scans = rl.Scans(Path("."), texts)
        self.assertEqual(
            rl.modules(scans),
            {
                "src/main.rs": ("src/main.rs", ()),
                "src/a.rs": ("src/main.rs", ("a",)),
                "src/b/mod.rs": ("src/main.rs", ("b",)),
                "src/b/deep.rs": ("src/main.rs", ("b", "deep")),
                "src/gen/c.rs": ("src/main.rs", ("c",)),
            },
        )
        self.assertEqual(
            dict(rl.links(scans)),
            {
                ("src/main.rs", "a", "b"): [("src/a.rs", 2)],
                ("src/main.rs", "a", "c"): [("src/a.rs", 4)],
                ("src/main.rs", "b", "a"): [("src/b/deep.rs", 2)],
            },
        )
        graph = rl.graphs(rl.links(scans))["src/main.rs"]
        self.assertEqual(rl.cycles(graph), [["a", "b"]])
        self.assertEqual(rl.route(graph, "b", "c"), ["b", "a", "c"])
        self.assertIsNone(rl.route(graph, "c", "a"))


URL = "https://raw.githubusercontent.com/vladkens/lint/main/rust.py"
OLD = "fn old() {\n    if a {\n        b();\n    }\n    c();\n}\n"
NEW = "fn new() {\n    if a {\n        b();\n    }\n    c();\n}\n"
TESTS = "#[cfg(test)]\nmod tests {\n    fn t() {\n        x.unwrap();\n    }\n}\n"
LONG = "\n// one\n// two\n// three\n// four\n"


class Repo(unittest.TestCase):
    """A repository on branch feat, whose main has src/a.rs with OLD."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.enterContext(mock.patch.dict(os.environ, env))
        self.write("src/a.rs", OLD)
        self.write("Makefile", f"LINT := uv run --script {URL}\n\ncheck:\n\t$(LINT)\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        self.git("switch", "-qc", "feat")

    @property
    def top(self) -> Path:
        return Path(self.tmp.name).resolve()

    def git(self, *args: str) -> str:
        cfg = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        rs = subprocess.run(
            ["git", *cfg, *args], cwd=self.top, check=True, capture_output=True, text=True
        )

        return rs.stdout.strip()

    def write(self, path: str, text: str):
        (self.top / path).parent.mkdir(parents=True, exist_ok=True)
        (self.top / path).write_bytes(text.encode())

    def scope(self, since: str | None = None) -> dict:
        since = since or rl.base(self.top, None)
        assert since
        return rl.changed(self.top, since)

    def test_only_changed_lines_count(self):
        self.write("src/a.rs", OLD + "\n" + NEW)
        self.write("src/b.rs", NEW)
        self.git("commit", "-qam", "new")
        scope = self.scope()
        self.assertEqual(scope, {"src/a.rs": set(range(7, 14)), "src/b.rs": None})
        hits = [(x.path, x.line) for x in rl.findings(self.top, scope, None)]
        self.assertEqual(hits, [("src/a.rs", 12), ("src/b.rs", 5)])
        self.assertEqual(len(rl.findings(self.top, None, None)), 3)

    def test_base(self):
        main = self.git("rev-parse", "main")
        self.write("src/a.rs", NEW)
        self.git("commit", "-qam", "new")
        self.assertEqual(rl.base(self.top, None), main)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(rl.base(self.top, None), self.git("rev-parse", "HEAD"))
        with mock.patch.object(rl, "CI", True):
            self.assertEqual(rl.base(self.top, None), main)

    def test_deleting_a_blank_line_counts(self):
        self.write("src/a.rs", OLD.replace("    }\n", "    }\n\n"))
        self.git("commit", "-qam", "blank")
        self.write("src/a.rs", OLD)
        scope = self.scope(self.git("rev-parse", "HEAD"))
        self.assertEqual(scope, {"src/a.rs": {4, 5}})
        self.assertEqual([x.line for x in rl.findings(self.top, scope, None)], [5])

    def test_fix_adds_blank_lines_only_where_changed(self):
        self.write("src/a.rs", OLD + "\n" + NEW)
        with redirect_stdout(io.StringIO()):
            rl.fix(self.top, self.scope())

        fixed = OLD + "\n" + NEW.replace("}\n    c", "}\n\n    c")
        self.assertEqual((self.top / "src/a.rs").read_bytes().decode(), fixed)

    def test_fix_keeps_crlf(self):
        self.write("src/b.rs", NEW.replace("\n", "\r\n"))
        with redirect_stdout(io.StringIO()):
            rl.fix(self.top, self.scope())

        fixed = NEW.replace("}\n    c", "}\n\n    c").replace("\n", "\r\n")
        self.assertEqual((self.top / "src/b.rs").read_bytes(), fixed.encode())

    def test_warnings_show_once(self):
        self.write("src/b.rs", LONG)
        warns = [x for x in rl.findings(self.top, self.scope(), None) if x.warn]
        self.assertEqual(len(rl.unseen(self.top, warns)), 1)
        self.assertEqual(rl.unseen(self.top, warns), [])

    def commit(self, files: dict[str, str]) -> str:
        for path, text in files.items():
            self.write(path, text)

        self.git("add", ".")
        self.git("commit", "-qm", "base")
        return self.git("rev-parse", "HEAD")

    def test_long_fn_ratchet(self):
        def fn(name: str, n: int) -> str:
            return f"fn {name}() {{\n" + "    x();\n\n" * n + "}\n"

        since = self.commit({"src/a.rs": fn("old", 105)})

        def rules() -> list[str]:
            hits = rl.findings(self.top, self.scope(since), since)
            return [f"{x.line} {x.msg}" for x in hits if x.rule == "long-fn"]

        self.write("src/a.rs", fn("old", 105).replace("x();", "y();", 1))
        self.assertEqual(rules(), [])
        self.write("src/a.rs", fn("old", 106))
        self.assertEqual(rules(), ["1 `old` has 106 lines of code; split it"])
        self.write("src/a.rs", fn("old", 105) + fn("new", 101))
        self.assertEqual(rules(), ["213 `new` has 101 lines of code; split it"])

    def test_tangles(self):
        since = self.commit(
            {
                "src/lib.rs": "mod a;\nmod b;\nmod c;\n",
                "src/a.rs": "use crate::b::x;\n",
                "src/b.rs": "pub fn x() {}\n",
                "src/c.rs": "",
            }
        )
        self.write("src/b.rs", "use crate::c::y;\npub fn x() {}\n")
        self.write("src/c.rs", "use crate::a::z;\n")
        hits = rl.findings(self.top, self.scope(since), since)
        self.assertEqual(
            [str(x) for x in hits],
            [
                "src/b.rs:1: warning[cycle]: `b` now uses `c`, which uses it back: b -> c -> a -> "
                "b; move what both need into a module of their own, or keep it if it's wanted",
            ],
        )
        hits = rl.findings(self.top, None, None)
        self.assertEqual(
            [str(x) for x in hits],
            ["src/a.rs:1: warning[cycle]: modules a, b, c use each other in a cycle"],
        )
        outline = "src/lib.rs\n  a -> b 1\n  b -> c 1\n  c -> a 1\n  cycle: a, b, c\n"
        self.assertEqual(self.check("modules"), (0, outline))
        since = self.commit({})
        self.write("src/a.rs", "use crate::b::x;\n\nfn f() {}\n")
        self.assertEqual(rl.findings(self.top, self.scope(since), since), [])

    def check(self, *args: str) -> tuple[int, str]:
        argv = ["rust-lint", *args, "--no-clippy"]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("pathlib.Path.cwd", return_value=self.top),
            redirect_stdout(io.StringIO()) as out,
        ):
            code = rl.main()

        return code, out.getvalue()

    def test_check(self):
        since = self.git("rev-parse", "--short=7", "HEAD")
        self.assertEqual(
            self.check(), (0, f"rust-lint: no errors in the lines changed since {since}\n")
        )
        self.write("src/b.rs", NEW + LONG)
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("src/b.rs:5: error[blank-line]", out)
        self.assertIn("src/b.rs:8: warning[long-comment]", out)
        self.assertNotIn("src/a.rs", out)
        self.assertIn("rust-lint: 1 error, 1 warning in", out)
        code, out = self.check()
        self.assertNotIn("long-comment", out)
        self.assertIn("rust-lint: 1 error in the lines changed since", out)
        self.assertIn("(1 warning shown before, `--all` lists it)", out)
        code, out = self.check("--all")
        self.assertIn("src/a.rs:5: error[blank-line]", out)
        self.assertIn("src/b.rs:8: warning[long-comment]", out)
        self.assertIn("rust-lint: 2 errors, 1 warning in all lines\n", out)

    def hook(self, **pld) -> dict:
        stdin = io.StringIO(json.dumps({"cwd": str(self.top)} | pld))
        with mock.patch("sys.stdin", stdin), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rl.hook(), 0)

        return json.loads(out.getvalue()) if out.getvalue() else {}

    def test_hook(self):
        self.assertEqual(self.hook(), {})
        self.write("src/b.rs", NEW + LONG)
        self.write("src/c.rs", "fn c(x: Option<u8>) -> u8 {\n    x.unwrap()\n}\n")
        rep = self.hook()
        self.assertEqual(rep["decision"], "block")
        self.assertIn("src/c.rs:2: error[unwrap_used]", rep["reason"])
        self.assertIn("src/b.rs:5: error[blank-line]", rep["reason"])
        self.assertIn("src/b.rs:8: warning[long-comment]", rep["reason"])
        self.assertEqual(self.hook(stop_hook_active=True), {})
        rep = self.hook()
        self.assertIn("src/b.rs:5: error[blank-line]", rep["reason"])
        self.assertNotIn("long-comment", rep["reason"])
        self.write("Makefile", "check:\n\tcargo clippy\n")
        self.assertEqual(self.hook(), {})

    def test_hook_fails_open(self):
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(rl.hook(), 0)

    def test_clippy_report(self):
        self.write("src/a.rs", OLD + "\n" + NEW + TESTS)
        self.write("tests/t.rs", "fn t() {\n    x.unwrap();\n}\n")

        def msg(code: str, path: str, line: int) -> str:
            span = {"is_primary": True, "file_name": str(self.top / path), "column_start": 1}
            span |= {"line_start": line, "line_end": line}
            m = {"level": "warning", "code": {"code": code}, "message": code, "spans": [span]}
            m["rendered"] = f"{code} {path}:{line}\n"
            return json.dumps({"reason": "compiler-message", "message": m})

        out = [
            msg("clippy::unwrap_used", "src/a.rs", 3),  # an unchanged line
            msg("clippy::unwrap_used", "src/a.rs", 9),
            msg("clippy::unwrap_used", "src/a.rs", 9),  # the same message from the test target
            msg("clippy::unwrap_used", "src/a.rs", 17),  # in mod tests
            msg("clippy::unwrap_used", "tests/t.rs", 2),
            msg("clippy::needless_return", "src/a.rs", 3),  # not strict: counts anywhere
            '{"reason": "build-finished", "success": false}',
        ]
        with redirect_stdout(io.StringIO()) as printed:
            failed = rl.report(self.top, self.scope(), "\n".join(out))

        self.assertEqual(failed, 2)
        rendered = "clippy::unwrap_used src/a.rs:9\nclippy::needless_return src/a.rs:3\n"
        self.assertEqual(printed.getvalue(), rendered)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rl.report(self.top, None, "\n".join(out)), 3)


if __name__ == "__main__":
    unittest.main()
