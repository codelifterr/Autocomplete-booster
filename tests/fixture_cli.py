"""A CLI exercising as many argparse/argcomplete features as possible."""

import argparse
import enum
import os

import argcomplete
from argcomplete.completers import (
    ChoicesCompleter,
    DirectoriesCompleter,
    EnvironCompleter,
    FilesCompleter,
    SuppressCompleter,
)

from argcomplete_booster import static_completer


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


class KeyValueAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, dict(v.split("=", 1) for v in values))


class SafeCustomAction(argparse._StoreAction):
    pass


argcomplete.safe_actions.add(SafeCustomAction)


def dynamic_names(prefix, parsed_args, **kwargs):
    return ["alice", "bob"]


@static_completer
def static_sizes(**kwargs):
    return {"small": "1 CPU", "large": "8 CPUs"}


def build_parser(**parser_kwargs):
    parser = argparse.ArgumentParser(prog="fx", **parser_kwargs)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--level", type=int, choices=range(1, 4), help="level %(choices)s")
    parser.add_argument("--color", type=Color, choices=list(Color), help="color (default: %(default)s)")
    parser.add_argument("--mode", choices=["fast", "slow"], help="100%% mode")
    parser.add_argument("--hidden", help=argparse.SUPPRESS)
    parser.add_argument("--secret").completer = SuppressCompleter()
    parser.add_argument("--env").completer = EnvironCompleter
    parser.add_argument("--dir").completer = DirectoriesCompleter()
    parser.add_argument("--file").completer = FilesCompleter(["py"])
    parser.add_argument("--user").completer = dynamic_names
    parser.add_argument("--size").completer = static_sizes
    parser.add_argument("--pick").completer = ChoicesCompleter(["x1", "x2"])
    parser.add_argument("--label", action="append", choices=["a", "b"])
    parser.add_argument("--more", action="extend", nargs="+")
    parser.add_argument("--kv", action=KeyValueAction, nargs="+")
    parser.add_argument("--safe", action=SafeCustomAction, choices=["s1", "s2"])
    parser.add_argument("--feature", action=argparse.BooleanOptionalAction)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_const", const="json", dest="fmt")
    group.add_argument("--yaml", action="store_const", const="yaml", dest="fmt")

    sub = parser.add_subparsers(dest="cmd", title="commands")
    run = sub.add_parser("run", aliases=["r"], help="run it")
    run.add_argument("target", choices=["alpha", "beta", "gamma"])
    run.add_argument("extra", nargs="?", choices=["one", "two"])
    run.add_argument("--jobs", "-j", type=int)
    run.add_argument("rest", nargs=argparse.REMAINDER)

    sub.add_parser("nohelp")  # no help= -> no description

    db = sub.add_parser("db", help="database")
    dbsub = db.add_subparsers(dest="dbcmd", required=True)
    mig = dbsub.add_parser("migrate", help="apply migrations")
    mig.add_argument("revision", nargs="*", choices=["head", "base"])
    mig.add_argument("--sql", action="store_true")
    mig.add_argument("--kind", action=KeyValueAction, choices=["k1", "k2"])
    dbsub.add_parser("reset", help="drop everything")
    dbsub.add_parser("shell", help=argparse.SUPPRESS)
    return parser


def main():
    argcomplete.autocomplete(build_parser())


def main_long_exclude():
    argcomplete.autocomplete(build_parser(), always_complete_options="long", exclude=["--quiet"])


def main_positional_args():
    argcomplete.autocomplete(build_parser(), False)


def main_exclusive():
    argcomplete.ExclusiveCompletionFinder()(build_parser())


def main_fromfile():
    argcomplete.autocomplete(build_parser(fromfile_prefix_chars="@"))


def main_custom_validator():
    argcomplete.autocomplete(build_parser(), validator=lambda c, p: p in c)


if __name__ == "__main__":
    main()
    os._exit(0)
