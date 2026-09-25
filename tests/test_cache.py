import json

from argcomplete_booster import _cache
from argcomplete_booster._rebuild import _Stub, build_parser
from argcomplete_booster._spec import serialize_parser
from argcomplete_booster.testing import complete
from fixture_cli import build_parser as real_build_parser


def _spec():
    parser_spec, prune = serialize_parser(real_build_parser())
    return {"header": {}, "fingerprint": [], "prune": prune, "parser": parser_spec}


def test_sharded_file_reads_only_needed_shards(tmp_path, monkeypatch):
    monkeypatch.setattr(_cache, "SHARD_MIN_BYTES", 1)
    path = str(tmp_path / "spec.json")
    _cache.store(path, _spec())

    with open(path, "rb") as f:
        head = json.loads(f.readline())
    assert "$shard" in json.dumps(head)  # subcommand parsers moved out of the head

    spec = _cache.read(path)
    loader = _cache.shard_loader(spec)
    loaded = []
    parser = build_parser(spec["parser"], {"fx", "db", "migrate"}, lambda ref: loaded.append(ref) or loader(ref))
    assert len(loaded) == 2  # "db" and "db migrate"; "run", "nohelp", ... untouched
    sub = parser._subparsers._group_actions[0].choices
    assert isinstance(sub["run"], _Stub)
    assert complete(parser, "fx db migrate h") == ["head "]


def test_load_all_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(_cache, "SHARD_MIN_BYTES", 1)
    path = str(tmp_path / "spec.json")
    original = _spec()
    _cache.store(path, original)
    assert _cache.load_all(path) == json.loads(json.dumps(original))


def test_truncated_shard_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(_cache, "SHARD_MIN_BYTES", 1)
    path = tmp_path / "spec.json"
    _cache.store(str(path), _spec())
    path.write_bytes(path.read_bytes()[:-10])
    spec = _cache.read(str(path))
    try:
        build_parser(spec["parser"], None, _cache.shard_loader(spec))
    except ValueError:
        pass
    else:
        raise AssertionError("truncated shard not detected")
