"""Measure completion latency: plain argcomplete vs. argcomplete-booster.

    python benchmarks/bench.py [--import-seconds 0.5] [--runs 15]

Generates throwaway CLIs of several sizes in a temp dir and times one TAB press
(one process, as bash runs it) for each variant. Prints a Markdown table.
"""

import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

CLI = '''
import argparse, time, argcomplete
time.sleep({sleep})  # stands in for heavy SDK imports

def main():
    parser = argparse.ArgumentParser(prog="bench")
    parser.add_argument("--profile", choices=["a", "b"])
    groups = parser.add_subparsers(dest="group")
    for g in range({groups}):
        gp = groups.add_parser("group%03d" % g, help="group %d" % g)
        ops = gp.add_subparsers(dest="op")
        for o in range({ops}):
            op = ops.add_parser("op%03d" % o, help="operation %d" % o)
            for a in range(10):
                op.add_argument("--arg%d" % a, help="argument %d" % a)
    argcomplete.autocomplete(parser)
'''

ENTRY = '''
from argcomplete_booster import boost
main = boost("benchcli:main")
'''


def time_once(root, module, line, cache):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([root, SRC]), ARGCOMPLETE_BOOSTER_CACHE_DIR=cache,
               _ARGCOMPLETE="1", COMP_LINE=line, COMP_POINT=str(len(line)),
               _ARGCOMPLETE_STDOUT_FILENAME=os.path.join(root, "out"))
    code = "import sys; sys.argv[0]='bench'; from %s import main; main()" % module
    start = time.perf_counter()
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    return (time.perf_counter() - start) * 1000


def cache_files(cache):
    return [f for f in os.listdir(cache) if f.endswith(".json")] if os.path.isdir(cache) else []


def median(root, module, line, cache, runs, cold=False):
    samples = []
    for _ in range(runs):
        if cold:
            for f in cache_files(cache):
                os.unlink(os.path.join(cache, f))
        samples.append(time_once(root, module, line, cache))
        if cold:  # the spec is written in the background after replying; let it finish
            deadline = time.time() + 120
            while not cache_files(cache) and time.time() < deadline:
                time.sleep(0.05)
    return statistics.median(samples)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-seconds", type=float, default=0.5)
    ap.add_argument("--runs", type=int, default=15)
    args = ap.parse_args()

    baseline = statistics.median(
        (lambda s: (subprocess.run([sys.executable, "-c", "pass"]), (time.perf_counter() - s) * 1000)[1])(
            time.perf_counter()) for _ in range(args.runs))
    print("Python %s, bare interpreter start: %.0f ms, simulated import time: %.0f ms\n"
          % (sys.version.split()[0], baseline, args.import_seconds * 1000))
    print("| command tree | spec size | plain argcomplete | booster, first TAB | booster, cached |")
    print("|---|---|---|---|---|")
    for groups, ops in [(10, 10), (50, 20), (300, 40)]:
        with tempfile.TemporaryDirectory() as root:
            cache = os.path.join(root, "cache")
            with open(os.path.join(root, "benchcli.py"), "w") as f:
                f.write(CLI.format(sleep=args.import_seconds, groups=groups, ops=ops))
            with open(os.path.join(root, "bench_entry.py"), "w") as f:
                f.write(ENTRY)
            line = "bench group001 op00"
            plain = median(root, "benchcli", line, cache, max(3, args.runs // 3))
            cold = median(root, "bench_entry", line, cache, max(3, args.runs // 3), cold=True)
            time_once(root, "bench_entry", line, cache)
            warm = median(root, "bench_entry", line, cache, args.runs)
            size = sum(os.path.getsize(os.path.join(cache, f)) for f in cache_files(cache))
            print("| %d x %d = %d commands | %.1f MB | %.0f ms | %.0f ms | **%.0f ms** |"
                  % (groups, ops, groups * ops, size / 1e6, plain, cold, warm))


if __name__ == "__main__":
    main()
