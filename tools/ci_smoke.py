#!/usr/bin/env python3
"""Check the installed public wheel independently of the source checkout."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SKILLS = ("build-kym", "build-tco-coverage")


def inventory(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"missing or unsafe Skill directory: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink in Skill: {path}")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError(f"empty Skill directory: {root}")
    return result


# -I and a temporary cwd ensure imports cannot fall back to src/ or PYTHONPATH.
INSTALLED_CHECK = r'''
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import sysconfig
import kym_tco
from kym_tco.workspace import load_config

source, work = map(Path, sys.argv[1:])
installed_module = Path(kym_tco.__file__).resolve()
if source == installed_module or source in installed_module.parents:
    raise RuntimeError("source checkout imported instead of installed wheel")
distribution = importlib.metadata.distribution("bm-kym-tco")
if installed_module != Path(distribution.locate_file("kym_tco/__init__.py")).resolve():
    raise RuntimeError("imported module does not belong to installed bm-kym-tco")
scripts = Path(sysconfig.get_path("scripts"))
for name in ("kym", "tco"):
    subprocess.run([str(scripts / name), "--help"], cwd=work, check=True, stdout=subprocess.DEVNULL)
subprocess.run([str(scripts / "kym"), "--workspace", str(work), "init",
                "--product-id", "ci-smoke"], cwd=work, check=True, stdout=subprocess.DEVNULL)
config = load_config(work / ".kym-tco" / "ci-smoke")
if config["product_id"] != "ci-smoke":
    raise RuntimeError("installed CLI/schema smoke failed")
print(json.dumps({"version": distribution.version,
                  "skill_root": str(Path(sysconfig.get_path("data")) / "share/bm-kym-tco/codex/skills")}))
'''


def smoke(source: Path) -> dict[str, object]:
    source = source.resolve()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="bm-kym-tco-smoke-") as temporary:
        work = Path(temporary)
        result = subprocess.run(
            [sys.executable, "-I", "-c", INSTALLED_CHECK, str(source), str(work)],
            cwd=work, env=env, check=True, capture_output=True, text=True,
        )
        installed = json.loads(result.stdout)
        installed_skills = Path(installed["skill_root"])
        if sorted(path.name for path in installed_skills.iterdir() if path.is_dir()) != sorted(SKILLS):
            raise ValueError("wheel must contain exactly the two public Skills")
        for name in SKILLS:
            if inventory(source / ".codex/skills" / name) != inventory(installed_skills / name):
                raise ValueError(f"wheel Skill content mismatch: {name}")
        command = [sys.executable, "-I", str(source / "tools/install_codex_skills.py"),
                   "--source", str(installed_skills), "--target", str(work / "skills")]
        subprocess.run(command + ["--install"], cwd=work, env=env, check=True, capture_output=True, text=True)
        repeated = subprocess.run(command, cwd=work, env=env, check=True, capture_output=True, text=True)
        report = json.loads(repeated.stdout)
        expected = [{"skill": name, "action": "unchanged"} for name in sorted(SKILLS)]
        if report["actions"] != expected or report["wrote"]:
            raise ValueError("repeated Skill installation was not unchanged")
        for name in SKILLS:
            if inventory(work / "skills" / name) != inventory(source / ".codex/skills" / name):
                raise ValueError(f"installed Skill content mismatch: {name}")
    return {"status": "pass", "version": installed["version"], "skills": list(SKILLS)}


def main() -> int:
    try:
        print(json.dumps(smoke(Path(__file__).resolve().parents[1]), sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"installed-wheel smoke failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
