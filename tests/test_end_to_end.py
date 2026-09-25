"""Run a boosted console script in a subprocess, the way bash does."""

import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

CLI = '''
# PYTHON_ARGCOMPLETE_OK
import argparse
import argcomplete

COMMANDS = {commands!r}

def regions(prefix, parsed_args, **kwargs):
    return ["eu-1", "us-1"]

def main():
    parser = argparse.ArgumentParser(prog="demo")
    parser.add_argument("--region").completer = regions
    sub = parser.add_subparsers(dest="cmd")
    for name in COMMANDS:
        sub.add_parser(name).add_argument("--force", action="store_true")
    argcomplete.autocomplete(parser)
    print("ran", parser.parse_args())
'''

ENTRY = '''
# PYTHON_ARGCOMPLETE_OK
from argcomplete_booster import boost
main = boost("demo.cli:main"{extra})
'''


@pytest.fixture
def project(tmp_path):
    """A 'demo' package whose import is recorded in a log file."""

    class Project:
        root = tmp_path / "site"
        cache = tmp_path / "cache"
        log = tmp_path / "imports.log"

        def write(self, commands, extra=""):
            pkg = self.root / "demo"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "__init__.py").write_text(
                "with open(%r, 'a') as f: f.write('imported\\n')\n" % str(self.log)
            )
            cli = pkg / "cli.py"
            before = cli.stat().st_mtime_ns if cli.exists() else 0
            cli.write_text(textwrap.dedent(CLI.format(commands=commands)))
            # Make sure the change is visible even on coarse-mtime filesystems.
            os.utime(cli, ns=(before + 10**9, before + 10**9))
            (self.root / "demo_entry.py").write_text(ENTRY.format(extra=extra))

        def run(self, line=None, env=None, argv=()):
            if self.log.exists():
                self.log.unlink()
            out = self.root.parent / "out.txt"
            full_env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(self.root), SRC]))
            full_env["ARGCOMPLETE_BOOSTER_CACHE_DIR"] = str(self.cache)
            full_env["ARGCOMPLETE_BOOSTER_SYNC"] = "1"  # deterministic; see test_cache_written_in_background
            for k in list(full_env):
                if k.startswith("_ARGCOMPLETE") or k in ("COMP_LINE", "COMP_POINT", "ARGCOMPLETE_BOOSTER"):
                    del full_env[k]
            if line is not None:
                full_env.update(
                    _ARGCOMPLETE="1",
                    COMP_LINE=line,
                    COMP_POINT=str(len(line)),
                    _ARGCOMPLETE_STDOUT_FILENAME=str(out),
                )
            full_env.update(env or {})
            code = "import sys; sys.argv[0] = 'demo'; from demo_entry import main; sys.exit(main())"
            proc = subprocess.run(
                [sys.executable, "-c", code, *argv], env=full_env, capture_output=True, text=True, timeout=60
            )
            assert proc.returncode == 0, proc.stderr
            imported = self.log.exists()
            if line is None:
                return proc.stdout, imported
            result = out.read_text().split("\013") if out.exists() else []
            return result, imported

        def cache_files(self):
            return sorted(self.cache.glob("*.json")) if self.cache.exists() else []

    return Project()


def test_miss_then_hit(project):
    project.write(["build", "bundle"])
    assert project.run("demo b") == (["build", "bundle"], True)  # miss: real CLI, writes cache
    assert len(project.cache_files()) == 1
    assert project.run("demo b") == (["build", "bundle"], False)  # hit: package never imported
    assert project.run("demo build --f") == (["--force "], False)


def test_changed_cli_invalidates_cache(project):
    project.write(["build"])
    project.run("demo ")
    project.write(["build", "deploy"])  # "pip install -U" with a new command
    assert project.run("demo d") == (["deploy "], True)
    assert project.run("demo d") == (["deploy "], False)


