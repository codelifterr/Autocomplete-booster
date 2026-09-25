# Autocomplete booster: fast argcomplete completion for slow-to-import Python CLIs

**Audience:** developers who maintain Python CLIs built on `argparse` + `argcomplete`.
**Deliverable:** the design of a package, `argcomplete-booster`, plus a working, tested reference
implementation: <https://github.com/codelifterr/Autocomplete-booster>.

## TL;DR

- **Problem.** Every TAB press starts the CLI from scratch. The shell waits while Python imports the
  whole SDK and builds the whole parser, often 0.5–3 s, just to print a few words.
- **Idea.** Save a description of the parser (a *spec*) the first time it is built. On later TAB
  presses, rebuild a lightweight parser from the spec and let the real `argcomplete` complete it,
  without importing the CLI at all.
- **Result.** A TAB press drops from **~560 ms to ~35 ms**, and stays at ~35 ms even for a CLI with
  12,000 commands (3.2 s without the booster).
- **Updates are automatic.** The saved spec is checked against the installed files on every TAB, so
  `pip install -U` is all a user ever does. The shell setup never changes.
- **Cheap to adopt.** A 3-line module and a one-line entry-point change. No change to parser code.

## 1. The problem

When the user presses TAB, bash runs the CLI again with a few environment variables set:

```
TAB ─▶ bash starts `mycli` (with _ARGCOMPLETE=1, COMP_LINE="mycli dep")
        ├─ start Python interpreter ................. ~12 ms
        ├─ `from mycli.cli import main` ............. 100 ms – 1 s+   ◀ SDK, requests, plugins
        ├─ build the ArgumentParser ................. 1 ms – 2 s      ◀ grows with command count
        └─ argcomplete prints completions, exits .... ~2 ms
```

`import requests` alone costs ~100 ms, and users notice any delay above ~100 ms. Measured with
plain argcomplete (CLI import simulated at 500 ms):

| Commands in the CLI | 100 | 1,000 | 12,000 |
|---|---|---|---|
| One TAB press | 562 ms | 732 ms | 3,220 ms |

## 2. Requirements and how they are met

| # | Requirement | How the design meets it |
|---|---|---|
| 1 | Import time (often 500 ms+) must not delay completion | The CLI package is never imported on a normal TAB press; completion is served from the saved spec (§3). |
| 2 | Installed with `pip install`; a one-time setup command is fine; **updates must be automatic** | The only setup is argcomplete's usual one-time `register-python-argcomplete mycli`. The spec is invalidated by any install/upgrade and rebuilt by itself (§5). |
| 3 | Completion must not break after the CLI is updated | The shell hook only knows the command name and never changes. Every error falls back to today's normal argcomplete behaviour (§6). |
| 4 | Low cost for maintainers of existing argcomplete CLIs | One new 3-line module, one changed line in `pyproject.toml`. No change to parsers or completers (§4). |
| 5 | Publishing is out of scope | Not covered. |
| 6 | The package targets CLI developers | Library + maintainer tooling (`generate`, `check`, `parity`) + a pytest helper. End users do nothing new. |

## 3. How it works

### 3.1 Overview

The CLI's console script points at a tiny wrapper from the booster instead of the real `main`. The
wrapper imports only `os` and `sys`, so it costs nothing on normal runs.

```
      mycli (console script) ─▶ booster wrapper
                                     │
       not completing ◀──────────────┼──────────────▶ completing (TAB)
             │                                             │
 import CLI, run main()                         spec saved and up to date?
(unchanged, < 1 ms extra)                          │                 │
                                                  yes               no
                                                   │                 │
                                     ┌─────────────▼───┐   ┌─────────▼──────────┐
                                     │ FAST PATH       │   │ SLOW PATH          │
                                     │ rebuild parser  │   │ = today: import    │
                                     │ from spec; real │   │ CLI, complete;     │
                                     │ argcomplete     │   │ then save the spec │
                                     │ answers ~35 ms  │   │ in the background  │
                                     └───────┬─────────┘   └────────────────────┘
                                             │ argument needs live code
                                             └──▶ hand over to the slow path
```

### 3.2 The spec

The spec is a JSON description of the parser tree: every argument's option strings, `nargs`,
choices, help text, mutually exclusive groups, subcommands and aliases, and which completer each
argument uses. It also records the options passed to `argcomplete.autocomplete()`.

- **Captured automatically.** On a slow-path run, the booster hooks argcomplete and receives the
  parser the CLI passes to `argcomplete.autocomplete(parser)`. Maintainers write no export code.
