#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

# Use the exact pinned Sandia Pipe API without importing TalkPipe's optional
# application stack. The workflow checks out TalkPipe under /tmp/talkpipe.
talkpipe_pkg = types.ModuleType("talkpipe")
talkpipe_pkg.__path__ = ["/tmp/talkpipe/src/talkpipe"]
sys.modules["talkpipe"] = talkpipe_pkg
from talkpipe.pipe.core import segment, source

ROOT = Path("/work")
spec = importlib.util.spec_from_file_location(
    "m2image_source_publication", ROOT / "scripts/m2image_source_publication.py"
)
assert spec and spec.loader
sourcepub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sourcepub)


def smap(distribution, packages):
    return {
        "schemaVersion": 1,
        "image": {
            "alias": f"{distribution}-image",
            "version": "1",
            "distribution": distribution,
            "architecture": "x86_64",
        },
        "packages": packages,
    }


def pkg(name, source):
    return {
        "binaryPackage": name,
        "binaryVersion": "1-r0",
        "architecture": "x86_64",
        "declaredLicense": "GPL-2.0-only",
        "source": source,
    }


ALPINE = {
    "type": "alpine-aports",
    "sourcePackage": "busybox",
    "sourceVersion": "1.37.0-r31",
    "repositoryCommit": "1" * 40,
}

SCENARIOS = [
    {"name": "complete", "expected": "pass", "kind": "complete"},
    {"name": "dedupe-subpackages", "expected": "pass", "kind": "dedupe"},
    {"name": "missing-alpine-commit", "expected": "reject", "kind": "missing-commit"},
    {"name": "wrong-resolver", "expected": "reject", "kind": "wrong-resolver"},
    {"name": "empty-source-unit", "expected": "reject", "kind": "empty-source"},
    {"name": "coverage-gap", "expected": "reject", "kind": "coverage-gap"},
    {"name": "deterministic-order", "expected": "pass", "kind": "deterministic"},
]


@source()
def cases():
    yield from SCENARIOS


@segment()
def exercise(items):
    for case in items:
        outcome = "pass"
        detail = ""
        try:
            source = dict(ALPINE)
            packages = [pkg("busybox", source)]
            if case["kind"] == "dedupe":
                packages.append(pkg("busybox-binsh", dict(source)))
            elif case["kind"] == "missing-commit":
                source.pop("repositoryCommit")
            elif case["kind"] == "wrong-resolver":
                source["type"] = "ubuntu-source-package"
            plan = sourcepub.publication_plan(smap("alpine", packages))

            if case["kind"] in {"empty-source", "coverage-gap"}:
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir) / "bundle" / "sources"
                    root.mkdir(parents=True)
                    if case["kind"] == "empty-source":
                        (root / plan["sources"][0]["sourceId"]).mkdir()
                    else:
                        unit = root / plan["sources"][0]["sourceId"]
                        unit.mkdir()
                        (unit / "source.tar.gz").write_bytes(b"source")
                        plan["packageCount"] += 1
                    sourcepub.source_index(plan, root)
            elif case["kind"] == "deterministic":
                second = sourcepub.publication_plan(smap("alpine", list(reversed(packages))))
                if json.dumps(plan, sort_keys=True) != json.dumps(second, sort_keys=True):
                    raise ValueError("publication plan changed with input order")
            elif case["kind"] == "dedupe" and plan["sourceCount"] != 1:
                raise ValueError("subpackages did not deduplicate to one source unit")
        except (ValueError, OSError, AssertionError) as exc:
            outcome = "reject"
            detail = str(exc)

        yield {
            "name": case["name"],
            "expected": case["expected"],
            "outcome": outcome,
            "detail": detail,
        }


pipeline = cases() | exercise()
results = list(pipeline())
failed = False
for result in results:
    print(
        f"{result['name']}: expected={result['expected']} got={result['outcome']}"
        + (f" ({result['detail']})" if result['detail'] else "")
    )
    failed |= result["expected"] != result["outcome"]

if failed:
    raise SystemExit("TalkPipe #176 source-publication adversary found mismatches")
print(f"TalkPipe #176 source-publication adversary passed: {len(results)} scenarios")
