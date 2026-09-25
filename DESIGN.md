# argcomplete-booster: design

**Goal:** make TAB completion fast for Python CLIs built on `argparse` + [`argcomplete`](https://kislyuk.github.io/argcomplete/),
even when importing the CLI takes hundreds of milliseconds, at close to zero cost for the CLI's maintainers.

**Result (reference implementation in this repository):** a TAB press on a CLI that takes 500 ms to import drops from
**~560 ms to ~35 ms**. The time stays flat as the command tree grows: 12,000 commands complete in the same ~35 ms, down from 3.2 s.
After `pip install -U`, completion picks up new commands with no action from the user. Adopting it takes one new
3-line module and a one-line change to the entry point.

---

## 1. Problem

### 1.1 Anatomy of one TAB press with argcomplete

```
bash ──(complete -F _python_argcomplete mycli)──▶ fork/exec `mycli` with _ARGCOMPLETE=1, COMP_LINE, COMP_POINT
      │
      ├─ Python interpreter start-up                                ~12 ms
      ├─ console script: `from mycli.cli import main`               100 ms … 1 s+   ◀── the problem
      │     (SDK, requests, botocore, pydantic, plugin discovery…)
      ├─ main(): build the ArgumentParser                            1 ms … 1 s (large trees)
      ├─ argcomplete.autocomplete(parser): parse COMP_LINE,
      │     find completions, write them to fd 8, os._exit(0)          ~2 ms
      ▼
bash reads fd 8 → COMPREPLY
```

The shell starts a new process for every TAB press, so every press pays for all of the CLI's imports and builds the
whole parser again. `import requests` alone costs ~100 ms. Users notice any delay above ~100 ms.

Measured (Python 3.11, argcomplete 3.7, `benchmarks/bench.py`, CLI import simulated as 500 ms):

| command tree | plain argcomplete |
|---|---|
| 100 commands | 562 ms |
| 1,000 commands | 732 ms |
| 12,000 commands (AWS-CLI-sized) | 3,220 ms |

Building the parser matters for large trees: most of the 3.2 s is `add_parser`/`add_argument` calls, not imports.

### 1.2 Global completion adds its own cost

With `activate-global-python-argcomplete`, bash first runs **another Python process**
(`python -m argcomplete._check_console_script`) on every TAB press. It checks that the console script is argcomplete-enabled
by scanning every installed distribution's entry points: **~65 ms**, and more as more packages are installed. That cost is
inside argcomplete's shell code, so no Python-side solution can remove it. We recommend per-command registration
(`eval "$(register-python-argcomplete mycli)"`), which skips the check (see §8).

## 2. Requirements

| # | Requirement (from the task) | How it is met |
|---|---|---|
| 1 | Import time of the SDK/CLI must not be paid on TAB | Completion is answered from a cached **spec** of the parser. The CLI package is never imported on a cache hit (§4). |
| 2 | `pip install` distribution; a one-time extra setup command is OK; **updates must be automatic** | The cache fingerprints the installed files and the `site-packages` directories. Any `pip install -U` invalidates it, and the next TAB rebuilds it (§6). Nothing to re-run. |
| 3 | Bash completion must not break after the CLI is updated | The shell registration (`register-python-argcomplete mycli`) is unchanged and only calls `mycli`. All booster logic runs inside the process. Any failure falls back to the normal argcomplete path (§7). |
| 4 | Low cost of adoption for maintainers of argcomplete CLIs | No change to parser code or completers. Add a 3-line entry module and point the console script at it (§5). |
| 5 | Publishing is out of scope | — |
| 6 | Audience: CLI developers | A library + a maintainer tool (`argcomplete-booster generate/check/parity`) + a pytest helper. End users do nothing new. |

Non-goals: installing shell hooks automatically (explicitly out of scope), replacing argcomplete, making the CLI's
normal (non-completion) start-up faster.

## 3. Options considered

| Option | Latency | Stays correct after upgrade | Maintainer cost | Verdict |
|---|---|---|---|---|
| **A. Lazy imports in the CLI** (import SDK inside command handlers) | good if done fully | yes | **high**: touches the whole codebase; regresses easily; plugin/entry-point discovery still costs | Recommended hygiene, but not a product |
| **B. Generate a static bash completion script** at install time | best (~5 ms, no Python) | **no**: the script is a snapshot. Re-generation needs a post-install hook, which pip wheels don't have. Breaks req. 2/3 | medium | Rejected |
| **C. Long-lived completion daemon/server** holding the imported CLI | good after warm-up | hard: must detect upgrades, restart, one per venv | low | Rejected: process management, stale state, security (socket), surprising for users |
| **D. Pre-fork "zygote" / import snapshot** | good | fragile | low | Rejected: platform-specific; no stable CPython support |
| **E. Cache a declarative *spec* of the parser; answer TAB from the spec using the real argcomplete; fall back to the real CLI when code must run** | **~35 ms** | **yes** (fingerprint) | **very low** | **Chosen** |

Option E keeps argcomplete as the single source of completion semantics. Only the expensive part (imports and parser
construction) is replaced by loading data.

## 4. Architecture

```
                         ┌──────────────────────── process started by bash on TAB ─────────────────────────┐
console script           │                                                                                  │
`mycli` ──▶ mycli_entry.main()  (imports only argcomplete_booster: os, sys)                                  │
                         │                                                                                  │
            _ARGCOMPLETE set? ── no ──▶ import mycli.cli; run main()  (normal CLI run, overhead < 1 ms)      │
                         │ yes                                                                              │
                         ▼                                                                                  │
            load spec (~/.cache/argcomplete-booster/<prog>-<key>.json)                                      │
            valid & fingerprint fresh? ──────── no (miss / stale / corrupt) ─────────┐                      │
                         │ yes                                                       │                      │
                         ▼                                                           ▼                      │
            FAST PATH                                                   SLOW PATH (= today's behaviour)     │
            • rebuild a skeleton ArgumentParser from the spec           • hook argcomplete.CompletionFinder │
              (only subcommands named on the line)                      • import mycli.cli; run main()      │
            • argcomplete.CompletionFinder()(skeleton) ──▶ fd 8, exit   • main() calls autocomplete(parser) │
                         │                                                ─▶ completions to fd 8           │
            a completer needs real code (dynamic)?                        ─▶ on exit: fork a detached child │
                         └── raise NeedSlowPath ──────────────────────▶      that serializes the parser to │
                                                                             the cache (user does not wait) │
                         └──────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Components (`src/argcomplete_booster/`)

| Module | Runs on | Responsibility |
|---|---|---|
| `__init__.py`, `_boost.py` | every invocation | `boost(target)` builds the console-script `main`. It decides fast or slow path, installs the capture hook, and writes the cache from a detached child. Imports only `os`/`sys` at module level. |
| `_cache.py` | fast path | Cache location, environment key, fingerprint check, sharded file format, atomic writes. Needs only `json`, `zlib`. |
| `_rebuild.py` | fast path | Spec → lightweight `argparse.ArgumentParser` that argcomplete can drive; placeholder for dynamic completers. |
| `_spec.py` | slow path / tooling | Parser + `autocomplete()` arguments → spec; fingerprint collection. |
| `testing.py` | maintainers' tests | `assert_parity(target, lines)`: compares real vs. fast completions. |
| `cli.py` | maintainers | `argcomplete-booster generate / check / parity`. |

### 4.2 Why drive the *real* argcomplete with a rebuilt parser?

Completion behavior is subtle: shell lexing and quoting, `COMP_WORDBREAKS`, `--opt=value`, nargs rules, mutually
exclusive groups, `--`, REMAINDER, aliases, and zsh/fish/tcsh/powershell output formats. argcomplete learns about the
parser by **introspecting a live `ArgumentParser`** (it monkey-patches actions and runs `parse_known_args`). So we give
it a live parser made from data, and reuse all of that behavior instead of re-implementing it. The reproduction is
exact because the rebuilt parser has the same actions, in the same order, with the same `nargs`, option strings,
choices, mutex groups and help texts. The parity tests check this across 47 command lines × 5 integration styles ×
3 shells (§9).

Fast-path cost breakdown (cached, CLI with 384 commands): interpreter start ~12 ms, `import argcomplete` ~15 ms
(pulls in argparse, subprocess, re), read + validate spec ~2 ms, rebuild skeleton parser ~0.5 ms.

### 4.3 The spec

A JSON tree mirroring `parser._actions`:

```json
{"header": {"format": 1, "booster": "0.1.0", "target": "mycli.cli:main", "python": "3.11", "prefix": "/home/u/.venv"},
 "fingerprint": [["/home/u/.venv/lib/python3.11/site-packages", 1727262000123456789, 4096], ["…/mycli/cli.py", …]],
 "fast": true, "prune": true, "finder": "default",
 "autocomplete": {"always_complete_options": true, "print_suppressed": false, "default_completer": {"kind": "files", …}},
 "parser": {"prog": "mycli", "prefix_chars": "-", "actions": [
    {"kind": "help", "dest": "==SUPPRESS==", "opts": ["-h", "--help"], "nargs": 0, "help": "show this help message and exit"},
    {"kind": "store", "dest": "profile", "opts": ["--profile"], "choices": ["default", "prod"], "help": "profile to use"},
    {"kind": "store", "dest": "region", "opts": ["--region"], "completer": {"kind": "dynamic", "name": "region_completer"}},
    {"kind": "subparsers", "dest": "service", "prog": "mycli", "choices": [
       {"names": ["compute", "com"], "help": "manage compute", "parser": {"$shard": [0, 5120]}}, …]}],
  "mutex": [{"required": false, "actions": [7, 8]}]}}
```

What gets captured:

* **Actions**: the built-in action classes by kind. Any other class becomes an *opaque* action with the same `nargs`,
  and it is marked `safe` if the CLI added it to `argcomplete.safe_actions`. This reproduces exactly how argcomplete
  treats custom actions: it never runs them unless they are safe.
* **Choices**: kept when they are JSON scalars and the `type` is `None`/`str`/`int`/`float`. Otherwise only their
  string forms are kept, for display (see §7.2).
* **Help** is stored pre-expanded (`%(default)s`, `%(choices)s`) exactly as argcomplete would show it in zsh/fish, but
  only if the installed argcomplete expands help (≥ 3.7).
* **Completers:**
  * serializable completers (`ChoicesCompleter`, `FilesCompleter`, `DirectoriesCompleter`, `SuppressCompleter`,
    `EnvironCompleter`) are stored as data. `EnvironCompleter` reads the environment of the *current* process.
  * `@static_completer`-decorated functions are evaluated once and their output is stored.
  * anything else is stored as `{"kind": "dynamic"}`.
* **`autocomplete(...)` arguments**: `always_complete_options`, `exclude`, `print_suppressed`, `append_space`,
  `default_completer`, and whether `ExclusiveCompletionFinder` is used. A custom `validator` or a custom
  `CompletionFinder` subclass marks the spec `"fast": false`. That CLI then always takes the slow path: correct, just
  not faster.

**Sharding.** A subcommand subtree of 2 KB or more is moved out of the head of the file into a shard. The file is one
JSON line (the head) followed by the raw shards, addressed by `[offset, length]`. The fast path parses the head plus
only the shards for subcommands named on the command line. That is why the 12,000-command CLI (11 MB of spec)
completes in 34 ms rather than ~330 ms with a single JSON document.

**Pruning.** When rebuilding, a subcommand whose names do not appear in the words of `COMP_LINE` (split with
argcomplete's own lexer) becomes a stub object. argcomplete only reads its name and help. argparse can only descend
into a subparser named by one of those words, so this is exact. `fromfile_prefix_chars` (`@args.txt`) can bring in
words we cannot see, so any such parser in the tree turns pruning off.

### 4.4 Dynamic completers: fall back to the real CLI

A completer like `lambda prefix, parsed_args, **kw: api.list_instances(...)` needs the CLI's code and possibly the
network, so it can't be cached. The rebuilt action gets a `DynamicCompleter` placeholder instead. argcomplete only calls
completers of the argument being completed, so the placeholder fires only when the user actually completes that
argument (e.g. `mycli --region <TAB>`). Completing options, subcommands and static choices stays on the fast path.

When it fires, it raises `NeedSlowPath`, a `BaseException` so argcomplete's own `except Exception` handlers can't
swallow it. `boost()` catches it and runs the real CLI in the same process. The fast path opened fd 8 with
`closefd=False` and keeps argcomplete's fd 9 debug-stream wrapper alive, so the real argcomplete can use both
descriptors again. The result is the same as without the booster, plus the few ms spent on the fast attempt. argcomplete
3.3+ is required, because older releases catch every exception raised by completers.

Maintainers can move a completer to the fast path by marking it `@static_completer`, when its output depends only
on the installed version (e.g. a list of output formats).

### 4.5 Capturing the parser without API changes

On the slow path, `boost()` wraps `argcomplete.CompletionFinder.__call__` before importing the CLI.
`argcomplete.autocomplete` is an instance of that class, so the wrapper sees the parser and the `autocomplete()`
arguments whether the CLI does `argcomplete.autocomplete(parser)`, `from argcomplete import autocomplete`, or uses
`ExclusiveCompletionFinder`. The wrapper also wraps `exit_method`. After argcomplete has written the completions, the
process **forks a detached child** (new session, stdio → `/dev/null`, all other fds closed so bash's pipe sees EOF).
The child serializes the parser and writes the cache while the parent exits right away. A cache miss is therefore
exactly as fast as plain argcomplete. On platforms without `fork`, or with `ARGCOMPLETE_BOOSTER_SYNC=1`, the write
happens before exit.

## 5. Integration for CLI maintainers (requirement 4)

### 5.1 Minimum integration: 2 files touched

```python
# src/mycli_entry.py   (new, top-level module so that importing it doesn't import `mycli`)
# PYTHON_ARGCOMPLETE_OK
from argcomplete_booster import boost

main = boost("mycli.cli:main")
```

```toml
# pyproject.toml
[project]
dependencies = ["argcomplete>=3.3", "argcomplete-booster"]

[project.scripts]
-mycli = "mycli.cli:main"
+mycli = "mycli_entry:main"

[tool.setuptools]
py-modules = ["mycli_entry"]        # or the equivalent for hatch/poetry/flit
```

Nothing else changes: the parser code, completers, `argcomplete.autocomplete(parser)` call and the user's shell
registration all stay the same.

Why a separate module: the console script imports the module that holds `main`, and importing `mycli.entry` would run
`mycli/__init__.py`, which in SDK-style packages usually imports the heavy parts. If `mycli/__init__.py` is already
light, `mycli/_entry.py` works just as well. The `# PYTHON_ARGCOMPLETE_OK` marker has to be in that module for users of
argcomplete's *global* completion. argcomplete looks for it in the module the console script imports.

### 5.2 Optional: ship the spec in the wheel (zero-latency first TAB)

```python
main = boost("mycli.cli:main", spec_file=os.path.join(os.path.dirname(__file__), "mycli_completion.json"))
```

```bash
argcomplete-booster generate mycli.cli:main -o src/mycli_completion.json   # at release time
argcomplete-booster check    mycli.cli:main src/mycli_completion.json      # in CI: fails if stale
```

The shipped spec comes from the same wheel as the code, so it can't go stale for users. It is used as-is and the user
cache is never written, which also helps with read-only home directories and containers. The price is one generation
step in the release process, enforced by `check` in CI.

### 5.3 Optional: one test to guard fidelity

```python
from argcomplete_booster.testing import assert_parity

def test_completion_parity():
    assert_parity("mycli.cli:main", ["mycli ", "mycli dep", "mycli deploy --", "mycli deploy --env "])
```

`argcomplete-booster parity mycli.cli:main "mycli " "mycli deploy --"` prints the same comparison from the command
line, and marks lines that will use the dynamic fallback.

### 5.4 Cost summary

| Item | Required? | Effort |
|---|---|---|
| New 3-line entry module + entry point change + dependency | yes | minutes |
| `@static_completer` on version-bound completers | no | minutes |
| Shipped spec + CI `check` | no | one CI step |
| Parity test | no (recommended) | one test |
| Changes to parser/completer code | **never** | — |

**Things that silently make a CLI use the slow path** (still correct, just not faster): a custom `validator`, a
`CompletionFinder` subclass, a completer that is not one of argcomplete's built-ins or `@static_completer` (only for
that argument), or a CLI that never calls `argcomplete.autocomplete`. `ARGCOMPLETE_BOOSTER_DEBUG=1` prints why.

