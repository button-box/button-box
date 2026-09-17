#!/bin/bash
set -euo pipefail
umask 077
backup_root=/var/backups/messagebox-development-access
[[ $# == 5 && ${1:-} == --development-only && ${2:-} == --operator && ${4:-} == --backup-id ]] || exit 2
operator=$3; backup_id=$5
valid_backup_id(){ [[ $1 =~ ^[A-Za-z0-9._-]+$ && $1 != . && $1 != .. ]]; }
[[ $operator =~ ^[a-z_][a-z0-9_-]*$ && $operator != root && $operator != messagebox ]]
valid_backup_id "$backup_id"
test "$(id -u)" -eq 0; test ! -L "$backup_root"; test "$(stat -c '%u:%g:%a' "$backup_root")" = 0:0:700
backup_dir="$backup_root/$backup_id"; test -d "$backup_dir"; test ! -L "$backup_dir"; test "$(stat -c '%u:%g:%a' "$backup_dir")" = 0:0:700
test -f "$backup_dir/installed-policy-sha256.txt"
destination="/etc/sudoers.d/90-messagebox-development-$operator"; test -f "$destination"; test ! -L "$destination"
expected_sha=$(awk 'NR == 1 {print $1}' "$backup_dir/installed-policy-sha256.txt"); [[ $expected_sha =~ ^[a-f0-9]{64}$ ]]
test "$(sha256sum "$destination" | awk '{print $1}')" = "$expected_sha"
rm -f "$destination"; visudo -c >"$backup_dir/visudo-after-rollback.txt" 2>&1; chmod 0600 "$backup_dir/visudo-after-rollback.txt"
printf '%s\n' "removed $destination"