def test_site_packages_change_invalidates_cache(project, tmp_path):
    project.write(["build"])
    project.run("demo ")
    spec_path = project.cache_files()[0]
    spec = json.loads(spec_path.read_text())
    # Simulate a directory that pip rewrites on install (like site-packages).
    fake_site = tmp_path / "fake-site-packages"
    fake_site.mkdir()
    st = fake_site.stat()
    spec["fingerprint"].append([str(fake_site), st.st_mtime_ns, st.st_size])
    spec_path.write_text(json.dumps(spec))
    assert project.run("demo ")[1] is False
    (fake_site / "other-2.0.dist-info").mkdir()
    os.utime(fake_site, ns=(st.st_mtime_ns + 10**9, st.st_mtime_ns + 10**9))
    assert project.run("demo ")[1] is True


def test_dynamic_completer_uses_real_cli_and_keeps_cache(project):
    project.write(["build"])
    project.run("demo ")
    before = project.cache_files()[0].stat().st_mtime_ns
    assert project.run("demo --region ") == (["eu-1", "us-1"], True)
    assert project.cache_files()[0].stat().st_mtime_ns == before
    assert project.run("demo b") == (["build "], False)


def test_corrupt_cache_is_ignored(project):
    project.write(["build"])
    project.run("demo ")
    project.cache_files()[0].write_text("{not json")
    assert project.run("demo b") == (["build "], True)
    assert project.run("demo b") == (["build "], False)


def test_unwritable_cache_still_completes(project):
    project.write(["build"])
    project.cache.write_text("a file where the directory should be")
    assert project.run("demo b") == (["build "], True)


def test_kill_switch(project):
    project.write(["build"])
    project.run("demo ")
    assert project.run("demo b", env={"ARGCOMPLETE_BOOSTER": "0"}) == (["build "], True)


def test_normal_run_is_untouched(project):
    project.write(["build"])
    out, imported = project.run(argv=["build", "--force"])
    assert imported and "ran Namespace(region=None, cmd='build', force=True)" in out
    assert project.cache_files() == []


def test_shipped_spec(project):
    project.write(["build", "bundle"])
    spec_file = project.root / "demo_spec.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(project.root), SRC]))
    subprocess.run(
        [sys.executable, "-m", "argcomplete_booster.cli", "generate", "demo.cli:main", "-o", str(spec_file)],
        env=env, check=True,
    )
    project.write(["build", "bundle"], extra=", spec_file=%r" % str(spec_file))
    assert project.run("demo b") == (["build", "bundle"], False)  # fast from the very first TAB
    assert project.cache_files() == []

    check = [sys.executable, "-m", "argcomplete_booster.cli", "check", "demo.cli:main", str(spec_file)]
    assert subprocess.run(check, env=env, capture_output=True).returncode == 0
    project.write(["build", "bundle", "deploy"], extra=", spec_file=%r" % str(spec_file))
    assert subprocess.run(check, env=env, capture_output=True).returncode == 1


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_real_fd8_protocol(project):
    """Without _ARGCOMPLETE_STDOUT_FILENAME, completions go to fd 8 as with bash."""
    project.write(["build", "bundle"])
    project.run("demo ")  # warm the cache
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(project.root), SRC]),
               ARGCOMPLETE_BOOSTER_CACHE_DIR=str(project.cache),
               _ARGCOMPLETE="1", COMP_LINE="demo b", COMP_POINT="6")
    code = "import sys; sys.argv[0] = 'demo'; from demo_entry import main; sys.exit(main())"
    for line in ("demo b", "demo --region "):  # fast path, then fallback after fast path opened fd 8
        env.update(COMP_LINE=line, COMP_POINT=str(len(line)))
        proc = subprocess.run(["bash", "-c", '"$0" -c "$1" 8>&1 9>/dev/null', sys.executable, code],
                              env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split("\013") == (["build", "bundle"] if line == "demo b" else ["eu-1", "us-1"])


def test_cache_written_in_background(project):
    """By default the spec is written by a detached child after replying."""
    import time

    project.write(["build"])
    assert project.run("demo b", env={"ARGCOMPLETE_BOOSTER_SYNC": ""}) == (["build "], True)
    deadline = time.time() + 20
    while not project.cache_files() and time.time() < deadline:
        time.sleep(0.05)
    assert project.cache_files()
    assert project.run("demo b") == (["build "], False)

