#!/usr/bin/env python3
"""Validate the existing zpty library, fixtures and package at one source commit."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import tomllib


def output(*command):
    return subprocess.check_output(command, text=True, encoding="utf-8").strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/acceptance")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["CARGO_TERM_COLOR"] = "never"
    report = {
        "schema": 1,
        "source": output("git", "rev-parse", "HEAD"),
        "source_worktree_dirty": bool(output("git", "status", "--porcelain")),
        "recorded_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": platform.platform(),
        "rustc": output("rustc", "--version"),
        "commands": [],
        "passed": False,
    }

    def command(name, arguments, timeout=600):
        started = time.monotonic()
        print("+ " + " ".join(arguments), flush=True)
        result = subprocess.run(
            arguments, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        (destination / (name + ".log")).write_text(result.stdout, encoding="utf-8")
        print(result.stdout, end="", flush=True)
        report["commands"].append({
            "name": name, "command": arguments, "exit_code": result.returncode,
            "seconds": time.monotonic() - started,
        })
        if result.returncode:
            raise RuntimeError(f"{name} exited {result.returncode}")
        return result.stdout

    try:
        provenance = json.loads(Path("SOURCE.json").read_text(encoding="utf-8"))
        actual = {p.as_posix() for p in Path("src").rglob("*.rs")}
        expected = provenance["production_files"]
        if actual != set(expected):
            raise ValueError("The five-file extraction source boundary changed")
        for name, identity in expected.items():
            data = Path(name).read_bytes()
            sha256 = hashlib.sha256(data).hexdigest()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if sha256 != identity["sha256"] or blob != identity["git_blob"]:
                raise ValueError(f"Production source differs from the extraction: {name}")
        report["source_identity"] = {
            "files": len(expected), "unchanged": True,
            "upstream_commit": provenance["upstream_commit"],
        }

        manifest = tomllib.loads(Path("Cargo.toml").read_text(encoding="utf-8"))
        package = manifest["package"]
        if package["name"] != "zpty" or "workspace" not in manifest:
            raise ValueError("Expected the standalone zpty workspace")
        if any(not entry.startswith("/") for entry in package["include"]):
            raise ValueError("Package includes must be anchored at the repository root")
        if "#![no_std]" not in Path("src/lib.rs").read_text(encoding="utf-8"):
            raise ValueError("The library must remain no_std")
        metadata = json.loads(command("metadata", [
            "cargo", "metadata", "--locked", "--offline", "--no-deps", "--format-version", "1",
        ]))
        packages = metadata["packages"]
        if len(packages) != 1 or packages[0]["name"] != "zpty" or packages[0]["dependencies"]:
            raise ValueError("zpty must remain one dependency-free package")
        report["package"] = {"name": "zpty", "version": package["version"], "cargo_dependencies": 0}

        command("format", ["cargo", "fmt", "--all", "--", "--check"])
        command("no-std", ["cargo", "check", "--locked", "--lib", "--no-default-features"])
        command("build", ["cargo", "build", "--locked", "--release", "--lib"])
        units = command("unit-tests", ["cargo", "test", "--locked", "--lib"])
        docs = command("doc-tests", ["cargo", "test", "--locked", "--doc"])
        counts = lambda text: [int(n) for n in re.findall(r"test result: ok\. (\d+) passed;", text)]
        report["unit_tests_passed"] = sum(counts(units))
        report["doc_tests_passed"] = sum(counts(docs))
        command("capture", ["cargo", "run", "--locked", "--example", "capture"], timeout=180)
        if platform.system() == "Darwin":
            native = command("darwin-spawn", [
                "cargo", "test", "--locked", "--test", "darwin_spawn",
            ], timeout=300)
            receipts = [
                json.loads(line) for line in native.splitlines()
                if line.startswith('{"suite":"darwin-threaded-spawn"')
            ]
            if len(receipts) != 1 or receipts[0].get("passed") is not True:
                raise ValueError("The native Darwin fixture did not emit its success receipt")
            report["darwin_spawn"] = receipts[0]
        else:
            report["darwin_spawn"] = {"status": "not_run", "reason": "requires real macOS"}
        command("clippy", ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"])

        fixture = Path("artifacts/package-boundary-fixture/nested")
        fixture.mkdir(parents=True, exist_ok=True)
        for name in ("README.md", "LICENSE"):
            (fixture / name).write_text("Ignored package boundary fixture\n", encoding="utf-8")
        listing = set(command("package-list", ["cargo", "package", "--locked", "--list"]).splitlines())
        required = set(expected) | {
            "README.md", "LICENSE", "SOURCE.json", "examples/capture.rs",
            "tests/darwin_spawn.rs", "tests/darwin_spawn_abi.c",
        }
        leaked = [name for name in listing if name.startswith(("artifacts/", "target/"))]
        if required - listing or leaked:
            raise ValueError(f"Invalid package boundary: missing={sorted(required-listing)}, leaked={leaked}")
        command("package", ["cargo", "package", "--locked"])
        target = Path(os.environ.get("CARGO_TARGET_DIR", "target"))
        crate = target / "package" / f"zpty-{package['version']}.crate"
        report["crate"] = {
            "path": str(crate), "sha256": hashlib.sha256(crate.read_bytes()).hexdigest(),
            "bytes": crate.stat().st_size,
        }
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (destination / "validation.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8",
        )
    print(json.dumps({
        "passed": report["passed"], "source": report["source"],
        "unit_tests_passed": report["unit_tests_passed"],
        "doc_tests_passed": report["doc_tests_passed"],
        "darwin_spawn": report["darwin_spawn"].get("status", "passed"),
        "output": str(destination),
    }), flush=True)


if __name__ == "__main__":
    main()
