"""Check a reviewed publication snapshot; this is not an anonymity guarantee.

Manifest schema_version=1 has files=[{path, sha256}] and an optional
literal_exceptions={relative_path: [exact_reviewed_literal, ...]} mapping.
The manifest itself is automatically allowed, without an impossible self-hash.
PNG files still require human visual review after this structural check.
Only finding categories, relative paths and line numbers are reported.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zlib


TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".svg", ".html", ".css", ".js", ".txt", ".toml"}
SPECIAL_TEXT = {".gitignore", ".gitattributes"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"sRGB", b"gAMA", b"cHRM", b"pHYs", b"sBIT", b"bKGD", b"hIST"}
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PHONE = re.compile(r"(?<![A-Za-z0-9])\+?\d[\d ()-]{8,}\d(?![A-Za-z0-9])")
# Markdown's ]( separates label and target. A closing bracket belongs to
# the token only for the opening IPv6 authority, never for a Markdown label.
URL = re.compile(r"https?://(?:\[[^\]\s<>\"'`()]*\][^\]\s<>\"'`()]*|[^\]\s<>\"'`()]+)", re.I)
SECRETS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA[A-Z0-9]{16}|LTAI[A-Za-z0-9]{12,})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{12,}"),
]
ABSOLUTE_PATH = re.compile(r"(?i)(?:\b[A-Z]:[\\/](?:Users|UserData|Windows|Program Files)\b|(?<!\w)/(?:home|Users)/[^\s<>\"']+)")
NOREPLY_EMAIL = re.compile(r"(?:\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com\Z", re.I)


class AuditError(ValueError):
    """Configuration errors use fixed codes rather than raw input values."""


def valid_path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(char) < 32 for char in value):
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and str(path) == value and all(part not in {".", "..", ".git"} for part in path.parts)
            and not re.match(r"^[A-Za-z]:", value))


def load_manifest(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise AuditError("invalid_manifest") from None
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("files"), list):
        raise AuditError("invalid_manifest")
    files, folded = {}, set()
    for entry in value["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise AuditError("invalid_manifest_entry")
        name, digest = entry["path"], entry["sha256"]
        if not valid_path(name) or name.casefold() in folded or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AuditError("invalid_manifest_entry")
        files[name] = digest
        folded.add(name.casefold())
    exceptions = value.get("literal_exceptions", {})
    if not isinstance(exceptions, dict):
        raise AuditError("invalid_literal_exceptions")
    for name, literals in exceptions.items():
        if name not in files or not isinstance(literals, list) or not literals:
            raise AuditError("invalid_literal_exceptions")
        if any(not isinstance(literal, str) or not literal or len(literal) > 256 or any(c in literal for c in "\r\n\x00") for literal in literals):
            raise AuditError("invalid_literal_exceptions")
        if len(set(literals)) != len(literals):
            raise AuditError("invalid_literal_exceptions")
    return files, exceptions


def load_deny(path, root):
    if path is None:
        return []
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise AuditError("deny_file_must_stay_outside_package")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise AuditError("invalid_deny_file") from None
    if isinstance(value, dict):
        value = value.get("terms")
    if not isinstance(value, list) or any(not isinstance(term, str) or not term.strip() for term in value):
        raise AuditError("invalid_deny_file")
    return value


def png_errors(raw):
    """Validate chunks/CRC, forbid textual/profile metadata and trailing payload."""
    if not raw.startswith(PNG_SIGNATURE):
        return ["invalid_png"]
    offset, seen_header, seen_data = len(PNG_SIGNATURE), False, False
    while offset < len(raw):
        if offset + 12 > len(raw):
            return ["invalid_png"]
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(raw) or size > 64 * 1024 * 1024 or kind not in PNG_CHUNKS:
            return ["png_unreviewed_chunk"]
        payload = raw[offset + 8:offset + 8 + size]
        checksum = struct.unpack(">I", raw[offset + 8 + size:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != checksum:
            return ["invalid_png_crc"]
        if not seen_header:
            if kind != b"IHDR" or size != 13:
                return ["invalid_png"]
            width, height = struct.unpack(">II", payload[:8])
            if not width or not height or width * height > 40_000_000:
                return ["png_dimensions_out_of_scope"]
            seen_header = True
        elif kind == b"IHDR":
            return ["invalid_png"]
        if kind == b"IDAT":
            seen_data = True
        if kind == b"IEND":
            return [] if size == 0 and seen_data and end == len(raw) else ["png_trailing_or_incomplete"]
        offset = end
    return ["invalid_png"]


class Auditor:
    def __init__(self, root, manifest="publication-manifest.json", deny_terms=None):
        self.root = Path(root).resolve()
        if not valid_path(manifest):
            raise AuditError("invalid_manifest_path")
        self.manifest_name = manifest
        self.files, self.exceptions = load_manifest(self.root / manifest)
        if manifest in self.files:
            raise AuditError("manifest_must_not_hash_itself")
        self.deny = list(deny_terms or [])
        self.findings = []
        self._seen = set()
        self.allowed = set(self.files) | {manifest}

    def add(self, code, path, line=None):
        safe_path = str(path)
        for term in self.deny:
            safe_path = re.sub(re.escape(term), "[redacted]", safe_path, flags=re.I)
        item = {"code": code, "path": safe_path}
        if line:
            item["line"] = line
        key = tuple(item.items())
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(item)

    def text(self, path, original, exceptions=None, check_unused=True):
        folded = original.casefold()
        if any(term.casefold() in folded for term in self.deny):
            self.add("private_deny_match", path)
        text = original
        for literal in exceptions or []:
            if literal not in text and check_unused:
                self.add("unused_literal_exception", path)
            text = text.replace(literal, " " * len(literal))
        for match in EMAIL.finditer(text):
            domain = match.group().rsplit("@", 1)[1].lower()
            if domain not in {"example.com", "example.org", "example.net"} and not NOREPLY_EMAIL.fullmatch(match.group()):
                self.add("non_example_email", path, text.count("\n", 0, match.start()) + 1)
        for match in IPV4.finditer(text):
            try:
                address = ipaddress.IPv4Address(match.group())
            except ipaddress.AddressValueError:
                continue
            if str(address) != "127.0.0.1":
                self.add("non_loopback_ipv4", path, text.count("\n", 0, match.start()) + 1)
        for match in PHONE.finditer(text):
            if 10 <= sum(char.isdigit() for char in match.group()) <= 15:
                self.add("phone_like_value", path, text.count("\n", 0, match.start()) + 1)
        for pattern in SECRETS:
            for match in pattern.finditer(text):
                self.add("secret_like_value", path, text.count("\n", 0, match.start()) + 1)
        for match in ABSOLUTE_PATH.finditer(text):
            self.add("local_absolute_path", path, text.count("\n", 0, match.start()) + 1)
        for match in URL.finditer(text):
            raw_url = match.group().rstrip(").,;")
            credentials = False
            try:
                parsed = urllib.parse.urlsplit(raw_url)
                host = parsed.hostname
                parsed.port
                credentials = parsed.username is not None or parsed.password is not None
            except ValueError:
                host = None
            if credentials:
                self.add("credential_url", path, text.count("\n", 0, match.start()) + 1)
            if host not in {"localhost", "127.0.0.1", "::1", "github.com", "www.w3.org"}:
                self.add("unreviewed_url", path, text.count("\n", 0, match.start()) + 1)

    def content(self, path, raw, *, expected=None, history=False):
        if any(term.casefold() in path.casefold() for term in self.deny):
            self.add("private_deny_in_path", path)
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            self.add("hash_mismatch", path)
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".png":
            if not path.startswith("assets/") or path not in self.files:
                self.add("unapproved_png_path", path)
            if hashlib.sha256(raw).hexdigest() != self.files.get(path):
                self.add("unreviewed_png_hash", path)
            for code in png_errors(raw):
                self.add(code, path)
            return
        if suffix not in TEXT_SUFFIXES and path not in SPECIAL_TEXT:
            self.add("unsupported_file_type", path)
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            self.add("non_utf8_or_binary", path)
            return
        if "\x00" in text:
            self.add("non_utf8_or_binary", path)
        exceptions = self.exceptions.get(path, [])
        if path == self.manifest_name:
            # Policy literals are already reviewed file-specific values. Private
            # deny matching still runs before these exact values are masked.
            exceptions = list(dict.fromkeys(literal for literals in self.exceptions.values() for literal in literals))
        self.text(path, text, exceptions, check_unused=not history)
        if suffix == ".svg":
            try:
                tree = ET.fromstring(text)
                for node in tree.iter():
                    if node.tag.rsplit("}", 1)[-1].lower() in {"script", "foreignobject"}:
                        self.add("svg_active_content", path)
                    for key, value in node.attrib.items():
                        local_key = key.rsplit("}", 1)[-1].lower()
                        if local_key.startswith("on") or (local_key in {"href", "src"} and not value.startswith("#")):
                            self.add("svg_active_content", path)
            except ET.ParseError:
                self.add("invalid_svg", path)

    def git(self, *args, binary=False):
        try:
            result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            raise AuditError("git_inspection_failed") from None
        return result.stdout if binary else result.stdout.decode("utf-8", errors="strict")

    def ignored(self, name):
        if (self.root / ".git").exists():
            try:
                result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--quiet", "--", name], capture_output=True)
                if result.returncode in {0, 1}:
                    return result.returncode == 0
            except OSError:
                pass
        try:
            rules = (self.root / ".gitignore").read_text(encoding="utf-8").splitlines()
        except OSError:
            rules = []
        for rule in rules:
            rule = rule.strip()
            if not rule or rule.startswith(("#", "!")):
                continue
            if rule.endswith("/"):
                if rule.rstrip("/") in PurePosixPath(name).parts:
                    return True
            elif fnmatch.fnmatch(name, rule) or fnmatch.fnmatch(PurePosixPath(name).name, rule):
                return True
        return False

    def worktree(self):
        seen = set()
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in list(dirs):
                full = Path(directory) / name
                relative = full.relative_to(self.root).as_posix()
                if name == ".git":
                    dirs.remove(name)
                elif full.is_symlink():
                    if not self.ignored(relative):
                        self.add("symlink_not_allowed", relative)
                    dirs.remove(name)
                elif self.ignored(relative + "/"):
                    dirs.remove(name)
            for name in files:
                if name == ".git":
                    continue
                full = Path(directory) / name
                relative = full.relative_to(self.root).as_posix()
                if relative not in self.allowed and self.ignored(relative):
                    continue
                seen.add(relative)
                if full.is_symlink():
                    self.add("symlink_not_allowed", relative)
                    continue
                if relative not in self.allowed:
                    self.add("unexpected_file", relative)
                try:
                    self.content(relative, full.read_bytes(), expected=self.files.get(relative))
                except OSError:
                    self.add("unreadable_file", relative)
        for name in self.allowed - seen:
            self.add("missing_file", name)

    def history(self):
        # The current allowlist also bounds older reachable trees. Removed files
        # remain findings; an older PNG needs explicit review in this snapshot.
        commits = self.git("rev-list", "--all").splitlines()
        for commit in commits:
            metadata = self.git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit)
            self.text("git-metadata/" + commit, metadata)
            tree = self.git("ls-tree", "-r", "-z", "--full-tree", commit, binary=True)
            for row in tree.split(b"\x00"):
                if not row:
                    continue
                header, raw_name = row.split(b"\t", 1)
                mode, kind, object_id = header.decode("ascii").split()
                name = raw_name.decode("utf-8", errors="strict")
                if mode not in {"100644", "100755"} or kind != "blob":
                    self.add("git_nonregular_entry", name)
                if name not in self.allowed:
                    self.add("git_unexpected_path", name)
        objects = self.git("rev-list", "--objects", "--all").splitlines()
        for row in objects:
            object_id, _, name = row.partition(" ")
            if self.git("cat-file", "-t", object_id).strip() != "blob":
                continue
            if not name or name not in self.allowed:
                self.add("git_unexpected_blob", name or "git-unmapped-blob/" + object_id)
                # Still scan unapproved text blobs; never print their contents.
            safe_name = name or "unmapped.txt"
            self.content(safe_name, self.git("cat-file", "blob", object_id, binary=True), history=True)

    def report(self):
        return {"ok": not self.findings, "scope": "reviewed_snapshot_and_requested_history",
                "manifest_files": len(self.files), "findings": self.findings,
                "limitations": ["PNG pixels require human review", "Pattern checks do not guarantee anonymity or rights clearance"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", default="publication-manifest.json")
    parser.add_argument("--deny-file", type=Path)
    parser.add_argument("--git-history", action="store_true")
    args = parser.parse_args(argv)
    try:
        auditor = Auditor(args.root, args.manifest, load_deny(args.deny_file, args.root))
        auditor.worktree()
        if args.git_history:
            auditor.history()
        report = auditor.report()
    except (AuditError, UnicodeError) as error:
        code = str(error) if isinstance(error, AuditError) else "invalid_git_text_encoding"
        report = {"ok": False, "findings": [{"code": code}]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
