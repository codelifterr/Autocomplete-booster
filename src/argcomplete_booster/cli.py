"""``argcomplete-booster`` maintainer tool.

    argcomplete-booster generate mycli.cli:main -o src/mycli/completion-spec.json
    argcomplete-booster check    mycli.cli:main src/mycli/completion-spec.json
    argcomplete-booster parity   mycli.cli:main "mycli " "mycli deploy --"
"""

import argparse
import json
import sys


def _generate(target):
    from ._spec import make_spec
    from .testing import capture

    finder, parser, args, kwargs, original = capture(target)
    # Shipped specs are tied to the package contents, not to a user's environment.
    return make_spec(target, finder, parser, original, args, kwargs, with_fingerprint=False)


def _dump(spec):
    return json.dumps(spec, indent=1, sort_keys=True) + "\n"


def _comparable(spec):
    return {k: v for k, v in spec.items() if k != "header"} | {"target": spec["header"]["target"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="argcomplete-booster", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="write the completion spec of a CLI")
    g.add_argument("target", help="real entry point, e.g. mycli.cli:main")
    g.add_argument("-o", "--output", help="output file (default: readable JSON on stdout)")

    c = sub.add_parser("check", help="fail if a shipped spec is out of date (for CI)")
    c.add_argument("target")
    c.add_argument("spec")

    p = sub.add_parser("parity", help="compare real and fast-path completions")
    p.add_argument("target")
    p.add_argument("lines", nargs="+", help="command lines to complete (cursor at the end)")

    args = parser.parse_args(argv)

    if args.command == "generate":
        spec = _generate(args.target)
        if args.output:
            from ._cache import store

            store(args.output, spec)
        else:
            sys.stdout.write(_dump(spec))
        return 0

    if args.command == "check":
        from ._cache import load_all

        shipped = load_all(args.spec)
        current = json.loads(_dump(_generate(args.target)))
        if _comparable(shipped) != _comparable(current):
            print("%s is out of date; run: argcomplete-booster generate %s -o %s"
                  % (args.spec, args.target, args.spec), file=sys.stderr)
            return 1
        print("%s is up to date" % args.spec)
        return 0

    if args.command == "parity":
        from .testing import compare

        status = 0
        for line, real, fast in compare(args.target, args.lines):
            if fast is None:
                verdict = "FALLBACK (dynamic completer)"
            elif real == fast:
                verdict = "OK"
            else:
                verdict, status = "MISMATCH fast=%r" % fast, 1
            print("%-40r %s real=%r" % (line, verdict, real))
        return status
    return 2


if __name__ == "__main__":
    sys.exit(main())