## 6. Freshness: why upgrades are automatic (requirements 2 and 3)

The cache is keyed by `(target, sys.prefix, Python major.minor, booster version, spec format)`. So each virtualenv,
pipx venv and interpreter gets its own entry, stored under `$XDG_CACHE_HOME/argcomplete-booster/`
(override: `ARGCOMPLETE_BOOSTER_CACHE_DIR`).

Each spec also carries a **fingerprint**: `(path, mtime_ns, size)` for

1. every `site-packages`/`dist-packages` directory on `sys.path`. pip creates or removes a `*.dist-info` directory
   there on every install, upgrade, reinstall and uninstall, which changes the directory's mtime;
2. every source file and package directory of the CLI's top-level package that was imported while building the
   parser. This covers editable installs (`pip install -e`), where code changes without pip touching `site-packages`,
   and plugins dropped into a package directory.

The fast path `stat()`s these entries (typically 5–50; microseconds each). Any difference is treated as a miss.

| Scenario | Detected by | Outcome |
|---|---|---|
| `pip install -U mycli` (new commands) | site-packages mtime, file stat | 1st TAB: real CLI (as today), cache rebuilt in background; then fast. **Verified end-to-end** with a real non-editable upgrade adding a command. |
| `pip install --force-reinstall` same version | new dist-info dir → mtime | same |
| New plugin distribution installed (entry-point discovered commands) | site-packages mtime | same |
| Editable install, developer edits `cli.py` | file stat | same |
| Another package installed in the venv | site-packages mtime | one unnecessary rebuild (safe, cheap) |
| argcomplete / booster upgraded | site-packages mtime; `booster` in header | rebuild |
| Python upgraded / venv recreated | key includes `sys.prefix` + version | new cache entry |
| Two venvs with different versions of the CLI | key includes `sys.prefix` | independent entries |

