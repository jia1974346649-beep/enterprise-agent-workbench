"""Publication checks reject actual artifacts, not just helper return values."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib


SPEC = importlib.util.spec_from_file_location("public_audit", Path(__file__).resolve().parents[1] / "scripts" / "audit_public_package.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def tiny_png(extra=b""):
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return AUDIT.PNG_SIGNATURE + png_chunk(b"IHDR", header) + extra + png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + png_chunk(b"IEND", b"")


class PublicPackageAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)

    def manifest(self, names, exceptions=None):
        data = {"schema_version": 1, "files": [{"path": name, "sha256": hashlib.sha256((self.root / name).read_bytes()).hexdigest()} for name in names]}
        if exceptions:
            data["literal_exceptions"] = exceptions
        self.write("publication-manifest.json", json.dumps(data))

    def scan(self, deny=None):
        audit = AUDIT.Auditor(self.root, deny_terms=deny)
        audit.worktree()
        return audit

    def codes(self, audit):
        return {item["code"] for item in audit.findings}

    def test_reviewed_hash_and_loopback_pass(self):
        self.write("README.md", "Local demo: http://localhost:8765 http://127.0.0.1:8765 http://[::1]:8765")
        self.manifest(["README.md"])
        self.assertTrue(self.scan().report()["ok"])

    def test_markdown_loopback_links_keep_token_boundaries_and_ipv6(self):
        self.write("README.md", "[http://localhost:8765](http://localhost:8765) "
                   "[IPv6](http://[::1]:8765) [http://[::1]:8765](http://[::1]:8765)")
        self.manifest(["README.md"])
        self.assertTrue(self.scan().report()["ok"])
        self.write("README.md", "[local](http://localhost:8765) [remote](https://" + "unreviewed.invalid)")
        self.manifest(["README.md"])
        self.assertIn("unreviewed_url", self.codes(self.scan()))

    def test_modified_file_and_extra_file_rejected(self):
        self.write("README.md", "reviewed")
        self.manifest(["README.md"])
        self.write("README.md", "changed")
        self.write("extra.txt", "not in the list")
        self.assertTrue({"hash_mismatch", "unexpected_file"}.issubset(self.codes(self.scan())))

    def test_ignored_cache_does_not_expand_publication(self):
        self.write(".gitignore", "__pycache__/\n*.pyc\n")
        self.write("README.md", "reviewed")
        self.manifest([".gitignore", "README.md"])
        self.write("__pycache__/cache.pyc", b"\x00")
        self.assertTrue(self.scan().report()["ok"])

    def test_private_deny_wins_over_literal_exception_without_logging_it(self):
        marker = "PRIVATE" + "_ORGANIZATION"
        self.write("sample.py", 'value = "' + marker + '"')
        self.manifest(["sample.py"], {"sample.py": [marker]})
        audit = self.scan([marker])
        self.assertIn("private_deny_match", self.codes(audit))
        self.assertNotIn(marker, json.dumps(audit.report()))

    def test_exact_literal_does_not_hide_another_value_on_same_line(self):
        first = "203.0." + "113.7"
        second = "198.51." + "100.9"
        self.write("sample.py", repr(first) + "; " + repr(second))
        self.manifest(["sample.py"], {"sample.py": [first]})
        self.assertIn("non_loopback_ipv4", self.codes(self.scan()))
        self.write("sample.py", repr(first))
        self.manifest(["sample.py"], {"sample.py": [first]})
        self.assertTrue(self.scan().report()["ok"])

    def test_real_contact_secret_and_nonlocal_url_rejected(self):
        email = "contact@" + "unreviewed.invalid"
        phone = "138" + "12345678"
        secret = "ghp_" + "a" * 30
        endpoint = "http://" + "remote.invalid/api"
        self.write("README.md", "\n".join([email, phone, secret, endpoint]))
        self.manifest(["README.md"])
        self.assertTrue({"non_example_email", "phone_like_value", "secret_like_value", "unreviewed_url"}.issubset(self.codes(self.scan())))

    def test_exact_loopback_name_not_suffix_match(self):
        self.write("README.md", "http://" + "localhost.unreviewed.invalid/api")
        self.manifest(["README.md"])
        self.assertIn("unreviewed_url", self.codes(self.scan()))

    def test_loopback_url_still_rejects_embedded_credentials(self):
        endpoint = "http://" + "user:password@localhost:11434"
        self.write("README.md", endpoint)
        self.manifest(["README.md"])
        self.assertIn("credential_url", self.codes(self.scan()))
        self.write("README.md", "http://localhost:11434")
        self.manifest(["README.md"])
        self.assertTrue(self.scan().report()["ok"])

    def test_binary_and_unapproved_extension_rejected(self):
        self.write("sample.py", b"\xff\x00")
        self.write("sample.bin", b"data")
        self.manifest(["sample.py", "sample.bin"])
        self.assertTrue({"non_utf8_or_binary", "unsupported_file_type"}.issubset(self.codes(self.scan())))

    def test_png_hash_structure_metadata_and_trailing_bytes(self):
        self.write("assets/screen.png", tiny_png())
        self.manifest(["assets/screen.png"])
        self.assertTrue(self.scan().report()["ok"])
        self.write("assets/screen.png", tiny_png(png_chunk(b"tEXt", b"Comment\x00opaque")))
        self.manifest(["assets/screen.png"])
        self.assertIn("png_unreviewed_chunk", self.codes(self.scan()))
        self.write("assets/screen.png", tiny_png() + b"tail")
        self.manifest(["assets/screen.png"])
        self.assertIn("png_trailing_or_incomplete", self.codes(self.scan()))
        self.write("assets/screen.png", tiny_png())
        self.manifest(["assets/screen.png"])
        self.write("assets/screen.png", tiny_png() + b"changed")
        self.assertIn("unreviewed_png_hash", self.codes(self.scan()))

    def test_svg_script_and_external_image_rejected(self):
        self.write("assets/a.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>run()</script><image href="data:image/png;base64,AA"/></svg>')
        self.manifest(["assets/a.svg"])
        self.assertIn("svg_active_content", self.codes(self.scan()))

    def test_manifest_escape_duplicate_and_self_hash_rejected(self):
        self.write("publication-manifest.json", json.dumps({"schema_version": 1, "files": [{"path": "../escape.py", "sha256": "0" * 64}]}))
        with self.assertRaises(AUDIT.AuditError):
            self.scan()
        self.write("README.md", "reviewed")
        self.manifest(["README.md", "README.md"])
        with self.assertRaises(AUDIT.AuditError):
            self.scan()
        self.manifest(["publication-manifest.json"])
        with self.assertRaises(AUDIT.AuditError):
            self.scan()

    def test_invalid_policy_and_stale_exception_fail_closed(self):
        self.write("README.md", "reviewed")
        self.manifest(["README.md"], {"README.md": [["not-a-literal"]]})
        with self.assertRaises(AUDIT.AuditError):
            self.scan()
        self.manifest(["README.md"], {"README.md": ["stale-marker"]})
        self.assertIn("unused_literal_exception", self.codes(self.scan()))

    def test_shared_exact_literals_across_reviewed_files_are_allowed(self):
        endpoint = "https://" + "reviewed.invalid/test"
        self.write("one.py", repr(endpoint))
        self.write("two.py", repr(endpoint))
        self.manifest(["one.py", "two.py"], {"one.py": [endpoint], "two.py": [endpoint]})
        self.assertTrue(self.scan().report()["ok"])

    @unittest.skipUnless(shutil.which("git"), "Git is needed for reachable-history checks")
    def test_history_inspects_old_blob_and_private_author_metadata(self):
        def git(*args):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        git("init", "-b", "main")
        self.write("README.md", "OLD" + "_PRIVATE_CONTENT")
        self.manifest(["README.md"])
        git("add", ".")
        git("-c", "user.name=PrivateAuthor", "-c", "user.email=demo@example.org", "commit", "-m", "first")
        self.write("README.md", "clean current content")
        self.manifest(["README.md"])
        git("add", ".")
        git("-c", "user.name=Demo", "-c", "user.email=demo@example.org", "commit", "-m", "second")
        audit = self.scan(["OLD" + "_PRIVATE_CONTENT", "Private" + "Author"])
        self.assertTrue(audit.report()["ok"])
        audit.history()
        self.assertIn("private_deny_match", self.codes(audit))

    @unittest.skipUnless(shutil.which("git"), "Git is needed for staged symlink modes")
    def test_history_rejects_symlink_and_unmapped_blob_ref(self):
        def git(*args, data=None):
            return subprocess.run(["git", "-C", str(self.root), *args], input=data, check=True, capture_output=True).stdout.decode().strip()
        git("init", "-b", "main")
        self.write("README.md", "reviewed")
        self.manifest(["README.md"])
        git("add", ".")
        blob = git("hash-object", "-w", "--stdin", data=b"outside-target")
        git("update-index", "--add", "--cacheinfo", "120000," + blob + ",link.txt")
        git("-c", "user.name=Demo", "-c", "user.email=demo@example.org", "commit", "-m", "snapshot")
        orphan = git("hash-object", "-w", "--stdin", data=b"unmapped")
        git("update-ref", "refs/review/orphan", orphan)
        audit = self.scan()
        audit.history()
        self.assertTrue({"git_nonregular_entry", "git_unexpected_blob"}.issubset(self.codes(audit)))


if __name__ == "__main__":
    unittest.main()