- **Saved after answering.** The spec is written by a detached background process *after* the
  completions are printed, so a first TAB costs about the same as without the booster.
- **Stored per user and per environment** in `~/.cache/argcomplete-booster/`, keyed by the CLI,
  the virtualenv and the Python version. Two venvs with different CLI versions never mix.
- **Split for large CLIs.** Each subcommand tree is stored as a separate chunk inside the file.
  A TAB press loads only the chunks for subcommands already typed on the line, which is why
  12,000 commands complete as fast as 100.

### 3.3 Why reuse the real argcomplete

Completion has many subtle rules: shell quoting, `--opt=value`, `nargs`, `--`, mutually exclusive
options, aliases, and the output formats of bash, zsh, fish and PowerShell. Re-implementing them
would drift from argcomplete. Instead, the booster rebuilds a real (but empty) `ArgumentParser` from
the spec and hands it to the real argcomplete. Completion behaviour is therefore identical by
construction; a parity test suite confirms it (§7).

### 3.4 Arguments that need live code

Some completers must run the CLI's code, for example one that lists cloud regions from an API.
These are recorded in the spec as "dynamic". Only when the user completes *that* argument does the
fast path hand over to the slow path, which gives the same result as today. Everything else
(options, subcommands, fixed choices, file paths) stays fast.

A maintainer can make a completer fast as well by marking it `@static_completer` when its output
only changes between releases (e.g. a list of output formats); its result is then saved in the spec.

## 4. Adopting it in an existing CLI

**Step 1.** Add a small entry module that does *not* import the CLI package:

```python
# src/mycli_entry.py
# PYTHON_ARGCOMPLETE_OK
from argcomplete_booster import boost

main = boost("mycli.cli:main")   # the existing entry point, unchanged
```

It is a separate top-level module because importing `mycli.anything` would run
`mycli/__init__.py`, which in SDK-style packages usually imports the heavy parts. (If that file is
already light, `mycli/_entry.py` works too.) The marker comment keeps argcomplete's global
completion mode working.

**Step 2.** Point the console script at it and add the dependency:

```toml
[project]
dependencies = ["argcomplete>=3.3", "argcomplete-booster"]

[project.scripts]
mycli = "mycli_entry:main"          # was: "mycli.cli:main"

[tool.setuptools]
py-modules = ["mycli_entry"]        # or the equivalent for hatch / poetry / flit
```

That is all. Parser code, completers, the `argcomplete.autocomplete(parser)` call and users' shell
setup stay exactly as they are.

**Optional extras**

| Extra | What it gives | Cost |
|---|---|---|
| `@static_completer` on release-bound completers | those arguments complete fast too | minutes |
| Ship the spec in the wheel (`argcomplete-booster generate`) + `argcomplete-booster check` in CI | fast from the very first TAB; works with read-only home directories | one CI step |
| `assert_parity("mycli.cli:main", ["mycli ", "mycli dep"])` in the test suite | proves fast completions equal real ones | one test |

**When the booster steps aside.** A custom `validator=`, a custom `CompletionFinder` subclass, or a
CLI that never calls `argcomplete.autocomplete()` makes it always use the slow path. Completion stays
correct, just not faster. `ARGCOMPLETE_BOOSTER_DEBUG=1` prints why.

## 5. Automatic updates

Each saved spec includes a fingerprint: the modification time and size of

1. the `site-packages` directories. pip adds or removes a `*.dist-info` folder there on every
   install, upgrade, reinstall and uninstall, so any pip change is detected;
2. the CLI's own source files that were loaded when the parser was built. This catches editable
   installs (`pip install -e`), where code changes without pip being involved.

Checking the fingerprint is a handful of `stat()` calls (microseconds). If anything differs, that
TAB press takes the slow path and the spec is rebuilt in the background.

| What happens | Result |
|---|---|
| `pip install -U mycli` adds commands | 1st TAB: as slow as today, spec rebuilt; then fast. *Verified with a real upgrade.* |
| Reinstall, plugin installed, argcomplete or booster upgraded | Detected, rebuilt once |
| Unrelated package installed in the same venv | Rebuilt once (harmless; errs on the side of fresh) |
| Developer edits code in an editable install | Detected through source file times |
| New Python version or new virtualenv | Separate spec; nothing shared |

The shell side never needs updating: `register-python-argcomplete mycli` only knows the name
`mycli`, and all booster logic runs inside the Python process.

## 6. Safety and correctness

