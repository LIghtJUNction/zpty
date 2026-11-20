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
        origin = provenance["production_files"]
        maintained = provenance.get("maintained_files", {})
        if not isinstance(origin, dict) or not isinstance(maintained, dict):
            raise ValueError("Source identities must be objects")
        if actual != set(origin):
            raise ValueError("The five-file extraction source boundary changed")
        if not set(maintained).issubset(origin):
            raise ValueError("Maintained identities cannot silently expand the production boundary")
        expected = {**origin, **maintained}
        evolved = []
        for name, identity in expected.items():
            data = Path(name).read_bytes()
            sha256 = hashlib.sha256(data).hexdigest()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if sha256 != identity["sha256"] or blob != identity["git_blob"]:
                scope = "maintained revision" if name in maintained else "historical extraction"
                raise ValueError(f"Production source differs from its {scope}: {name}")
            if name in maintained:
                if not isinstance(identity.get("reason"), str) or not identity["reason"].strip():
                    raise ValueError(f"Maintained source needs a change reason: {name}")
                if (sha256, blob) == (origin[name]["sha256"], origin[name]["git_blob"]):
                    raise ValueError(f"Unchanged source should retain only its origin identity: {name}")
                evolved.append(name)
        report["source_identity"] = {
            "files": len(expected), "unchanged": not evolved,
            "upstream_commit": provenance["upstream_commit"],
            "evolved_files": sorted(evolved),
            "origin_unchanged_files": sorted(set(origin) - set(evolved)),
            "identity_policy": "historical production_files plus explicit maintained_files overrides",
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
        restore_tests = [entry for entry in manifest.get("test", [])
                         if entry.get("name") == "linux_restore"]
        if (len(restore_tests) != 1
                or restore_tests[0].get("path") != "tests/linux_restore.rs"
                or restore_tests[0].get("harness") is not False):
            raise ValueError("The Linux restore fixture must run without libtest worker threads")
        report["package"] = {"name": "zpty", "version": package["version"], "cargo_dependencies": 0}

        command("format", ["cargo", "fmt", "--all", "--", "--check"])
        command("no-std", ["cargo", "check", "--locked", "--lib", "--no-default-features"])
        command("build", ["cargo", "build", "--locked", "--release", "--lib"])
        units = command("unit-tests", ["cargo", "test", "--locked", "--lib"])
        docs = command("doc-tests", ["cargo", "test", "--locked", "--doc"])
        command("documentation", ["cargo", "doc", "--locked", "--no-deps"])
        counts = lambda text: [int(n) for n in re.findall(r"test result: ok\. (\d+) passed;", text)]
        report["unit_tests_passed"] = sum(counts(units))
        report["doc_tests_passed"] = sum(counts(docs))
        command("capture", ["cargo", "run", "--locked", "--example", "capture"], timeout=180)
        if platform.system() == "Linux":
            restored = command("linux-restore", [
                "cargo", "test", "--locked", "--test", "linux_restore",
            ], timeout=60)
            receipts = [
                json.loads(line) for line in restored.splitlines()
                if line.startswith('{"suite":"linux-pty-exec-restore"')
            ]
            if (len(receipts) != 1 or receipts[0].get("passed") is not True
                    or receipts[0].get("same_parent_pid") is not True
                    or receipts[0].get("child_exit_code") != 7):
                raise ValueError("The Linux exec/restore fixture did not emit its success receipt")
            report["linux_restore"] = receipts[0]
        else:
            report["linux_restore"] = {"status": "not_run", "reason": "requires real Linux"}
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
        # Cargo prints host-native path separators on Windows.
        listing = {
            Path(name).as_posix()
            for name in command("package-list", ["cargo", "package", "--locked", "--list"]).splitlines()
        }
        required = set(expected) | {
            "README.md", "LICENSE", "SOURCE.json", "examples/capture.rs",
            "tests/darwin_spawn.rs", "tests/darwin_spawn_abi.c", "tests/linux_restore.rs",
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
        "linux_restore": report["linux_restore"].get("status", "passed"),
        "output": str(destination),
    }), flush=True)


if __name__ == "__main__":
    main()
