"""Serialize an argparse parser (plus argcomplete settings) into a JSON spec.

Runs only on the slow path (cache miss) or in the maintainer tooling, so it may
import anything.
"""

import argparse
import inspect
import os
import sys

import argcomplete
from argcomplete.completers import (
    ChoicesCompleter,
    DirectoriesCompleter,
    FilesCompleter,
    SuppressCompleter,
)
from argcomplete.finders import default_validator

from ._cache import environment_key

KIND_BY_CLASS = {
    argparse._StoreAction: "store",
    argparse._StoreConstAction: "store_const",
    argparse._StoreTrueAction: "store_true",
    argparse._StoreFalseAction: "store_false",
    argparse._AppendAction: "append",
    argparse._AppendConstAction: "append_const",
    argparse._CountAction: "count",
    argparse._HelpAction: "help",
    argparse._VersionAction: "version",
}
if hasattr(argparse, "_ExtendAction"):
    KIND_BY_CLASS[argparse._ExtendAction] = "extend"
if hasattr(argparse, "BooleanOptionalAction"):
    KIND_BY_CLASS[argparse.BooleanOptionalAction] = "boolean_optional"

TYPE_NAMES = {int: "int", float: "float"}
# argcomplete >= 3.7 expands "%(default)s" & co. in help shown by zsh/fish; older versions print it raw.
EXPANDS_HELP = hasattr(argcomplete.CompletionFinder, "_get_action_help")
_JSON_SCALARS = (str, int, float, bool)


class Unsupported(Exception):
    """The parser uses something the fast path cannot reproduce faithfully."""


def _suppress(value):
    return argparse.SUPPRESS if value is argparse.SUPPRESS else value


class _Serializer:
    def __init__(self, top_parser):
        formatter_class = getattr(top_parser, "formatter_class", argparse.HelpFormatter)
        # argcomplete expands "%(default)s"-style help with the *top* parser's formatter.
        self.formatter = formatter_class(prog=top_parser.prog)
        self.prune = True
        self._stack = []

    def help_text(self, action):
        help = action.help
        if help is None or help is argparse.SUPPRESS or "%" not in help or not EXPANDS_HELP:
            return help
        try:
            expanded = self.formatter._expand_help(action)
        except Exception:
            expanded = help
        # Escaped so argcomplete's own expansion in the fast path is a no-op.
        return expanded.replace("%", "%%")

    def completer(self, completer, action=None, parser=None):
        if completer is None:
            return None
        cls = type(completer)
        if getattr(completer, "__argcomplete_booster_static__", False):
            result = completer(prefix="", action=action, parser=parser, parsed_args=argparse.Namespace())
            if isinstance(result, dict):
                return {"kind": "static", "items": [[str(k), str(v or "")] for k, v in result.items()]}
            return {"kind": "static", "items": [str(c) for c in result]}
        if cls is ChoicesCompleter:
            if completer.choices is os.environ:
                return {"kind": "environ"}
            return {"kind": "choices", "choices": [completer._convert(c) for c in completer.choices]}
        if cls is FilesCompleter:
            return {"kind": "files", "allowednames": list(completer.allowednames), "directories": completer.directories}
        if cls is DirectoriesCompleter:
            return {"kind": "dirs"}
        if cls is SuppressCompleter:
            return {"kind": "suppress"}
        return {"kind": "dynamic", "name": getattr(completer, "__qualname__", cls.__qualname__)}

    def action(self, action, parser):
        if isinstance(action, argparse._SubParsersAction):
            return self.subparsers(action)
        # argcomplete swaps classes when it patches a parser; look through that.
        cls = getattr(action, "_orig_class", type(action))
        spec = {"kind": KIND_BY_CLASS.get(cls, "opaque"), "dest": action.dest}
        if spec["kind"] == "opaque":
            spec["class"] = cls.__qualname__
            if cls in argcomplete.safe_actions:
                spec["safe"] = True
        if action.option_strings:
            spec["opts"] = list(action.option_strings)
        if action.nargs is not None:
            spec["nargs"] = action.nargs
        if action.required:
            spec["required"] = True
        help = self.help_text(action)
        if help is not None:
            spec["help"] = help

        choices = action.choices
        completer = getattr(action, "completer", None)
        if choices is not None:
            known_type = action.type is None or action.type is str or action.type in TYPE_NAMES
            try:
                values = list(choices)
            except TypeError:
                values = None
            if values is not None and known_type and all(type(c) in _JSON_SCALARS for c in values):
                spec["choices"] = values
                if action.type in TYPE_NAMES:
                    spec["type"] = TYPE_NAMES[action.type]
            elif completer is None and values is not None:
                # Parsing would depend on an unknown type/container: keep only
                # what completion shows. argcomplete renders choices the same way.
                completer = ChoicesCompleter(values)
        elif action.type in TYPE_NAMES:
            spec["type"] = TYPE_NAMES[action.type]

        if completer is not None:
            spec["completer"] = self.completer(completer, action, parser)
        return spec

    def subparsers(self, action):
        pseudo_by_name = {a.dest: a for a in action._choices_actions}
        groups = []
        by_parser = {}
        for name, sub in action._name_parser_map.items():
            if id(sub) in by_parser:
                by_parser[id(sub)]["names"].append(name)
                continue
            entry = {"names": [name], "parser": sub}
            by_parser[id(sub)] = entry
            groups.append(entry)
        choices = []
        for entry in groups:
            choice = {"names": entry["names"], "parser": self.parser(entry["parser"])}
            pseudo = pseudo_by_name.get(entry["names"][0])
            if pseudo is not None:
                choice["help"] = self.help_text(pseudo)
            choices.append(choice)
        spec = {"kind": "subparsers", "dest": action.dest, "prog": action._prog_prefix, "choices": choices}
        if action.required:
            spec["required"] = True
        help = self.help_text(action)
        if help is not None:
            spec["help"] = help
        return spec

    def parser(self, parser):
        if any(p is parser for p in self._stack):
            raise Unsupported("recursive parser structure")
        self._stack.append(parser)
        try:
            spec = {"prog": parser.prog, "prefix_chars": parser.prefix_chars}
            if parser.fromfile_prefix_chars:
                spec["fromfile_prefix_chars"] = parser.fromfile_prefix_chars
                self.prune = False  # arguments may come from files we cannot see
            if not parser.allow_abbrev:
                spec["allow_abbrev"] = False
            index = {id(a): i for i, a in enumerate(parser._actions)}
            spec["actions"] = [self.action(a, parser) for a in parser._actions]
            mutex = [
                {"required": bool(g.required), "actions": [index[id(a)] for a in g._group_actions if id(a) in index]}
                for g in parser._mutually_exclusive_groups
            ]
            if mutex:
                spec["mutex"] = mutex
            return spec
        finally:
            self._stack.pop()


