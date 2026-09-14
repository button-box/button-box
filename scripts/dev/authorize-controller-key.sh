#!/bin/sh
# Bootstrap one authorized controller key on a development/test Button Box.
set -eu

usage() {
  printf '%s\n' \
    "Usage: $0 user@host [public-key-file]" \
    "Default public key: ~/.ssh/id_ed25519.pub" >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

case "$#" in
  1) TARGET=$1; PUBLIC_KEY=${HOME}/.ssh/id_ed25519.pub ;;
  2) TARGET=$1; PUBLIC_KEY=$2 ;;
  *) usage; exit 2 ;;
esac

case "$TARGET" in
  root@*|-*|@*|*@|*[!A-Za-z0-9._@-]*|*@*@*)
    die "invalid non-root SSH target: $TARGET"
    ;;
  *@*) ;;
  *) die "SSH target must be in user@host form" ;;
esac

command -v ssh >/dev/null 2>&1 || die "ssh is required"
command -v ssh-copy-id >/dev/null 2>&1 || die "ssh-copy-id is required"
command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is required"
[ -f "$PUBLIC_KEY" ] && [ ! -L "$PUBLIC_KEY" ] && [ -r "$PUBLIC_KEY" ] ||
  die "public key is not a readable regular file: $PUBLIC_KEY"

FINGERPRINT=$(ssh-keygen -lf "$PUBLIC_KEY" | awk 'NR == 1 { print $2 }')
case "$FINGERPRINT" in
  SHA256:*) ;;
  *) die "could not determine an SHA256 public-key fingerprint" ;;
esac

printf '%s\n' \
  "Authorizing controller key $FINGERPRINT on $TARGET." \
  "The existing account password may be requested once."
ssh-copy-id -i "$PUBLIC_KEY" -- "$TARGET"

ssh_batch() {
  ssh -o BatchMode=yes -o PasswordAuthentication=no -o ConnectTimeout=5 "$@"
}

ssh_batch "$TARGET" true ||
  die "public-key login verification failed"

if ssh_batch "$TARGET" sudo -n true; then
  PRIVILEGE_STATUS="verified: sudo -n succeeds"
else
  PRIVILEGE_STATUS="not ready: SSH works, but sudo still requires interaction"
fi

cat <<EOF

CONTROLLER ACCESS VERIFIED
Target: $TARGET
Public-key fingerprint: $FINGERPRINT
SSH authentication: verified noninteractive public key
Deployment privilege: $PRIVILEGE_STATUS

Record the target, administrator, fingerprint, and recovery controller in the
private Unit/run record. Never record the private key. Keep a second authorized
controller or local console recovery path, and rerun this check after restart or
reprovisioning.
EOF

[ "$PRIVILEGE_STATUS" = "verified: sudo -n succeeds" ] || exit 3
