#!/usr/bin/env bash
# Encrypt plaintext secrets bundles (made by tools/import_snapshot.py) into
# PROFILES_DIR/<name>/secrets.tar.gpg - next to that host's host.env, in your
# PRIVATE profiles repository. You type the passphrase yourself, twice per file;
# keep it in your password manager.
#
#   bash tools/encrypt-secrets.sh PROFILES_DIR /path/pc2.tar [/path/pc1.tar ...]
#
# Format: OpenPGP symmetric (RFC 4880): AES-256, iterated+salted SHA-512 S2K,
# MDC integrity check. gpg 2.2 on Ubuntu 22.04 decrypts it; install.sh does that.
# On Windows: Git Bash, or from PowerShell via "C:\Program Files\Git\bin\bash.exe".
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GPG=${GPG:-gpg}
[ $# -ge 2 ] || { sed -n '2,11p' "$0"; exit 2; }
command -v "$GPG" >/dev/null || { echo "gpg not found" >&2; exit 1; }
OUT="$(cd "$1" && pwd)"; shift
case "$OUT/" in "$REPO"/*) echo "refusing: $OUT is inside this public repository" >&2; exit 1 ;; esac
for src in "$@"; do
  [ -f "$src" ] || { echo "no such file: $src" >&2; exit 1; }
  dir="$(cd "$(dirname "$src")" && pwd)/"
  case "$dir" in "$REPO"/*|"$OUT"/*) echo "refusing: keep plaintext $src outside both repositories" >&2; exit 1 ;; esac
  name=$(basename "$src" .tar)
  [ -f "$OUT/$name/host.env" ] || { echo "no profile $OUT/$name/host.env for $src" >&2; exit 1; }
  out="$OUT/$name/secrets.tar.gpg"
  echo "== $name: encrypting $(wc -c < "$src") bytes -> $out"
  rm -f "$out.tmp"
  "$GPG" --pinentry-mode loopback --rfc4880 --cipher-algo AES256 --digest-algo SHA512 \
    --s2k-digest-algo SHA512 --s2k-mode 3 --s2k-count 65011712 --compress-algo none \
    --symmetric --output "$out.tmp" "$src"
  # Packet check without the passphrase: the header must say AES-256 (cipher 9), no AEAD.
  packets=$("$GPG" --list-packets --batch --pinentry-mode loopback --passphrase '' "$out.tmp" 2>&1 || true)
  case "$packets" in
    *'symkey enc packet: version 4, cipher 9, aead 0'*) ;;
    *) echo "unexpected packet format in $out.tmp" >&2; exit 1 ;;
  esac
  mv "$out.tmp" "$out"
  echo "   ok: $(wc -c < "$out") bytes, sha256 $(sha256sum "$out" | cut -c1-16)..."
done
echo "Done. Commit the secrets.tar.gpg files in the private profiles repo; never commit plaintext .tar files."
