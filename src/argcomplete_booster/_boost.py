"""Entry-point wrapper: decides between the fast path and the original CLI.

Nothing in this module may import the wrapped CLI (or anything heavy) at module
level. The fast path only needs ``os``, ``sys``, ``json`` and argcomplete.
"""

import os
import sys

FORMAT = 1

# Environment switches (documented in README/DESIGN).
ENV_DISABLE = "ARGCOMPLETE_BOOSTER"  # "0" disables the fast path
ENV_CACHE_DIR = "ARGCOMPLETE_BOOSTER_CACHE_DIR"
ENV_DEBUG = "ARGCOMPLETE_BOOSTER_DEBUG"
ENV_SYNC = "ARGCOMPLETE_BOOSTER_SYNC"  # write the cache before exiting (tests, debugging)

# Objects that must outlive a failed fast-path attempt (e.g. argcomplete's
# debug stream wrapping fd 9, which would close the fd when garbage collected).
_keepalive = []


class NeedSlowPath(BaseException):
    """Raised inside the fast path to hand over to the real CLI.

    Derives from BaseException so argcomplete's ``except Exception`` handlers
    do not swallow it.
    """


def _debug(*args):
    if os.environ.get(ENV_DEBUG):
        print("argcomplete-booster:", *args, file=sys.stderr)


def resolve(target):
    """Resolve ``"package.module:attr.path"`` to an object."""
    import importlib

    module_name, _, attr_path = target.partition(":")
    obj = importlib.import_module(module_name)
    for part in filter(None, attr_path.split(".")):
        obj = getattr(obj, part)
    return obj


def static_completer(func):
    """Mark a completer whose output does not depend on the prefix or on the
    other arguments, so it can be evaluated once when the spec is generated.

    The completer is called with ``prefix=""`` and its result is stored in the
    spec; the fast path then never has to import the CLI for it.
    """
    func.__argcomplete_booster_static__ = True
    return func


def boost(target, *, spec_file=None, cache=True):
    """Return a console-script ``main`` that completes from a cached spec.

    :param target: ``"module:function"`` of the CLI's real entry point. It is
        imported only when the CLI actually runs (or when completion cannot be
        served from the spec).
    :param spec_file: optional path of a spec generated at build time with
        ``argcomplete-booster generate`` and shipped inside the package. When it
        exists it is used as-is and the user cache is never touched.
    :param cache: set to ``False`` to disable the per-user runtime cache.
    """

    def main():
        if "_ARGCOMPLETE" in os.environ and os.environ.get(ENV_DISABLE, "1") != "0":
            _complete(target, spec_file, cache)
        return resolve(target)()

    main.__argcomplete_booster_target__ = target
    return main


def _complete(target, spec_file, cache):
    """Try to answer the completion request without importing ``target``.

    Returns only when the slow path is needed; in that case a capture hook has
    been installed so the real CLI refreshes the cache as it completes.
    """
    from . import _cache

    spec = None
    cache_path = None
    if spec_file is not None and os.path.exists(spec_file):
        spec = _cache.load(spec_file, target, check_fingerprint=False)
        _debug("shipped spec", spec_file, "usable" if spec else "unusable")
    elif cache:
        cache_path = _cache.cache_path(target)
        spec = _cache.load(cache_path, target)
        _debug("cache", cache_path, "hit" if spec else "miss/stale")

    if spec is not None and spec.get("fast", True):
        try:
            _fast_path(spec)  # exits the process on success
        except NeedSlowPath as e:
            _debug("falling back to the real CLI:", e)
            cache_path = None  # the spec is fresh, no need to rewrite it
        except Exception as e:  # never let a booster bug break completion
            _debug("fast path failed:", repr(e))

    _install_capture(target, cache_path)


def _fast_path(spec):
    import argcomplete
    import argcomplete.io

    from ._rebuild import build_autocomplete_kwargs, build_parser

    comp_line = os.environ["COMP_LINE"]
    comp_point = int(os.environ["COMP_POINT"])
    words = set(argcomplete.split_line(comp_line, comp_point)[3]) if spec.get("prune", True) else None
    from ._cache import shard_loader

    parser = build_parser(spec["parser"], words, shard_loader(spec))
    kwargs = build_autocomplete_kwargs(spec.get("autocomplete", {}))
    finder_cls = argcomplete.ExclusiveCompletionFinder if spec.get("finder") == "exclusive" else argcomplete.CompletionFinder

    output_stream = None
    if not os.environ.get("_ARGCOMPLETE_STDOUT_FILENAME"):
        try:
            # closefd=False: if we fall back, the real CLI must still be able to use fd 8.
            output_stream = os.fdopen(8, "w", closefd=False)
        except OSError:
            output_stream = None
    try:
        finder_cls()(parser, output_stream=output_stream, exit_method=os._exit, **kwargs)
    finally:
        _keepalive.append(argcomplete.io.debug_stream)


def _install_capture(target, cache_path):
    """Hook argcomplete so the real CLI's parser gets cached.

    The spec is written *after* the completions have been printed, from a
    detached child process, so a cache miss costs the user no extra latency.
    """
    try:
        from argcomplete.finders import CompletionFinder
    except ImportError:
        return
    original = CompletionFinder.__call__
    if getattr(original, "__argcomplete_booster_hook__", False):
        return

    def __call__(self, argument_parser, *args, **kwargs):
        if cache_path is None or "_ARGCOMPLETE" not in os.environ:
            return original(self, argument_parser, *args, **kwargs)
        import inspect

        bound = inspect.signature(original).bind(self, argument_parser, *args, **kwargs)
        bound.apply_defaults()
        user_exit = bound.arguments["exit_method"]

        def write_spec():
            from . import _cache
            from ._spec import make_spec

            _cache.store(cache_path, make_spec(target, self, argument_parser, original, args, kwargs))
            _debug("wrote", cache_path)

        def exit_method(code=0):
            if code == 0:
                _run_detached(write_spec)
            user_exit(code)

        bound.arguments["exit_method"] = exit_method
        return original(*bound.args, **bound.kwargs)

    __call__.__argcomplete_booster_hook__ = True
    __call__.__wrapped_original__ = original
    CompletionFinder.__call__ = __call__


def _run_detached(func):
    """Run ``func`` without delaying the shell, which waits for fd 8 to close."""

    def guarded():
        try:
            func()
        except Exception as e:
            _debug("could not write spec:", repr(e))

    if os.environ.get(ENV_SYNC) or not hasattr(os, "fork"):
        return guarded()
    try:
        pid = os.fork()
    except OSError:
        return guarded()
    if pid:
        return
    try:  # child: let go of the terminal and of every pipe the shell reads from
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.closerange(3, 1024)
        guarded()
    finally:
        os._exit(0)
