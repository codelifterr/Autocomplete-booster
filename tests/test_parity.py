"""The fast path must produce exactly what argcomplete produces on the real parser."""

import os

import pytest

from argcomplete_booster.testing import compare

LINES = [
    "fx ",
    "fx -",
    "fx --",
    "fx --v",
    "fx -vv --",
    "fx --level ",
    "fx --level=",
    "fx --color ",
    "fx --mode ",
    "fx --mode=f",
    "fx --secret ",
    "fx --dir ",
    "fx --file ",
    "fx --size ",
    "fx --pick ",
    "fx --label ",
    "fx --label a --label ",
    "fx --more x ",
    "fx --kv a=1 ",
    "fx --safe ",
    "fx --safe s1 ",
    "fx --feature --",
    "fx --no-f",
    "fx --json --",
    "fx --json --y",
    "fx r",
    "fx run ",
    "fx r a",
    "fx run alpha ",
    "fx run alpha one ",
    "fx run alpha one --",
    "fx run alpha -- ",
    "fx run --jobs ",
    "fx run -j 3 ",
    "fx run 'al",
    'fx run "b',
    "fx nohelp ",
    "fx db ",
    "fx db m",
    "fx db migrate ",
    "fx db migrate head ",
    "fx db migrate --kind ",
    "fx db migrate --kind k1 ",
    "fx db sh",
    "fx --level 9 run ",
    "fx --color red run ",
    "fx unknown ",
]

TARGETS = [
    "fixture_cli:main",
    "fixture_cli:main_long_exclude",
    "fixture_cli:main_positional_args",
    "fixture_cli:main_exclusive",
    "fixture_cli:main_fromfile",
]


@pytest.fixture(params=["bash", "zsh", "fish"])
def shell(request, monkeypatch):
    monkeypatch.setenv("_ARGCOMPLETE_SHELL", request.param)
    if request.param == "fish":
        monkeypatch.setenv("_ARGCOMPLETE_DFS", "\t")  # descriptions for fish
    return request.param


@pytest.mark.parametrize("target", TARGETS)
def test_parity(target, shell):
    results = compare(target, LINES)
    mismatches = [(line, real, fast) for line, real, fast in results if fast is not None and real != fast]
    assert not mismatches
    # Only the dynamic --user completer may need the real CLI.
    assert {line for line, _, fast in results if fast is None} <= {"fx --user "}


def test_dynamic_completer_falls_back():
    [(line, real, fast)] = compare("fixture_cli:main", ["fx --user "])
    assert fast is None
    assert real == ["alice", "bob"]


def test_environ_completer_reads_runtime_environment(monkeypatch):
    monkeypatch.setenv("BOOSTER_TEST_VARIABLE", "1")
    [(_, real, fast)] = compare("fixture_cli:main", ["fx --env BOOSTER_TEST_V"])
    assert real == fast == ["BOOSTER_TEST_VARIABLE "]


def test_directory_and_file_completers(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    for line, real, fast in compare("fixture_cli:main", ["fx --dir ", "fx --file "]):
        assert real == fast, line
    assert "a.py" in compare("fixture_cli:main", ["fx --file "])[0][1]


def test_custom_validator_disables_fast_path():
    from argcomplete_booster._spec import make_spec
    from argcomplete_booster.testing import capture

    finder, parser, args, kwargs, original = capture("fixture_cli:main_custom_validator")
    spec = make_spec("fixture_cli:main_custom_validator", finder, parser, original, args, kwargs)
    assert spec["fast"] is False
    assert spec["unsupported"] == ["custom validator"]


def test_fromfile_disables_pruning():
    from argcomplete_booster._spec import make_spec
    from argcomplete_booster.testing import capture

    for target, prune in [("fixture_cli:main", True), ("fixture_cli:main_fromfile", False)]:
        spec = make_spec(target, *capture(target)[:2], capture(target)[4], (), {})
        assert spec["prune"] is prune


def test_pruning_only_builds_named_subcommands():
    from argcomplete_booster._rebuild import _Stub, build_parser
    from argcomplete_booster._spec import serialize_parser
    from fixture_cli import build_parser as real_build

    spec, _ = serialize_parser(real_build())
    parser = build_parser(spec, {"fx", "db"})
    choices = parser._subparsers._group_actions[0].choices
    assert isinstance(choices["run"], _Stub)
    assert choices["run"] is choices["r"]
    assert not isinstance(choices["db"], _Stub)
    assert isinstance(choices["db"]._subparsers._group_actions[0].choices["migrate"], _Stub)


def test_known_divergence_invalid_value_for_custom_type():
    """Custom ``type=`` callables are not run on the fast path. When the user has
    typed a value the type would reject, real argparse stops parsing and offers
    top-level completions; the fast path keeps going. Valid input is identical."""
    [(_, real, fast)] = compare("fixture_cli:main", ["fx --color RED run "])
    assert "--verbose" in real and "alpha" in fast