We only ever get a false "stale" (costing one slow TAB), never a false "fresh" for pip-managed changes. The one
theoretical gap is code in the parser-building path that lives in *another* top-level package, edited in place outside
pip. That affects developers only, and `ARGCOMPLETE_BOOSTER=0` or deleting the cache file handles it.

**The shell side never changes.** `register-python-argcomplete mycli` produces shell code that only knows the command
name. The booster lives inside the Python process, so upgrading the CLI or the booster, or removing the booster, cannot
break the shell integration.

## 7. Robustness and correctness

### 7.1 Failure handling

Every failure on the fast path leads to the slow path, which is exactly today's behavior:

* missing, corrupt, truncated or mismatched spec → miss;
* exception while rebuilding or completing (e.g. a future argparse change) → caught, slow path, cache rewritten;
* unwritable cache directory (read-only home, a file in the way) → completion still works, just slow;
* concurrent TABs / writers → atomic `write tmp + os.replace`; readers see old or new, never partial;
* kill switch: `ARGCOMPLETE_BOOSTER=0`.

Normal CLI runs (no `_ARGCOMPLETE` in the environment) don't touch the cache. The only overhead is importing
`argcomplete_booster/__init__.py` (os/sys only, < 1 ms).

### 7.2 Known, accepted divergences

