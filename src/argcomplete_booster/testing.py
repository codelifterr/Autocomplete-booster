"""Helpers for CLI maintainers' test suites.

Typical use (one test is enough to guard a whole CLI)::

    from argcomplete_booster.testing import assert_parity

    def test_completion_parity():
        assert_parity("mycli.cli:main", ["mycli ", "mycli dep", "mycli deploy --"])
"""

import contextlib
import io
import os

import argcomplete

from ._boost import NeedSlowPath, resolve
from ._rebuild import build_autocomplete_kwargs, build_parser


class _Done(BaseException):
    pass


class _Captured(BaseException):
    def __init__(self, finder, parser, args, kwargs):
        self.finder, self.parser, self.args, self.kwargs = finder, parser, args, kwargs


@contextlib.contextmanager
def completion_env(line, point=None):
    env = {
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\013",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line) if point is None else point),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def capture(target):
    """Run ``target`` as if completing ``"<prog> "`` and return what it passed
    to argcomplete: ``(finder, parser, args, kwargs, original_call)``.

    Nothing is printed and the process does not exit.
    """
    from argcomplete.finders import CompletionFinder

    original = CompletionFinder.__call__
    while getattr(original, "__wrapped_original__", None):
        original = original.__wrapped_original__

    def hook(self, parser, *args, **kwargs):
        raise _Captured(self, parser, args, kwargs)

    current = CompletionFinder.__call__
    CompletionFinder.__call__ = hook
    try:
        with completion_env("prog "):
            try:
                resolve(target)()
            except _Captured as c:
                return c.finder, c.parser, c.args, c.kwargs, original
            except SystemExit:
                pass
    finally:
        CompletionFinder.__call__ = current
    raise RuntimeError("%s never called argcomplete.autocomplete()" % target)


def complete(parser, line, point=None, finder_cls=argcomplete.CompletionFinder, **autocomplete_kwargs):
    """Return the completions argcomplete would print for ``line``."""

    class Finder(finder_cls):
        def _init_debug_stream(self):
            argcomplete.io.debug_stream = io.StringIO()

    out = io.StringIO()

    def stop(code=0):
        raise _Done()

    with completion_env(line, point):
        try:
            Finder()(parser, output_stream=out, exit_method=stop, **autocomplete_kwargs)
        except _Done:
            pass
    value = out.getvalue()
    return value.split("\013") if value else []


def compare(target, lines):
    """Compare completions of the real parser with the fast path for each line.

    Returns a list of ``(line, real, fast)`` where ``fast`` is ``None`` when the
    fast path would hand over to the real CLI (dynamic completer).
    """
    from ._spec import make_spec

    finder, parser, args, kwargs, original = capture(target)
    spec = make_spec(target, finder, parser, original, args, kwargs, with_fingerprint=False)
    fast_kwargs = build_autocomplete_kwargs(spec["autocomplete"])
    fast_finder = argcomplete.ExclusiveCompletionFinder if spec["finder"] == "exclusive" else argcomplete.CompletionFinder
    results = []
    for line in lines:
        # argcomplete patches a parser for good (bound to one finder), so the
        # real side needs a freshly built parser for every line.
        finder, parser, args, kwargs, _ = capture(target)
        real = _complete_like(finder, parser, line, args, kwargs)
        words = set(argcomplete.split_line(line, len(line))[3]) if spec["prune"] else None
        try:
            fast = complete(build_parser(spec["parser"], words), line, None, fast_finder, **fast_kwargs)
        except NeedSlowPath:
            fast = None
        results.append((line, real, fast))
    return results


def _complete_like(finder, parser, line, args, kwargs):
    """Complete with the same finder class and autocomplete() arguments the CLI used."""
    import inspect

    names = list(inspect.signature(argcomplete.CompletionFinder.__call__).parameters)[2:]
    merged = dict(zip(names, args))
    merged.update(kwargs)
    merged.pop("exit_method", None)
    merged.pop("output_stream", None)
    return complete(parser, line, None, type(finder), **merged)


def assert_parity(target, lines):
    """Assert the fast path completes every line exactly like the real CLI."""
    mismatches = [(l, r, f) for l, r, f in compare(target, lines) if f is not None and r != f]
    if mismatches:
        msg = "\n".join("%r:\n  real: %r\n  fast: %r" % m for m in mismatches)
        raise AssertionError("completion mismatch:\n" + msg)
