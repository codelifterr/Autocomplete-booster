"""Rebuild a lightweight argparse parser from a spec (fast path).

The rebuilt parser is handed to the *real* argcomplete, so completion semantics
(lexing, quoting, option/positional handling, shells) stay identical. Only
subcommand parsers named on the command line are materialized.
"""

import argparse

import argcomplete
from argcomplete.completers import (
    BaseCompleter,
    ChoicesCompleter,
    DirectoriesCompleter,
    EnvironCompleter,
    FilesCompleter,
    SuppressCompleter,
)

from ._boost import NeedSlowPath

CLASS_BY_KIND = {
    "store": argparse._StoreAction,
    "store_const": argparse._StoreConstAction,
    "store_true": argparse._StoreTrueAction,
    "store_false": argparse._StoreFalseAction,
    "append": argparse._AppendAction,
    "append_const": argparse._AppendConstAction,
    "count": argparse._CountAction,
    "help": argparse._HelpAction,
    "version": argparse._VersionAction,
    "extend": getattr(argparse, "_ExtendAction", argparse._AppendAction),
    "boolean_optional": getattr(argparse, "BooleanOptionalAction", argparse._StoreTrueAction),
}
TYPES = {"int": int, "float": float}


class OpaqueAction(argparse.Action):
    """Stand-in for a custom action class; argcomplete never executes it."""

    def __call__(self, parser, namespace, values, option_string=None):
        pass


class SafeOpaqueAction(argparse.Action):
    """Stand-in for a custom action the CLI registered in argcomplete.safe_actions."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)


class DynamicCompleter(BaseCompleter):
    """Placeholder for a completer that needs the real CLI code."""

    def __init__(self, name):
        self.name = name

    def __call__(self, **kwargs):
        raise NeedSlowPath("dynamic completer %s" % self.name)


class StaticCompleter(BaseCompleter):
    """Replays the output recorded from a ``@static_completer``."""

    def __init__(self, items):
        self.items = items

    def __call__(self, **kwargs):
        if self.items and isinstance(self.items[0], list):
            return dict(self.items)
        return list(self.items)


class _Stub:
    """Unvisited subcommand: only its identity is used (to group aliases)."""

    __slots__ = ()


def _sup(value):
    return argparse.SUPPRESS if value == argparse.SUPPRESS else value


def build_completer(spec):
    kind = spec["kind"]
    if kind == "choices":
        return ChoicesCompleter(spec["choices"])
    if kind == "environ":
        return EnvironCompleter
    if kind == "files":
        return FilesCompleter(spec["allowednames"], directories=spec["directories"])
    if kind == "dirs":
        return DirectoriesCompleter()
    if kind == "suppress":
        return SuppressCompleter()
    if kind == "static":
        return StaticCompleter(spec["items"])
    return DynamicCompleter(spec.get("name", "?"))


def build_action(spec):
    kind = spec["kind"]
    cls = CLASS_BY_KIND.get(kind)
    if cls is None:
        cls = SafeOpaqueAction if spec.get("safe") else OpaqueAction
        if cls is SafeOpaqueAction:
            argcomplete.safe_actions.add(SafeOpaqueAction)
    # Bypass __init__: constructors differ between classes and Python versions,
    # but every argparse action is a plain bag of these attributes.
    action = cls.__new__(cls)
    action.option_strings = spec.get("opts", [])
    action.dest = _sup(spec["dest"])
    action.nargs = spec.get("nargs")
    action.const = None
    action.default = None
    action.type = TYPES.get(spec.get("type"))
    action.choices = spec.get("choices")
    action.required = spec.get("required", False)
    action.help = _sup(spec.get("help"))
    action.metavar = None
    action.deprecated = False
    if kind == "version":
        action.version = None
    if "completer" in spec:
        action.completer = build_completer(spec["completer"])
    return action


def _build_subparsers(parser, spec, words, load_shard):
    action = parser.add_subparsers(
        dest=_sup(spec["dest"]),
        required=spec.get("required", False),
        help=_sup(spec.get("help")),
        prog=spec["prog"],
    )
    for choice in spec["choices"]:
        names = choice["names"]
        if words is None or any(n in words for n in names):
            sub_spec = choice["parser"]
            if "$shard" in sub_spec:
                sub_spec = load_shard(sub_spec)
            sub = build_parser(sub_spec, words, load_shard)
        else:
            sub = _Stub()
        for name in names:
            action._name_parser_map[name] = sub
        if "help" in choice:
            action._choices_actions.append(action._ChoicesPseudoAction(names[0], names[1:], _sup(choice["help"])))
    return action


def build_parser(spec, words=None, load_shard=None):
    """Build a parser; subcommands not in ``words`` become stubs (``None`` = build all).

    ``load_shard`` resolves ``{"$shard": ...}`` references (see ``_cache.encode``).
    """
    kwargs = {}
    if spec.get("fromfile_prefix_chars"):
        kwargs["fromfile_prefix_chars"] = spec["fromfile_prefix_chars"]
    parser = argparse.ArgumentParser(
        prog=spec["prog"],
        prefix_chars=spec.get("prefix_chars", "-"),
        allow_abbrev=spec.get("allow_abbrev", True),
        add_help=False,
        **kwargs,
    )
    actions = []
    for a in spec["actions"]:
        if a["kind"] == "subparsers":
            actions.append(_build_subparsers(parser, a, words, load_shard))
        else:
            action = build_action(a)
            parser._add_action(action)
            actions.append(action)
    for group in spec.get("mutex", ()):
        g = parser.add_mutually_exclusive_group(required=group["required"])
        g._group_actions.extend(actions[i] for i in group["actions"])
    return parser


def build_autocomplete_kwargs(settings):
    kwargs = {
        "always_complete_options": settings.get("always_complete_options", True),
        "print_suppressed": settings.get("print_suppressed", False),
    }
    if "exclude" in settings:
        kwargs["exclude"] = settings["exclude"]
    if "append_space" in settings:
        kwargs["append_space"] = settings["append_space"]
    if "default_completer" in settings:
        default = settings["default_completer"]
        kwargs["default_completer"] = build_completer(default) if default else None
    return kwargs
