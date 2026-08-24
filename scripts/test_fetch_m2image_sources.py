#!/usr/bin/env python3
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "fetch_m2image_sources", ROOT / "fetch_m2image_sources.py"
)
assert spec and spec.loader
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


class AlpineDistfilesRecoveryTests(unittest.TestCase):
    def test_literal_sha512sums_extracts_exact_filename(self):
        payload = b"archived source bytes"
        digest = hashlib.sha512(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            apkbuild = Path(tmpdir) / "APKBUILD"
            apkbuild.write_text(
                'pkgname=iproute2\n'
                'source="https://kernel.example/iproute2-v7.0.0.tar.xz"\n'
                'sha512sums="\n'
                f'{digest}  iproute2-v7.0.0.tar.xz\n'
                '"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                fetcher._literal_sha512sums(apkbuild),
                {"iproute2-v7.0.0.tar.xz": digest},
            )

    def test_dynamic_or_unverified_checksum_syntax_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            apkbuild = Path(tmpdir) / "APKBUILD"
            apkbuild.write_text(
                'sha512sums="\nSKIP  source.tar.xz\n"\n', encoding="utf-8"
            )
            self.assertEqual(fetcher._literal_sha512sums(apkbuild), {})

    def test_distfiles_branch_comes_from_release_series(self):
        self.assertEqual(fetcher._alpine_distfiles_branch({"version": "3.24.1"}), "v3.24")
        with self.assertRaisesRegex(ValueError, "cannot derive Alpine distfiles branch"):
            fetcher._alpine_distfiles_branch({"version": "edge"})

    def test_recovery_accepts_only_checksum_matching_archive(self):
        payload = b"iproute2 exact source bytes"
        digest = hashlib.sha512(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipe = root / "recipe"
            distfiles = root / "distfiles"
            recipe.mkdir()
            distfiles.mkdir()
            (recipe / "APKBUILD").write_text(
                'sha512sums="\n'
                f'{digest}  iproute2-v7.0.0.tar.xz\n'
                '"\n',
                encoding="utf-8",
            )

            def fake_fetch(url, output):
                self.assertEqual(
                    url,
                    "https://distfiles.alpinelinux.org/distfiles/v3.24/"
                    "iproute2-v7.0.0.tar.xz",
                )
                output.write_bytes(payload)
                return True

            with mock.patch.object(fetcher, "fetch_url", side_effect=fake_fetch):
                recovered = fetcher._recover_alpine_distfiles(
                    recipe, distfiles, {"version": "3.24.1"}
                )

            self.assertEqual(len(recovered), 1)
            self.assertEqual(
                (distfiles / "iproute2-v7.0.0.tar.xz").read_bytes(), payload
            )

    def test_recovery_rejects_checksum_mismatch_and_removes_file(self):
        expected = hashlib.sha512(b"expected").hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipe = root / "recipe"
            distfiles = root / "distfiles"
            recipe.mkdir()
            distfiles.mkdir()
            (recipe / "APKBUILD").write_text(
                'sha512sums="\n'
                f'{expected}  source.tar.xz\n'
                '"\n',
                encoding="utf-8",
            )

            def fake_fetch(_url, output):
                output.write_bytes(b"wrong")
                return True

            with mock.patch.object(fetcher, "fetch_url", side_effect=fake_fetch):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    fetcher._recover_alpine_distfiles(
                        recipe, distfiles, {"version": "3.24.1"}
                    )
            self.assertFalse((distfiles / "source.tar.xz").exists())

    def test_local_recipe_files_are_never_replaced_from_distfiles(self):
        payload = b"local patch"
        digest = hashlib.sha512(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recipe = root / "recipe"
            distfiles = root / "distfiles"
            recipe.mkdir()
            distfiles.mkdir()
            (recipe / "fix.patch").write_bytes(payload)
            (recipe / "APKBUILD").write_text(
                'sha512sums="\n'
                f'{digest}  fix.patch\n'
                '"\n',
                encoding="utf-8",
            )
            with mock.patch.object(fetcher, "fetch_url") as mocked:
                recovered = fetcher._recover_alpine_distfiles(
                    recipe, distfiles, {"version": "3.24.1"}
                )
            self.assertEqual(recovered, [])
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
