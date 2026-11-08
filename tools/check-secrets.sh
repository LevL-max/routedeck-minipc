#!/usr/bin/env bash
# Check that every PROFILES_DIR/<name>/secrets.tar.gpg opens with your passphrase.
#
#   bash tools/check-secrets.sh PROFILES_DIR [PLAIN_DIR]
#
# gpg-agent's cached passphrases are cleared first, so you really type it once
# per bundle. With PLAIN_DIR (where the plaintext <name>.tar bundles are), the
# decrypted bundle is also compared byte for byte. The decrypted copy goes to a
# private temp dir and is deleted right away.
# On Windows: Git Bash, or from PowerShell via "C:\Program Files\Git\bin\bash.exe".
set -uo pipefail
GPG=${GPG:-gpg}
[ $# -ge 1 ] || { sed -n '2,10p' "$0"; exit 2; }
OUT=$1
PLAIN=${2:-}
tmp=$(mktemp -d)
chmod 700 "$tmp"
trap 'rm -rf "$tmp"' EXIT
gpgconf --reload gpg-agent >/dev/null 2>&1 || true
rc=0
found=0
for f in "$OUT"/*/secrets.tar.gpg; do
  [ -f "$f" ] || continue
  found=1
  name=$(basename "$(dirname "$f")")
  echo "== $name: type the passphrase"
  if ! "$GPG" --quiet --pinentry-mode loopback --decrypt --output "$tmp/$name.tar" "$f"; then
    echo "   FAILED: wrong passphrase or damaged file"
    rc=1
    continue
  fi
  if ! tar -tf "$tmp/$name.tar" secrets.manifest >/dev/null 2>&1; then
    echo "   FAILED: decrypted, but it is not a secrets bundle"
    rc=1
  elif [ -n "$PLAIN" ] && [ -f "$PLAIN/$name.tar" ]; then
    if cmp -s "$tmp/$name.tar" "$PLAIN/$name.tar"; then
      echo "   OK: opens with this passphrase and matches $PLAIN/$name.tar"
    else
      echo "   FAILED: opens, but differs from $PLAIN/$name.tar"
      rc=1
    fi
  else
    echo "   OK: opens with this passphrase ($(tar -tf "$tmp/$name.tar" | grep -c '^files/.*[^/]$') files)"
  fi
  rm -f "$tmp/$name.tar"
done
[ "$found" = 1 ] || { echo "no */secrets.tar.gpg under $OUT"; exit 1; }
exit $rc