def serialize_parser(parser):
    """Return ``(parser_spec, prune_allowed)``."""
    s = _Serializer(parser)
    return s.parser(parser), s.prune


def autocomplete_settings(finder, original_call, args, kwargs):
    """Normalize the arguments of ``argcomplete.autocomplete(parser, ...)``.

    Returns ``(settings, finder_kind, fast_supported_reason)``.
    """
    bound = inspect.signature(original_call).bind(finder, None, *args, **kwargs)
    bound.apply_defaults()
    a = bound.arguments
    settings = {
        "always_complete_options": a["always_complete_options"],
        "print_suppressed": bool(a["print_suppressed"]),
    }
    if a.get("exclude") is not None:
        settings["exclude"] = list(a["exclude"])
    if a.get("append_space") is not None:
        settings["append_space"] = bool(a["append_space"])
    s = _Serializer(argparse.ArgumentParser(add_help=False))
    settings["default_completer"] = s.completer(a["default_completer"])

    problems = []
    if a.get("validator") not in (None, default_validator):
        problems.append("custom validator")
    if type(finder) is argcomplete.CompletionFinder:
        kind = "default"
    elif type(finder) is argcomplete.ExclusiveCompletionFinder:
        kind = "exclusive"
    else:
        kind = "custom"
        problems.append("custom CompletionFinder subclass %s" % type(finder).__qualname__)
    return settings, kind, problems


def fingerprint(target):
    """Files whose change must invalidate the cache.

    * every source file and package directory of the CLI's top-level package
      that was imported while building the parser (editable installs, plugins
      dropped into a package directory);
    * every site-packages directory on ``sys.path``: pip rewrites a
      ``*.dist-info`` directory there on every install/upgrade/uninstall, which
      changes the directory's mtime.
    """
    top = target.partition(":")[0].split(".")[0]
    paths = set()
    for name, module in list(sys.modules.items()):
        if name != top and not name.startswith(top + "."):
            continue
        f = getattr(module, "__file__", None)
        if f:
            paths.add(os.path.abspath(f))
        for d in getattr(module, "__path__", None) or ():
            paths.add(os.path.abspath(d))
    for entry in sys.path:
        if os.path.basename(entry) in ("site-packages", "dist-packages") and os.path.isdir(entry):
            paths.add(os.path.abspath(entry))
    result = []
    for p in sorted(paths):
        try:
            st = os.stat(p)
        except OSError:
            continue
        result.append([p, st.st_mtime_ns, st.st_size])
    return result


def make_spec(target, finder, parser, original_call, args, kwargs, with_fingerprint=True):
    parser_spec, prune = serialize_parser(parser)
    settings, finder_kind, problems = autocomplete_settings(finder, original_call, args, kwargs)
    spec = {
        "header": environment_key(target),
        "fingerprint": fingerprint(target) if with_fingerprint else [],
        "fast": not problems,
        "prune": prune,
        "finder": finder_kind,
        "autocomplete": settings,
        "parser": parser_spec,
    }
    if problems:
        spec["unsupported"] = problems
    return spec
