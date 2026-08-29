# Remote support with Tailscale

Tailscale can give a shipped Button Box a private route back to its operator.
It does not make an offline box reachable and it does not replace OpenSSH keys,
local recovery, backups, or tested application releases.

Remote support is optional. Enrolling a household box gives authorized tailnet
members a path to its SSH service, which can expose private device state. Get
the household's consent, restrict tailnet membership and access rules, and do
not expose the dashboard or any service to the public internet. The Button Box
adds no heartbeat, but the Tailscale client itself exchanges connection and
diagnostic metadata with Tailscale's control plane; include that in the
household's consent decision.

## Provision before shipping

Prepare ordinary OpenSSH access first. The administrator must be non-root,
must have the operator's public key in `~/.ssh/authorized_keys`, and must have
passwordless `sudo` for this unattended support model. Protect the operator's
private key with a passphrase and keep a second authorized computer or recovery
key available.

From a clean, reviewed repository checkout on a computer that can currently
reach the Pi over LAN or Ethernet, run:

```sh
./scripts/provision-tailscale.sh admin@message-box-001.local
```

The helper:

1. Refuses root, malformed targets, and non-`message-box-*` devices.
2. Installs Tailscale from its signed official Debian 13 repository.
3. Opens Tailscale's interactive browser authorization when the device is not
   already enrolled.
4. Leaves Tailscale SSH, Serve, Funnel, subnet routing, and exit-node features
   disabled.
5. Holds the package so upgrades happen during an attended support session.

No reusable Tailscale auth key is accepted or stored. The device joins with the
same hostname by default. Use `--hostname NAME` only when the tailnet needs a
different, non-sensitive device name.

Keep the LAN session open. Move the operator computer to a genuinely different
network, such as a phone hotspot, then use the address printed by the helper:

```sh
ssh admin@100.x.y.z
```

That is ordinary OpenSSH carried over Tailscale. `tailscale ssh` is deliberately
not used, so every support computer still needs an authorized OpenSSH key.
Confirm both the remote path and the original LAN fallback before shipping.

By default, Tailscale device keys can expire and an expired remote box stops
accepting tailnet connections. For this trusted, unattended device, open its
entry on the **Machines** page and deliberately choose **Disable key expiry**.
This reduces security: revoke or remove the device immediately if the box is
lost, replaced, or no longer supported. Do not use
`tailscale up --force-reauth` over the only remote connection.

## Tailnet controls

Use the Tailscale admin console to verify that the device belongs to the
intended family-only tailnet. Give support access only to the operator accounts
that need it, require the tailnet's normal account security, and remove stale
devices and members promptly. Do not paste node addresses, authorization URLs,
account names, or access-rule details into public issues or logs.

Before shipping, confirm the machine is online, key expiry has the intended
setting, Tailscale SSH is off, it advertises no routes or exit-node capability,
and client auto-update is off. Recheck those controls after any membership or
policy change.

This repository does not install Tailscale access rules because the public code
must not contain private tailnet identity or policy. Review those controls in
the tailnet before shipping and after any membership change.

## On-demand support

The box sends no heartbeat or telemetry through this setup. During a support
session, connect over SSH and start with bounded checks:

```sh
tailscale status --self
messageboxctl services
messageboxctl status
systemctl --failed
df -h /
```

Read only the logs needed for the incident. Before sharing output, remove phone
numbers, WhatsApp identifiers, Wi-Fi details, NFC identifiers, recordings,
private addresses, and authentication state.

Application updates remain deliberate deployments from a reviewed commit:

```sh
git switch --detach <reviewed-commit>
./scripts/provision.sh admin@100.x.y.z
```

Record the previously installed commit and confirm there is enough disk space
before deploying. Do not combine a remote application update with an OS update
unless both changes have already passed physical testing and a rollback path is
available.

For an attended Tailscale package update:

```sh
ssh admin@100.x.y.z
sudo apt-mark unhold tailscale
sudo apt-get update
sudo apt-get install --only-upgrade tailscale
sudo apt-mark hold tailscale
tailscale version
tailscale status --self
```

Keep the SSH session open until a second connection succeeds. Apply Raspberry
Pi OS updates separately, in small batches, while a local helper is available.

## Recovery and removal

Tailscale cannot recover loss of power, failed storage, broken home internet,
or a network change that the Pi cannot join. Before shipping, make sure someone
at the household can:

- power-cycle the box;
- repeat phone-based Wi-Fi onboarding;
- connect Ethernet to the router when requested.

If remote access fails, try the local hostname or router address over Ethernet.
Do not reset application data merely to repair the network.

To remove remote support while connected locally:

```sh
sudo tailscale logout
sudo systemctl disable --now tailscaled
sudo apt-mark unhold tailscale
sudo apt-get purge tailscale
sudo rm -f /etc/apt/sources.list.d/tailscale.list
sudo rm -f /usr/share/keyrings/tailscale-archive-keyring.gpg
```

Also delete or expire the device in the Tailscale admin console. Removing
Tailscale does not remove OpenSSH keys; review `~/.ssh/authorized_keys`
separately when transferring or retiring a box.

## Tailscale references

- [Linux installation](https://tailscale.com/docs/install/linux)
- [CLI reference](https://tailscale.com/kb/1080/cli)
- [Device key expiry](https://tailscale.com/docs/features/access-control/key-expiry)
- [Standard SSH compared with Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh)
