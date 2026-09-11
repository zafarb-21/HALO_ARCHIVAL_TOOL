# SSH key login for the HALO drone

The HALO archive scripts use SSH and rsync for several independent actions. With
password login, each new connection may request the drone password again. SSH key
login lets the ground computer authenticate non-interactively and avoids those
repeated prompts during mission archive initialization and post-mission collection.

The configured test drone is:

```text
root@192.168.0.20
```

The confirmed passwordless alias `halo-d0012` is also supported anywhere a script
accepts `--drone-host`:

```bash
ssh halo-d0012 "hostname && date"
```

## Recommended setup

Run these commands on the ground computer as the same user who runs the HALO scripts:

```bash
ssh-keygen -t ed25519 -C "halo-archive"
ssh-copy-id root@192.168.0.20
ssh root@192.168.0.20 "hostname && date"
```

The first command creates an Ed25519 key pair. Accept the default key path unless that
would overwrite a key you need. The second command normally asks for the current drone
password once and installs only the public key. The third command verifies login and
remote command execution.

If a separate HALO key filename is preferred, use:

```bash
ssh-keygen -t ed25519 -C "halo-archive" -f "$HOME/.ssh/id_ed25519_halo"
ssh-copy-id -i "$HOME/.ssh/id_ed25519_halo.pub" root@192.168.0.20
ssh -i "$HOME/.ssh/id_ed25519_halo" root@192.168.0.20 "hostname && date"
```

A non-default key may need to be selected with `ssh-agent`, the `-i` option, or an
operator-managed SSH configuration. The HALO Python scripts intentionally do not
generate keys, install public keys, start agents, or edit `~/.ssh/config`.

## What the scripts do

Before a batch of remote actions, each script performs an SSH preflight and prints:

```text
Checking SSH connection to root@192.168.0.20 ...
SSH connection OK: <drone-hostname>
```

If authentication or network access fails, the script exits with a useful error before
starting the batch. Password-based SSH remains supported. The advanced
`--skip-ssh-check` option skips only the preflight; it does not make later SSH or
rsync operations work without valid credentials.

Remote commands use `LC_ALL=C LANG=C` to reduce locale warning noise without changing
the drone's system locale. A warning emitted before the remote command starts may still
appear on some SSH/server configurations, but it is not by itself a collection failure.

## Quick troubleshooting

- Confirm that the ground computer can reach `192.168.0.20`.
- Run `ssh -v root@192.168.0.20` to inspect authentication decisions.
- Check that the selected private key is readable only by its owner.
- Do not overwrite or delete an existing private key while troubleshooting.
- If the drone image or host key changed, review the fingerprint with the responsible
  operator before changing `known_hosts`.