**Nothing can make completion worse than today.** Any problem on the fast path (missing, stale,
corrupt or truncated spec; an unexpected error; an unwritable cache directory) falls back to
normal argcomplete. Specs are written atomically, so concurrent TAB presses never read a partial
file. `ARGCOMPLETE_BOOSTER=0` turns the booster off.

**Security.** The spec is plain JSON (no pickle), stored in the user's own cache directory, so
reading it cannot execute code. At worst, a tampered spec suggests wrong completions.

**Known, accepted differences**

| Case | Behaviour |
|---|---|
| Custom `type=` functions (e.g. `type=Color`) | Not run on the fast path. Only matters if an *invalid* value is already typed earlier on the line; valid input completes identically. |
| Custom `ArgumentParser` subclasses that change parsing | Rebuilt as a plain `ArgumentParser`; the parity helper shows whether that matters. |
| argcomplete's global activation mode | argcomplete itself spends ~65 ms per TAB checking the script; per-command registration avoids that. |

## 7. Validation and results

Benchmark (median of 12 runs, CLI import simulated at 500 ms, Python 3.11):

| Commands in the CLI | Plain argcomplete | Booster, first TAB | Booster, afterwards |
|---|---|---|---|
| 100 | 562 ms | 569 ms | **34 ms** |
| 1,000 | 732 ms | 757 ms | **35 ms** |
| 12,000 | 3,220 ms | 3,353 ms | **34 ms** |

Where the remaining ~35 ms goes: Python start-up ~12 ms, importing argcomplete ~15 ms, reading the
spec ~2 ms, rebuilding the parser ~0.5 ms.

Tests in the reference implementation (36, all passing on Python 3.9–3.14, argcomplete 3.3–3.7):

- **Parity:** 47 command lines × 5 ways of calling argcomplete × bash/zsh/fish output, compared
  with plain argcomplete, including help descriptions.
- **End-to-end:** real subprocesses prove the CLI is never imported on a fast TAB, and cover cache
  miss/hit, CLI changes, pip changes, dynamic completers, corrupt and unwritable caches, the off
  switch, shipped specs, background writing, and argcomplete's real file-descriptor protocol.
- **Manual:** a real `pip install -U` of a new CLI version adding a command, completed through the
  shell functions argcomplete generates.

## 8. Alternatives considered

| Option | Why not chosen |
|---|---|
| Ask maintainers to make imports lazy | Touches the whole codebase and regresses easily; parser construction and plugin discovery still cost time. Good hygiene, but not a product. |
| Generate a static bash completion script at install time | pip wheels have no post-install hook, so the script goes stale after an upgrade (breaks requirements 2 and 3). Cannot run dynamic completers. |
| A background completion daemon holding the CLI in memory | Needs process management, upgrade detection and a socket per environment; stale state and security concerns. |
| Snapshot a warmed-up interpreter (pre-fork "zygote") | Platform-specific and not supported by CPython. |
| **Spec cache driving the real argcomplete** (chosen) | Fast, stays correct after upgrades, near-zero adoption cost. |

## 9. Limitations and future work

- **First TAB after an upgrade** is as slow as today (unless the spec is shipped in the wheel).
- **~30 ms floor** from starting Python and importing argcomplete. Skipping argcomplete could reach
  ~15 ms but would mean re-implementing its rules; not worth the risk now.
- **Dynamic completers** stay slow. Next step: opt-in caching of their results with a time limit
  (e.g. "list regions" cached for an hour).
- **Old cache entries** from deleted virtualenvs are not cleaned up yet (a few KB–MB each).
- **Upstreaming:** the same idea could live inside argcomplete itself
  (`argcomplete.autocomplete(parser, cache=True)`), removing even the entry-module step.

## Appendix: reference implementation

<https://github.com/codelifterr/Autocomplete-booster>

| Path | Contents |
|---|---|
| `src/argcomplete_booster/` | the library: wrapper, spec capture, cache, parser rebuild, maintainer CLI |
| `tests/` | parity, end-to-end and cache tests (`pytest`) |
| `benchmarks/bench.py` | reproduces the numbers in §7 |
| `examples/heavycli/` | a demo CLI with a 500 ms import, boosted (`heavycli`) and plain (`heavycli-plain`) |

User-facing switches: `ARGCOMPLETE_BOOSTER=0` (off), `ARGCOMPLETE_BOOSTER_DEBUG=1` (explain
decisions), `ARGCOMPLETE_BOOSTER_CACHE_DIR` (cache location).
