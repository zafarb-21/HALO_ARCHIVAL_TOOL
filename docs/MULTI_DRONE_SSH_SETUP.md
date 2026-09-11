# Multi-drone SSH setup

The swarm orchestrator uses SSH aliases so each lab drone can be addressed
consistently. Add the following entries to ~/.ssh/config:

    Host halo-d0012
        HostName 192.168.0.20
        User root
        IdentityFile ~/.ssh/halo_archive_ed25519
        IdentitiesOnly yes
        ServerAliveInterval 10

    Host halo-d0013
        HostName 192.168.0.21
        User root
        IdentityFile ~/.ssh/halo_archive_ed25519
        IdentitiesOnly yes
        ServerAliveInterval 10

    Host halo-d0014
        HostName 192.168.0.22
        User root
        IdentityFile ~/.ssh/halo_archive_ed25519
        IdentitiesOnly yes
        ServerAliveInterval 10

    Host halo-d0015
        HostName 192.168.0.23
        User root
        IdentityFile ~/.ssh/halo_archive_ed25519
        IdentitiesOnly yes
        ServerAliveInterval 10

    Host halo-d0016
        HostName 192.168.0.24
        User root
        IdentityFile ~/.ssh/halo_archive_ed25519
        IdentitiesOnly yes
        ServerAliveInterval 10

The existing D0012 key setup can remain in place. Install the same public key on
the four additional known drones:

    ssh-copy-id -i ~/.ssh/halo_archive_ed25519.pub root@192.168.0.21
    ssh-copy-id -i ~/.ssh/halo_archive_ed25519.pub root@192.168.0.22
    ssh-copy-id -i ~/.ssh/halo_archive_ed25519.pub root@192.168.0.23
    ssh-copy-id -i ~/.ssh/halo_archive_ed25519.pub root@192.168.0.24

Protect the SSH configuration and private key:

    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/config
    chmod 600 ~/.ssh/halo_archive_ed25519

Check every alias and compare the UTC clocks before a synchronized test:

    ssh halo-d0012 "hostname && date -u"
    ssh halo-d0013 "hostname && date -u"
    ssh halo-d0014 "hostname && date -u"
    ssh halo-d0015 "hostname && date -u"
    ssh halo-d0016 "hostname && date -u"

The absolute start barrier relies on the drones' system clocks. Correct clock skew
before running a mission. The orchestrator uses noninteractive SSH in swarm mode,
so a missing host key acceptance prompt or password prompt is recorded as a
per-drone failure instead of blocking every drone.

For a future sixth drone, copy drones/DRONE_TEMPLATE.yaml, assign the real drone ID
and IP, add a matching SSH alias, test it, and only then add it to
drones/swarm_lab.yaml. No sixth IP address is reserved by this project.
