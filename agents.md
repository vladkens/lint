# lint

The linters of vladkens' projects, one file per language, run from their raw GitHub URL with `uv run --script`: `rust.py` for Rust. Agents carry out each change end to end by following this file.

Each fact has one source: what a linter checks and its commands are its docstring, the user's view of them `readme.md`, the checks of this repository the `Makefile`.

## Scope

- One file per language, `<lang>.py`: Python 3.11 with the standard library only, working the same on macOS, Linux and Windows.
- A linter checks only what the language's own linter and formatter can't, and never runs them: the project's `Makefile` does. It may write their settings into a project on request.
- Projects run `https://raw.githubusercontent.com/vladkens/lint/main/<lang>.py`: whatever lands on main reaches every project's next `make check`, CI run and agent hook at once.
- So a new rule counts only on the lines a branch changes, and nothing that passes today fails tomorrow on old lines.
- The commands, flags, output and hook protocol change only with the person's approval: Makefiles and CI depend on them.
- No dependencies, no package, no install step, no generated files.

## Code

- A linter reads git through `git` subprocesses and files as bytes decoded as UTF-8, so Windows and CRLF files behave as elsewhere.
- Constants with the thresholds sit at the top, the rules follow, the commands come last. `# MARK: <name>` separates blocks.
- A message names the rule and says what to do, in plain words. A warning is for what can be right; an error for what never is.
- The hook fails open: its own error never blocks an agent, and it lets the agent stop the second time.
- A comment adds what the code doesn't say, briefly. It never restates the code.

## Tests

- `test_<lang>.py` next to its linter, with `unittest`: rule fixtures marked `// expect: <rule>`, and commands in a scratch git repository in a temp folder.
- A new rule or fix comes with a test that fails without it.
- Before changing a rule, run the linter with `--all` on a few of the person's projects and say in the report what it finds there.

## Workflow

- Before committing, run `make check` and `make test`. `make prepare` rewrites files: not a check.
- A commit is one meaningful change a person can check: one short lowercase line, no body, docs updated in the same commit. When fixing your own unpushed commit, amend it.
- Ask the person only when the commands or output must change, or the request is ambiguous. Otherwise decide and say so in your report.
- CI runs the tests on Python 3.11 on `ubuntu-latest`, `macos-latest`, `windows-latest`, never pinned.
- Keep docs to `readme.md` (users), the linters' docstrings and `agents.md`. No progress journals or run logs.
