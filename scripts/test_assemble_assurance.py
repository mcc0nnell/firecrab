#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("assemble_assurance", ROOT / "assemble_assurance.py")
assert spec and spec.loader
assurance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assurance)

SHA = "a" * 40
DIGEST = "b" * 64


class AssembleAssuranceTests(unittest.TestCase):
    def fixture(self, tmp: Path):
        profile = tmp / "profile.json"
        manifest = tmp / "m2images.json"
        preflight = tmp / "preflight.json"
        evidence = tmp / "assurance"
        profile.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "profile": "firecrab-release-assurance-v1",
                    "stages": [
                        {
                            "id": "host-release-assurance",
                            "matrix": {"targets": ["x86_64-unknown-linux-gnu"]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "architectures": ["x86_64"],
                    "images": [{"alias": "alpine-test"}],
                }
            ),
            encoding="utf-8",
        )
        preflight.write_text(
            json.dumps({"schemaVersion": 1, "sha": SHA, "verdict": "PASS"}),
            encoding="utf-8",
        )
        return profile, manifest, preflight, evidence

    def write_m2(self, evidence: Path, *, sha: str = SHA, artifacts: bool = True):
        path = evidence / "m2images" / "alpine-test" / "x86_64" / "result.json"
        path.parent.mkdir(parents=True)
        doc = {
            "schemaVersion": 1,
            "verdict": "PASS",
            "reason": "ok",
            "subject": {"sha": sha, "alias": "alpine-test", "architecture": "x86_64"},
        }
        if artifacts:
            doc["binaryArtifact"] = {"bytes": 1, "sha256": DIGEST}
            doc["sourceArtifact"] = {"bytes": 1, "sha256": DIGEST}
        path.write_text(json.dumps(doc), encoding="utf-8")

    def write_host(self, evidence: Path, *, sha: str = SHA):
        target = "x86_64-unknown-linux-gnu"
        path = evidence / "host" / target / "result.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "verdict": "PASS",
                    "reason": "ok",
                    "subject": {"sha": sha, "target": target},
                    "artifact": {"bytes": 1, "sha256": DIGEST},
                }
            ),
            encoding="utf-8",
        )

    def invoke(self, profile: Path, manifest: Path, preflight: Path, evidence: Path):
        return assurance.main(
            [
                "--root", str(evidence),
                "--profile", str(profile),
                "--manifest", str(manifest),
                "--preflight", str(preflight),
                "--sha", SHA,
            ]
        )

    def test_complete_matrix_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile, manifest, preflight, evidence = self.fixture(Path(tmpdir))
            self.write_m2(evidence)
            self.write_host(evidence)
            self.assertEqual(self.invoke(profile, manifest, preflight, evidence), 0)
            verdict = json.loads((evidence / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "PASS")
            self.assertEqual(verdict["counts"], {"BLOCKED": 0, "FAIL": 0, "PASS": 3})
            self.assertTrue((evidence / "SHA256SUMS").is_file())

    def test_missing_required_cell_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile, manifest, preflight, evidence = self.fixture(Path(tmpdir))
            self.write_m2(evidence)
            self.assertEqual(self.invoke(profile, manifest, preflight, evidence), 3)
            verdict = json.loads((evidence / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "BLOCKED")
            self.assertEqual(verdict["counts"]["BLOCKED"], 1)

    def test_component_sha_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile, manifest, preflight, evidence = self.fixture(Path(tmpdir))
            self.write_m2(evidence, sha="c" * 40)
            self.write_host(evidence)
            self.assertEqual(self.invoke(profile, manifest, preflight, evidence), 1)
            verdict = json.loads((evidence / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "FAIL")

    def test_pass_without_artifact_hashes_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile, manifest, preflight, evidence = self.fixture(Path(tmpdir))
            self.write_m2(evidence, artifacts=False)
            self.write_host(evidence)
            self.assertEqual(self.invoke(profile, manifest, preflight, evidence), 1)
            verdict = json.loads((evidence / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
