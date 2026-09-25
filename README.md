# argcomplete-booster

Fast TAB completion for `argparse` + [argcomplete](https://kislyuk.github.io/argcomplete/) CLIs whose imports are slow.

On a TAB press, argcomplete normally starts your CLI, imports everything, and builds the whole parser. The booster
answers from a cached spec of the parser instead, using the real argcomplete, without importing your package.
It falls back to your real CLI when code has to run (dynamic completers), and it refreshes itself automatically after
`pip install -U`.

| CLI import time 500 ms | plain argcomplete | with booster |
|---|---|---|
| 100 commands | 562 ms | **34 ms** |
| 12,000 commands | 3,220 ms | **34 ms** |

See [DESIGN.md](DESIGN.md) for the full design, alternatives and measurements.

## For CLI maintainers

1. Add a tiny entry module that does **not** import your package:

   ```python
   # src/mycli_entry.py
   # PYTHON_ARGCOMPLETE_OK
   from argcomplete_booster import boost

   main = boost("mycli.cli:main")   # your existing entry point, unchanged
   ```

2. Point the console script at it and add the dependency:

   ```toml
   [project]
   dependencies = ["argcomplete>=3.3", "argcomplete-booster"]

   [project.scripts]
   mycli = "mycli_entry:main"

   [tool.setuptools]
   py-modules = ["mycli_entry"]
   ```

That's it. Your parser code and your `argcomplete.autocomplete(parser)` call stay as they are, and so do your users'
shell setups.

Optional:

* `@argcomplete_booster.static_completer` on completers whose output depends only on the installed version, so they
  are cached too.
* Ship a spec for a fast first TAB: `argcomplete-booster generate mycli.cli:main -o src/mycli_completion.json`,
  `boost(..., spec_file=...)`, and `argcomplete-booster check ...` in CI.
* Guard fidelity in tests: `argcomplete_booster.testing.assert_parity("mycli.cli:main", ["mycli ", "mycli dep"])`.

## For users

Nothing new. Register completion as usual, once:

```bash
eval "$(register-python-argcomplete mycli)"
```

| Environment variable | Effect |
|---|---|
| `ARGCOMPLETE_BOOSTER=0` | disable the fast path |
| `ARGCOMPLETE_BOOSTER_DEBUG=1` | print cache decisions to stderr |
| `ARGCOMPLETE_BOOSTER_CACHE_DIR` | cache location (default `$XDG_CACHE_HOME/argcomplete-booster`) |
| `ARGCOMPLETE_BOOSTER_SYNC=1` | write the cache before exiting instead of in the background |

## Development

```bash
pip install -e '.[test]' && pytest
python benchmarks/bench.py
pip install -e examples/heavycli   # demo: `heavycli` (boosted) vs `heavycli-plain`
```
