"""Location, validation and atomic storage of completion specs.

Used on the fast path: keep imports to ``os``, ``sys``, ``json`` and ``zlib``.
"""

import json
import os
import sys
import zlib

from . import __version__
from ._boost import ENV_CACHE_DIR, FORMAT


def cache_dir():
    explicit = os.environ.get(ENV_CACHE_DIR)
    if explicit:
        return explicit
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "argcomplete-booster")


def environment_key(target):
    """Identity of "this CLI in this interpreter environment"."""
    return {
        "format": FORMAT,
        "booster": __version__,
        "target": target,
        "python": "%d.%d" % sys.version_info[:2],
        "prefix": sys.prefix,
    }


def cache_path(target):
    key = environment_key(target)
    digest = "%08x" % zlib.crc32("\0".join(str(key[k]) for k in sorted(key)).encode())
    prog = "".join(c if c.isalnum() or c in "-_." else "_" for c in os.path.basename(sys.argv[0] or "cli"))
    return os.path.join(cache_dir(), "%s-%s.json" % (prog, digest))


def fingerprint_is_fresh(fingerprint):
    for path, mtime_ns, size in fingerprint:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_mtime_ns != mtime_ns or st.st_size != size:
            return False
    return True


def read(path):
    """Read a spec file; returns ``None`` if missing or corrupt.

    File layout: one line of JSON (the spec, with large subcommand parsers
    replaced by ``{"$shard": [offset, length]}``) followed by the shards. Only
    the shards for subcommands named on the command line are ever parsed.
    A plain (pretty-printed) JSON document is accepted too.
    """
    try:
        with open(path, "rb") as f:
            first = f.readline()
            base = f.tell()
            try:
                spec = json.loads(first)
            except ValueError:
                f.seek(0)
                spec = json.loads(f.read())
                base = None
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict):
        return None
    if base is not None:
        spec["$file"] = [path, base]
    return spec


def shard_loader(spec):
    """Return a function resolving ``{"$shard": ...}`` references of ``spec``."""
    path, base = spec.get("$file") or (None, None)

    def load_shard(ref):
        offset, length = ref["$shard"]
        with open(path, "rb") as f:
            f.seek(base + offset)
            data = f.read(length)
        if len(data) != length:
            raise ValueError("truncated spec shard")
        return json.loads(data)

    return load_shard


def load(path, target, check_fingerprint=True):
    """Return the spec at ``path`` if it is valid for ``target``, else ``None``."""
    spec = read(path)
    if spec is None:
        return None
    header = spec.get("header", {})
    expected = environment_key(target)
    keys = ("format", "target") if not check_fingerprint else tuple(expected)
    if any(header.get(k) != expected[k] for k in keys):
        return None
    if check_fingerprint and not fingerprint_is_fresh(spec.get("fingerprint", [])):
        return None
    return spec


def load_all(path):
    """Read a spec and inline every shard (maintainer tooling)."""
    spec = read(path)
    if spec is None:
        raise ValueError("unreadable spec: %s" % path)
    load_shard = shard_loader(spec)

    def inline(parser):
        for action in parser["actions"]:
            if action["kind"] != "subparsers":
                continue
            for choice in action["choices"]:
                if "$shard" in choice["parser"]:
                    choice["parser"] = load_shard(choice["parser"])
                inline(choice["parser"])
        return parser

    inline(spec["parser"])
    spec.pop("$file", None)
    return spec


SHARD_MIN_BYTES = 2048


def encode(spec):
    """Serialize ``spec`` into the sharded single-file layout (bytes)."""
    blobs = bytearray()

    def shard(parser):
        for action in parser["actions"]:
            if action["kind"] != "subparsers":
                continue
            for choice in action["choices"]:
                child = shard(choice["parser"])
                data = json.dumps(child, separators=(",", ":")).encode()
                if len(data) >= SHARD_MIN_BYTES:
                    choice["parser"] = {"$shard": [len(blobs), len(data)]}
                    blobs.extend(data)
                else:
                    choice["parser"] = child
        return parser

    spec = dict(spec, parser=shard(json.loads(json.dumps(spec["parser"]))))
    return json.dumps(spec, separators=(",", ":")).encode() + b"\n" + bytes(blobs)


def store(path, spec):
    """Atomically write ``spec``; concurrent completions never see partial files."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(encode(spec))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
