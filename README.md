# homelab-ansible

Ansible for the homelab. Right now it does one job: take a Debian or Ubuntu box
and guarantee it has Docker Engine, the Compose v2 plugin, and an unprivileged
`appuser` identity for containers to run as.

It is written to be safe to run against the box **as it is today**, with Immich,
Paperless and SilverBullet already serving traffic. See
[Running against a live host](#running-against-a-live-host) before the first run.

## Layout

```
ansible.cfg              inventory path, ssh tuning, yaml output
requirements.yml         galaxy collections (installed into ./collections, gitignored)
Makefile                 make ping / check / bootstrap / lint
inventory/
  hosts.yml              the machines
  group_vars/all.yml     ansible_user, filesystem convention
playbooks/
  bootstrap.yml          python3 -> docker -> appuser
roles/
  docker/                install from Docker's apt repo, then verify it works
  appuser/               the service identity, uid/gid pinned
stacks/                  compose files, one directory per app -- see stacks/README.md
```

## Requirements

**Control node** (this Mac): `ansible-core >= 2.15` — the roles use
`systemd_service` and `password_lock`. Install it into a venv rather than
system Python:

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install ansible-core ansible-lint
make deps
```

**Target host**: Debian or Ubuntu, SSH reachable, and an account with sudo. The
playbook installs `python3` itself if the host does not have it, so a minimal
netinstall is fine.

## Quick start

```sh
$EDITOR inventory/hosts.yml          # real hostname and IP
$EDITOR inventory/group_vars/all.yml # confirm ansible_user

make ping        # SSH + sudo both work?
make check       # dry run: shows exactly what would change
make bootstrap   # apply
```

`make check` on an unbootstrapped host will report the apt tasks as changes it
would make; that is the expected output, not a problem.

## What the bootstrap actually does

| | |
|---|---|
| Installs | `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin` from `download.docker.com` |
| Adds | `/etc/apt/keyrings/docker.asc` and `/etc/apt/sources.list.d/docker.list` |
| Creates | group `appuser` (gid 2000), user `appuser` (uid 2000), shell `/usr/sbin/nologin`, locked password, member of `docker` |
| Enables | `docker.service`, started and enabled at boot |
| Verifies | `docker --version` and `docker compose version` both respond and clear the configured minimums |

It does **not** touch `/opt`, `/srv`, any existing user, or any running
container.

## Running against a live host

Three things are worth knowing before you point this at the box that is
currently serving your photos.

**Docker is already installed there, so the apt tasks are no-ops.** The value of
the first run is the verification and the appuser, not the install.

**Daemon config is opt-in and starts off.** `docker_manage_daemon_config`
defaults to `false`. When you turn it on it writes `/etc/docker/daemon.json`
with log rotation and `live-restore`, and that requires restarting dockerd.
`live-restore` only protects container uptime once it is *already* active, so
the restart that enables it is the one outage it cannot prevent — a few seconds
where containers stop and come back. Do it in a window you choose:

```yaml
# inventory/group_vars/homelab.yml
docker_manage_daemon_config: true
```

Worth doing soon, though. `json-file` with no `max-size` is the standard way a
homelab fills its disk overnight and takes every service down with it. Note that
log options apply to containers created *after* the change; existing ones keep
their current settings until recreated.

**Conflicting-package removal is off.** `docker_remove_conflicting_packages`
defaults to `false`. Docker's install guide opens by purging `docker.io` and
friends — correct on a fresh machine, and on this one it would stop every
running container. Only flip it for a genuinely new host.

## appuser and the uid-1000 question

Your running stacks are owned by uid 1000 (`maboukerfa`) — SilverBullet pins
`user: "1000:1000"` in its compose file. `appuser` is pinned to **2000:2000**
specifically to stay out of their way. Nothing in the `appuser` role chowns
anything, so Immich, Paperless and SilverBullet keep working untouched.

That leaves two owners on the box for now, which is fine. New stacks use
`2000:2000`; existing ones move when you decide they should, one at a time:

```sh
docker compose -f /opt/<app>/compose.yaml down
chown -R 2000:2000 /srv/<app>
# change `user:` / PUID / PGID in the compose file to 2000:2000
docker compose -f /opt/<app>/compose.yaml up -d
```

The uid is pinned rather than auto-allocated because bind mounts carry numeric
ids, not names. If the host is rebuilt and `useradd` hands out 1001 next time,
every file under `/srv` is owned by a stranger and containers start failing in
ways that look like application bugs.

### appuser cannot log in

`appuser` is a pure ownership identity: shell `/usr/sbin/nologin`, password
locked, no `~/.ssh` and no way to add one through this role. Nothing needs a
login — containers run as `2000:2000`, which is a uid the kernel checks and
which requires no host shell whatsoever, and systemd runs `ExecStart` directly
without one either.

The one thing to remember, because it will catch you out once:

```sh
sudo -u appuser docker compose ps    # works: sudo execs the command
sudo -u appuser -i                   # fails: -i asks for a login shell
su - appuser                         # fails: same reason
```

A home directory still exists at `/home/appuser` (mode 0750). That is not an
oversight — anything running as appuser resolves `$HOME`, and the docker CLI
writes `~/.docker/config.json` while restic caches under `~/.cache`.

**On the `docker` group:** `appuser` is in it, which is root-equivalent — that
group lets you start a container bind-mounting `/` and step out as root. With
nologin, the only thing that can still use it is a systemd unit declaring
`User=appuser` that shells out to `docker compose`. If Ansible does all your
deploying it does so as root, in which case nothing needs the group and
`appuser_in_docker_group: false` removes the root-equivalence outright. Your
existing `paperless-backup.service` already runs as `User=root` for exactly
this reason, so `false` may well be the honest setting here.

## Secrets

Nothing here needs a vault yet. When it does — restic passwords, API tokens —
use `ansible-vault` and keep the password file out of the repo; `.gitignore`
already excludes `.vault_pass`. Encrypted vault files themselves are safe to
commit and belong in the repo.

Per-stack `.env` files stay on the host and are gitignored; commit
`.env.example` next to each compose file instead.

## Next

The obvious next role is `stack`: render `stacks/<name>/compose.yaml` to
`/opt/<name>/`, create `/srv/<name>/` owned by appuser, and bring it up with
`community.docker.docker_compose_v2` (already pinned in `requirements.yml`).
After that, folding in the restic backup scripts and systemd timers that
currently live beside this directory.