| Case | Behavior |
|---|---|
| Custom `type=` callable (e.g. `type=Color` enum) | not run on the fast path, so an **invalid** value typed earlier on the line doesn't stop parsing. Real argparse would stop and show top-level completions. Valid input is identical (pinned by a test). |
| Custom `ArgumentParser` subclass overriding parsing internals | the rebuilt parser is a plain `ArgumentParser`. Use `assert_parity` to check; set `ARGCOMPLETE_BOOSTER=0` or don't boost if it differs. |
| Dynamic completers | always correct (real CLI), but slow for that one argument. |
| argcomplete's global-completion check (~65 ms) | not ours to remove; recommend per-command registration. |

### 7.3 Security

The cache is JSON (no pickle), so reading it can't execute code. It lives in the user's own cache directory with
default permissions, and the worst a tampered cache can do is suggest wrong completions. The shipped spec is package
data, trusted like the package itself.

## 8. End-user experience

```bash
pip install mycli                                    # booster comes in as a dependency
eval "$(register-python-argcomplete mycli)"          # once, e.g. in ~/.bashrc (unchanged from today; out of scope)
pip install -U mycli                                 # nothing else to do, ever
```

Per-command registration is recommended over global activation: it skips argcomplete's extra `_check_console_script`
process (~65 ms per TAB). Both modes work. Measured through the real bash functions for the example CLI:

