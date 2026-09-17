#!/bin/bash
set -euo pipefail
umask 077
backup_root=/var/backups/messagebox-development-access
usage(){ printf '%s\n' 'usage: development-sudo-bootstrap.sh --development-only --operator NAME --backup-id NEW-ID'; }
valid_operator(){
  [[ $1 =~ ^[a-z_][a-z0-9_-]*$ && $1 != root && $1 != messagebox ]] || return 1
  local record uid shell
  record=$(getent passwd "$1") || return 1
  uid=$(printf '%s\n' "$record" | awk -F: '{print $3}'); shell=$(printf '%s\n' "$record" | awk -F: '{print $7}')
  [[ $uid =~ ^[0-9]+$ && $uid -ge 1000 ]] || return 1
  test -x "$shell" && grep -Fxq "$shell" /etc/shells
}
valid_backup_id(){ [[ $1 =~ ^[A-Za-z0-9._-]+$ && $1 != . && $1 != .. ]]; }
policy_path(){ printf '/etc/sudoers.d/90-messagebox-development-%s\n' "$1"; }
render_policy(){
  printf '%s\n' '# Development appliance only. Do not install on production or customer boxes.'
  printf '%s\n' '# SSH public-key authentication remains the login boundary.'
  printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$1"
}
if [[ ${1:-} == --render-policy && $# == 2 ]]; then valid_operator "$2" || exit 2; render_policy "$2"; exit 0; fi
if [[ ${1:-} == --validate-operator && $# == 2 ]]; then valid_operator "$2"; exit; fi
[[ $# == 5 && ${1:-} == --development-only && ${2:-} == --operator && ${4:-} == --backup-id ]] || { usage >&2; exit 2; }
operator=$3; backup_id=$5
valid_operator "$operator" || { printf '%s\n' 'operator must be a named interactive non-root development account' >&2; exit 2; }
valid_backup_id "$backup_id" || { printf '%s\n' 'invalid backup id' >&2; exit 2; }
test "$(id -u)" -eq 0; getent passwd messagebox >/dev/null; getent passwd nobody >/dev/null
id -nG "$operator" | tr ' ' '\n' | grep -qx sudo
test ! -L /etc/sudoers.d; test "$(stat -c '%u:%g' /etc/sudoers.d)" = 0:0
sudoers_mode=$(stat -c '%a' /etc/sudoers.d)
(( (8#$sudoers_mode & 0022) == 0 ))
destination=$(policy_path "$operator"); backup_dir="$backup_root/$backup_id"
test ! -L "$backup_root"; install -d -o root -g root -m 0700 "$backup_root"
test "$(stat -c '%u:%g:%a' "$backup_root")" = 0:0:700
if test -e "$destination" || test -L "$destination"; then
  test -f "$destination"; test ! -L "$destination"; test "$(stat -c '%u:%g:%a' "$destination")" = 0:0:440; temporary=$(mktemp); trap 'rm -f "$temporary"' EXIT
  render_policy "$operator" >"$temporary"
  test "$(sha256sum "$destination" | awk '{print $1}')" = "$(sha256sum "$temporary" | awk '{print $1}')"
  visudo -cf "$destination"; su -s /bin/sh -c 'sudo -k; sudo -n -u root true && sudo -n -u messagebox true && sudo -n -u nobody true' "$operator"
  printf '%s\n' "already configured $destination"; exit 0
fi
test ! -e "$backup_dir"; test ! -L "$backup_dir"; install -d -o root -g root -m 0700 "$backup_dir"
test "$(stat -c '%u:%g:%a' "$backup_dir")" = 0:0:700
installed=0; policy_sha=
on_error(){
  local status=$?; set +e
  printf 'post-install-status=%s\n' "$status" >"$backup_dir/error-status.txt"
  test -n "${temporary:-}" && rm -f "$temporary"
  if [[ $installed == 1 ]] && test -f "$destination" && test ! -L "$destination" && [[ $(sha256sum "$destination" | awk '{print $1}') == "$policy_sha" ]]; then
    rm -f "$destination"; printf '%s\n' 'removed exact newly installed policy after failure' >"$backup_dir/rollback-after-error.txt"
  fi
  visudo -c >"$backup_dir/visudo-error-state.txt" 2>&1 || true; chmod 0600 "$backup_dir"/* 2>/dev/null || true; exit "$status"
}
trap on_error ERR
{ date -Is; printf 'operator=%s\npolicy=%s\nprevious-state=absent\n' "$operator" "$destination"; sha256sum /etc/sudoers; } >"$backup_dir/preinstall-metadata.txt"
sudo -l -U "$operator" 2>&1 | cat >"$backup_dir/preexisting-sudo-list.txt"
visudo -c >"$backup_dir/visudo-before.txt" 2>&1
temporary=$(mktemp /etc/sudoers.d/.90-messagebox-development.XXXXXX)
render_policy "$operator" >"$temporary"; chown root:root "$temporary"; chmod 0440 "$temporary"; visudo -cf "$temporary"
policy_sha=$(sha256sum "$temporary" | awk '{print $1}'); mv -f "$temporary" "$destination"; installed=1
printf '%s  %s\n' "$policy_sha" "$destination" >"$backup_dir/installed-policy-sha256.txt"
visudo -cf "$destination"; su -s /bin/sh -c 'sudo -k; sudo -n -u root true && sudo -n -u messagebox true && sudo -n -u nobody true' "$operator"
visudo -c >"$backup_dir/visudo-after.txt" 2>&1; chmod 0600 "$backup_dir"/*; trap - ERR
printf '%s\n' "installed $destination"
printf '%s\n' "rollback backup id: $backup_id"
