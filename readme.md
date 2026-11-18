<div align="center">

# lint

**The checks my projects run in `make check`, CI and agent hooks**

[<img src="https://badges.ws/github/license/vladkens/lint" alt="license" />](https://github.com/vladkens/lint/blob/main/LICENSE)
[<img src="https://badges.ws/badge/follow-@uid127-black" alt="follow @uid127" />](https://x.com/uid127)

</div>

Each linter is one Python file with the standard library only. [uv](https://docs.astral.sh/uv/) runs it straight from its GitHub URL, so a project installs and copies nothing, on macOS, Linux and Windows alike.

A linter checks what the language's own linter and formatter can't, and keeps the set of their rules my projects take. It checks the lines a branch changes; uncommitted and untracked files count too.

| Linter    | For                                                                      |
| --------- | ------------------------------------------------------------------------ |
| `rust.py` | Rust: blank lines, long comments, dead code, unused `pub`, module cycles |

## ⚡ Quick start

In a Rust project's `Makefile`, next to rustfmt and clippy:

```make
LINT := uv run --script https://raw.githubusercontent.com/vladkens/lint/main/rust.py

prepare:
	cargo fmt
	$(LINT) fix

check:
	cargo fmt --check
	cargo clippy --all-targets -- -D warnings
	$(LINT)
```

Once, in the project, add the clippy lints to `Cargo.toml` and `clippy.toml`:

```sh
uv run --script https://raw.githubusercontent.com/vladkens/lint/main/rust.py lints
```

In CI, add uv before `make check`:

```yaml
- uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
- run: make check
```

There it fetches the full history if the checkout is shallow, compares a pull request with its base branch and a push to the default branch with the commit before it, and prints GitHub annotations.

## 🦀 rust.py

| Rule           | Finds                                                                       |
| -------------- | --------------------------------------------------------------------------- |
| `blank-line`   | a statement right after an `if` / `for` / `match` block, with no blank line |
| `dense-block`  | more than 12 statements in a row without a blank line                       |
| `long-comment` | more than 3 comment lines in a row; a warning, shown once on your machine   |
| `dead-code`    | `allow` / `expect` of `dead_code` or `unused_*`: delete unused code instead |
| `unused-pub`   | a new `pub` item that nothing else names, tests and doc examples included   |
| `cycle`        | a new `use` that makes two top modules of a crate use each other; a warning |
| `new-use`      | a note: a top module uses another for the first time                        |

Errors fail the check. A warning lets a comment or a cycle stay when it's needed. A field kept for its `Drop` or an FFI layout takes a name that starts with `_`.

`lints` adds these clippy lints, keeping what the project already sets:

| Lint                                                  | Against                                    |
| ----------------------------------------------------- | ------------------------------------------ |
| `unwrap_used`                                         | `.unwrap()` outside tests: `expect("why")` |
| `string_slice`, `unchecked_time_subtraction`          | panics on a char boundary or a clock       |
| `todo`, `unimplemented`, `dbg_macro`                  | unfinished code and debugging leftovers    |
| `allow_attributes`, `allow_attributes_without_reason` | `#[allow]`: `#[expect(x, reason = "why")]` |
| `too_many_lines`, `cognitive_complexity`              | functions over 100 lines or hard to follow |

| Command           | Does                                                    |
| ----------------- | ------------------------------------------------------- |
| `$(LINT)`         | checks the lines the branch changes                     |
| `$(LINT) --all`   | checks every line                                       |
| `$(LINT) fix`     | adds the missing blank lines                            |
| `$(LINT) modules` | prints what each top module uses, and the cycles        |
| `$(LINT) lints`   | adds the clippy lints to `Cargo.toml` and `clippy.toml` |

## 🤖 Agent hook

`rust.py hook` is a Stop hook for Claude Code and Codex. When the agent finishes, it runs the rules in a fraction of a second. On an error, or a warning it hasn't shown yet, it sends the agent back with the list. The second time it lets the agent stop, and its own failure never blocks the agent.

It works in projects whose `Makefile` mentions `vladkens/lint/`. In `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "timeout": 10,
            "command": "top=$(git rev-parse --show-toplevel 2>/dev/null) && grep -qs vladkens/lint/ \"$top/Makefile\" && uv run -q --script https://raw.githubusercontent.com/vladkens/lint/main/rust.py hook 2>/dev/null; true"
          }
        ]
      }
    ]
  }
}
```

The `grep` skips the download in other projects.

## 🛠 Development

```sh
make prepare  # format
make check    # ruff, ty, prettier
make test     # the tests, with a scratch git repository
```

## 📄 License

[MIT](LICENSE)
