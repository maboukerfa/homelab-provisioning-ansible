# homelab-ansible

Ansible for the homelab. Two jobs so far: bootstrap a Debian or Ubuntu box with
Docker Engine, the Compose v2 plugin and an unprivileged `appuser` identity, and
deploy compose stacks from `stacks/` onto it.

Written for a **fresh host**. Everything is idempotent, so re-running it is
cheap and boring, but two defaults assume nothing is serving traffic yet — see
[Defaults that assume a fresh host](#defaults-that-assume-a-fresh-host).

## Layout

```
ansible.cfg              inventory path, ssh tuning, yaml output
requirements.yml         galaxy collections (installed into ./collections, gitignored)
Makefile                 make ping / check / bootstrap / stack / lint
inventory/
  hosts.yml              the machines
  group_vars/all.yml     ansible_user, filesystem convention
playbooks/
  bootstrap.yml          python3 -> docker -> appuser
  silverbullet.yml       deploy one stack
roles/
  docker/                install from Docker's apt repo, then verify it works
  appuser/               the service identity, uid/gid pinned
  stack/                 deploy one compose stack, with preflight checks
stacks/                  compose files, one directory per app -- see stacks/README.md
  silverbullet/
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

ssh <user>@<host> true               # accept the host key -- see below

make ping        # SSH + sudo both work?
make bootstrap   # apply
```

**The admin account needs NOPASSWD sudo.** `become_ask_pass` is `False`, so
nothing prompts. On the host:

```sh
printf '<user> ALL=(ALL) NOPASSWD: ALL\n' > /tmp/nopasswd
sudo visudo -cf /tmp/nopasswd                                        # "parsed OK"
sudo install -o root -g root -m 0440 /tmp/nopasswd /etc/sudoers.d/99-<user>
rm /tmp/nopasswd && sudo -n true && echo "NOPASSWD works"
```

Validate before installing: a syntax error under `/etc/sudoers.d/` breaks all
sudo the moment the file lands. The file must be `0440 root:root`, and its name
must contain no dots — sudo silently ignores it otherwise.

This is not only convenience. With `become_ask_pass = True`, Ansible passes sudo
a randomised marker via `-p` and waits for it before sending the password.
sudo-rs — the Rust sudo Ubuntu ships by default from 25.10 — ignores `-p`, so
the marker never arrives and every task fails with `Timeout waiting for
privilege escalation prompt` even though SSH and sudo are both healthy. NOPASSWD
removes that handshake entirely.

**The first connection has to be made by hand.** `host_key_checking = True`, so
Ansible refuses a host whose key it has never seen; one manual `ssh` records it.

Extra flags reach any target through `ARGS`, e.g. `make bootstrap ARGS=-vv`,
`ARGS="--tags docker"`, `ARGS="--limit homelab01"`.

`make check` is worth it on a host that has already been bootstrapped, where a
clean re-run should report `changed=0`. On a bare one it cannot tell you much:
check mode changes nothing, so later tasks inspect state that earlier tasks
would have created, and the docker service task has no `docker.service` to look
at. Expect noise there. The version assertions skip themselves in check mode for
the same reason — nothing was installed to assert against.

On a VM, a snapshot beats a dry run anyway.

## What the bootstrap does

| | |
|---|---|
| Installs | `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin` from `download.docker.com` |
| Adds | `/etc/apt/keyrings/docker.asc` and `/etc/apt/sources.list.d/docker.list` |
| Removes | the distro's `docker.io` / `docker-compose` / `containerd` packages, per Docker's install guide |
| Writes | `/etc/docker/daemon.json` — log rotation and `live-restore` |
| Creates | group `appuser` (gid 2000), user `appuser` (uid 2000), shell `/usr/sbin/nologin`, locked password, member of `docker` |
| Enables | `docker.service`, started and enabled at boot |
| Verifies | `docker --version` and `docker compose version` both respond and clear the configured minimums |

That last row is the one that earns its place. `state: present` succeeding is not
the same as Docker working: a compose plugin that never landed, or a `docker.io`
still shadowing `$PATH`, both produce a green playbook run and a broken host.

## Defaults that assume a fresh host

Both live in `roles/docker/defaults/main.yml` and both are safe now and
disruptive later. Flip them in `inventory/group_vars/homelab.yml` before
pointing this at a machine that is already serving traffic.

**`docker_manage_daemon_config: true`** writes `/etc/docker/daemon.json` and
restarts dockerd. Free on an empty host; seconds of downtime on a busy one.
Doing it now is deliberate — `live-restore` only protects container uptime once
it is *already* active, so the restart that enables it is the one outage it
cannot cover. Enable it before there is anything to protect.

The config it writes matters: `json-file` logging with no `max-size` is the
standard way a homelab fills its disk overnight and takes every service down
with it. Note that log options apply to containers created *after* the change.

**`docker_remove_conflicting_packages: true`** purges `docker.io` and friends,
which is how Docker's own install guide opens. A no-op on a fresh host, and it
stops the distro packages being pulled in later as somebody else's dependency.
On a host already running containers from `docker.io`, it stops all of them.

## appuser

A service identity, not a person: uid/gid pinned to **2000:2000**, shell
`/usr/sbin/nologin`, password locked, no `~/.ssh` and no way to add one through
this role.

Nothing needs a login. Containers run as `2000:2000`, which is a uid the kernel
checks and which requires no host shell whatsoever, and systemd runs `ExecStart`
directly without one either. The one thing to remember, because it will catch
you out once:

```sh
sudo -u appuser docker compose ps    # works: sudo execs the command
sudo -u appuser -i                   # fails: -i asks for a login shell
su - appuser                         # fails: same reason
```

A home directory still exists at `/home/appuser` (mode 0750). Not an oversight —
anything running as appuser resolves `$HOME`, and the docker CLI writes
`~/.docker/config.json`.

**The uid is pinned, not auto-allocated,** because bind mounts carry numeric ids,
not names. If the host is rebuilt and `useradd` hands out 2001 next time, every
file under `/srv` is owned by a stranger and containers start failing in ways
that look like application bugs. 2000 also clears the 1000–1999 range that
`useradd` gives human logins, so a person added later cannot collide with it.

**On the `docker` group:** `appuser` is in it, which is root-equivalent — that
group lets you start a container bind-mounting `/` and step out as root. With
nologin, the only thing that can still use it is a systemd unit declaring
`User=appuser` that shells out to `docker compose`. If Ansible does all your
deploying it does so as root, in which case nothing needs the group and
`appuser_in_docker_group: false` removes the root-equivalence outright.

## Deploying a stack

```sh
make stack NAME=silverbullet     # or: ansible-playbook playbooks/silverbullet.yml
```

The `stack` role copies `stacks/<name>/compose.yaml` to `/opt/<name>/` (root
owned) and brings it up with `community.docker.docker_compose_v2`. Two things
happen first, both aimed at the same failure — a container that reports healthy
and is not:

- **Data directories are created and chowned to appuser.** Getting there before
  Docker does is the point: Docker creates a missing bind-mount source itself,
  as `root:root`, and with a pinned `user:` the container then starts, passes its
  healthcheck, and cannot write a byte.
- **The `.env` is checked to exist.** Secrets stay on the host and never enter
  this repo. The role never reads or rewrites the file — clobbering a working
  credential during a routine deploy is not a failure mode worth having. Commit
  `.env.example` next to the compose file to document the keys.

Adding a stack is a directory under `stacks/` and a playbook that names it plus
its data directories; `playbooks/silverbullet.yml` is nine lines and is the
template.

## Secrets

Per-stack `.env` files live on the host and are gitignored; commit
`.env.example` next to each compose file instead. That means a rebuild is not
yet fully reproducible from git — the credentials have to be placed by hand.

`ansible-vault` closes that gap when you want it: `.gitignore` already excludes
`.vault_pass`, and encrypted vault files are safe to commit and belong in the
repo.

## Next

- Vault the per-stack secrets, so a host rebuild needs nothing typed by hand.
- Backups. `/srv` is deliberately the only precious half of the filesystem
  convention, which makes it the whole of what a backup role has to cover.