| | plain argcomplete | booster (cached) |
|---|---|---|
| `register-python-argcomplete heavycli` | ~610 ms | **~41 ms** |
| global activation | ~667 ms | ~115 ms (65 ms of it is argcomplete's check) |

zsh, fish, tcsh and PowerShell work through argcomplete itself. The parity suite runs under the bash, zsh and fish
output modes.

## 9. Validation

* **Parity suite** (`tests/test_parity.py`): a feature-heavy fixture CLI (count/append/extend/const/boolean-optional,
  custom and custom-*safe* actions, enum and `range` choices, mutex groups, aliases, nested subparsers, REMAINDER, `?`,
  `*`, suppressed help, `%`-help, every built-in completer, static and dynamic completers). 47 command lines ×
  5 integration styles (default, `always_complete_options="long"`+`exclude`, positional `autocomplete()` args,
  `ExclusiveCompletionFinder`, `fromfile_prefix_chars`) × bash/zsh/fish. The comparison includes zsh/fish descriptions.
* **End-to-end** (`tests/test_end_to_end.py`): real subprocesses. A package whose import is recorded in a log proves
  the fast path never imports it. Covers miss → hit, CLI change, site-packages change, dynamic fallback (cache kept),
  corrupt and unwritable cache, kill switch, normal runs, shipped spec + `check`, background writing, and the real
  fd 8/fd 9 protocol through bash, including a fallback after the fast path had opened fd 8.
* **Sharding** (`tests/test_cache.py`): only the needed shards are read; round-trip; truncation detected.
* Manual: real `pip install` → `pip install -U` of a non-editable wheel adding a command, completed through the
  functions generated by `register-python-argcomplete` and through global activation.
* CI matrix run locally: Python 3.9–3.14; argcomplete 3.3, 3.5, 3.6, 3.7.

Benchmark (`python benchmarks/bench.py`, CLI import simulated at 500 ms, median of 12 runs):

| command tree | spec size | plain argcomplete | booster, first TAB | booster, cached |
|---|---|---|---|---|
| 100 commands | 0.1 MB | 562 ms | 569 ms | **34 ms** |
| 1,000 commands | 0.9 MB | 732 ms | 757 ms | **35 ms** |
| 12,000 commands | 11.0 MB | 3,220 ms | 3,353 ms | **34 ms** |

## 10. Limitations and future work

* **Floor of ~30 ms** = interpreter start (~12 ms) + `import argcomplete` (~15 ms). A native fast path that skips
  argcomplete could reach ~15 ms, but it would have to re-implement argcomplete's semantics. Not worth the fidelity
  risk now.
* **Dynamic completer results with a TTL** (e.g. cache `list regions` for 1 h in the spec cache), opt-in per completer.
* **Cache garbage collection**: entries for deleted venvs are left behind (a few KB–MB each). Add age-based cleanup
  when writing.
* **Upstreaming**: the capture hook and the "complete from a rebuilt parser" idea could become an argcomplete feature
  (`argcomplete.autocomplete(..., cache=True)`). That would remove the entry-module step for maintainers entirely.
* Windows/`cmd` has no argcomplete support; PowerShell works but without `fork`, the cache is written synchronously on
  a miss.

## Appendix: repository layout

```
src/argcomplete_booster/   the library (see §4.1)
tests/                     parity, end-to-end, cache tests (pytest)
benchmarks/bench.py        latency benchmark (§9)
examples/heavycli/         a slow-to-import demo CLI wired up as in §5.1 (+ an unboosted `heavycli-plain` for comparison)
```
