# PYTHON_ARGCOMPLETE_OK
import argparse

import argcomplete
from argcomplete.completers import ChoicesCompleter, FilesCompleter

from argcomplete_booster import static_completer

SERVICES = ["compute", "storage", "network", "database", "queue", "dns", "iam", "logs"] + [
    "service%02d" % i for i in range(40)
]
OPERATIONS = ["list", "get", "create", "delete", "update", "describe", "tag", "untag"]


def region_completer(prefix, parsed_args, **kwargs):
    # Dynamic: would normally call an API. Depends on other arguments.
    zone = getattr(parsed_args, "zone_prefix", None) or "eu"
    return ["%s-west-1" % zone, "%s-central-1" % zone]


@static_completer
def output_formats(**kwargs):
    return {"json": "machine readable", "table": "human readable", "yaml": "YAML"}


def build_parser():
    parser = argparse.ArgumentParser(prog="heavycli", description="Demo cloud CLI")
    parser.add_argument("--version", action="version", version="1.0.0")
    parser.add_argument("-v", "--verbose", action="count", help="more output")
    parser.add_argument("--profile", choices=["default", "staging", "prod"], help="profile to use")
    parser.add_argument("--output", help="output format (default: %(default)s)", default="json").completer = output_formats
    parser.add_argument("--config", type=str, help="config file").completer = FilesCompleter(["toml", "ini"])
    parser.add_argument("--zone-prefix", help=argparse.SUPPRESS)
    parser.add_argument("--region", help="region").completer = region_completer
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--token", help="API token")
    auth.add_argument("--anonymous", action="store_true", help="no auth")

    services = parser.add_subparsers(dest="service", metavar="SERVICE")
    for name in SERVICES:
        aliases = [name[:3]] if name in ("compute", "storage", "network") else []
        sp = services.add_parser(name, aliases=aliases, help="manage %s resources" % name)
        ops = sp.add_subparsers(dest="operation", required=True)
        for op in OPERATIONS:
            o = ops.add_parser(op, help="%s %s" % (op, name))
            o.add_argument("--id", help="resource id")
            o.add_argument("--limit", type=int, choices=[10, 50, 100])
            o.add_argument("--tags", nargs="*", help="key=value tags")
            o.add_argument("--dry-run", action="store_true")
            if op in ("create", "update"):
                o.add_argument("spec_file", help="resource spec").completer = FilesCompleter(["json", "yaml"])
                o.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
            if op == "get":
                o.add_argument("fields", nargs="*", choices=["id", "name", "state", "created"])
    return parser


def main():
    parser = build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    print(args)
    return 0
