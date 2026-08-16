# silverbullet

Personal notes, served over HTTP. Deployed with
`ansible-playbook playbooks/silverbullet.yml`.

| | |
|---|---|
| Host path | `/opt/silverbullet/` (compose file, root-owned) |
| Data | `/srv/space` (bind mount, uid 2000 / appuser) |
| Published on | `http://<host>:8002/` |
| Secrets | `/opt/silverbullet/.env` — `SB_USER` and `SB_AUTH_TOKEN`, see `.env.example` |
| Archived | `/srv/space` to a private git repo every 6h — see [Archiving the space](#archiving-the-space) |

## Setup

The playbook creates and chowns `/srv/space` itself, so the only manual step is
the two secrets — deliberately not in git. On the host:

```sh
sudo install -d -o root -g root -m 0755 /opt/silverbullet

read -rsp 'SilverBullet password: ' SBPW && echo
printf 'SB_USER=silverbullet:%s\nSB_AUTH_TOKEN=%s\n' "$SBPW" "$(openssl rand -hex 32)" \
    | sudo install -o root -g root -m 0640 /dev/stdin /opt/silverbullet/.env
unset SBPW
```

Then from this repo: `ansible-playbook playbooks/silverbullet.yml`.

`read -rsp` rather than typing the password as an argument keeps it out of your
shell history.

Both values are required — compose refuses to start without either, rather than
coming up with authentication half-configured. They do different jobs:

| | |
|---|---|
| `SB_USER` | interactive login, `user:password`. Log in as **`silverbullet`** — the part before the colon |
| `SB_AUTH_TOKEN` | bearer token for the HTTP API, for anything talking to SilverBullet programmatically |

Keep both in your password manager. They are the only credentials on the
instance and nothing in git can regenerate them.

## Reaching it

**`http://<host>:8002/` will not work properly**, and no server-side setting can
change that. Per [the upstream docs](https://silverbullet.md/TLS), SilverBullet
depends on service workers, crypto APIs and clipboard APIs, and browsers only
enable those in a *secure context*: `https://` or `http://localhost`. It is a
web-standards rule, not a SilverBullet option, and no environment variable can
grant it.

Two ways to get one.

### SSH tunnel — works right now, no extra infrastructure

```sh
ssh -N -L 8002:localhost:8002 maboukerfa@<host>
```

Leave that running and open `http://localhost:8002/`. The browser sees
`localhost`, so it is a secure context and everything works. Good for a single
machine and for verifying the deployment.

### Reverse proxy with TLS — the real fix

A proxy terminating HTTPS in front of the container, which is what makes it work
from phones and other machines. The upstream docs use Caddy, which gets a
certificate automatically:

```
notes.example.com {
    reverse_proxy <host>:8002
}
```

That needs a domain pointed at the box and ports 80/443 reachable. Nothing in
this repo deploys a proxy yet; it is the obvious next stack.

### Once you have a proxy, stop publishing 8002 on the LAN

Plain HTTP on 8002 cannot give a working session anyway, so exposing it to the
whole network buys nothing. Bind it to loopback in `compose.yaml`:

```yaml
    ports:
      - '127.0.0.1:8002:3000'
```

The SSH tunnel above keeps working; a proxy on the same host does too.

## The uid is pinned, and that matters

The image is a single static binary. It ships no non-root user and does no
PUID/PGID remapping in its entrypoint, so the uid is set in the compose file
(`user: "2000:2000"`) and the space has to be owned to match.

The playbook creates `/srv/space` owned by 2000 **before** compose runs, which
is the whole reason that step exists. Left to Docker, a missing bind-mount
source is created as `root:root` — SilverBullet then starts, passes its
healthcheck, and cannot write a byte. That failure looks like a working
instance right up until the first save.

`/srv/space` rather than `/srv/silverbullet` as the repo convention would
suggest: "space" is SilverBullet's own term for its data directory.

## Archiving the space

```sh
ansible-playbook playbooks/silverbullet-git.yml
```

A separate playbook from the deploy, because it needs things the container does
not: a repo at the forge and a deploy key on the host. A routine redeploy of
SilverBullet should never block on GitHub being reachable.

It installs `silverbullet-git.timer`, which every six hours commits whatever
changed under `/srv/space` and pushes it. The space is a flat tree of markdown,
so git is the natural archive for it: every edit becomes a diff and a year of
history costs less than one of the photos already in there. See
[Archiving a data directory to git](../../README.md#archiving-a-data-directory-to-git)
for what the role does and does not promise — the short version is per-edit
history and an offsite copy, not a replacement for restic.

`space.gitignore` in this directory is deployed to `/srv/space/.gitignore`. The
line that matters is `.silverbullet.auth.json`: it sits in the space root, holds
live session state, and is named in `git_archive_secrets` as well, so the timer
hard-stops rather than trusting the ignore file worked.

### One-time setup

1. **A private repo.** Private, not internal or public — the archive is a
   verbatim copy of your notes. Put its SSH URL in
   `inventory/group_vars/all.yml` as `silverbullet_git_remote`.

2. **A deploy key on the host.** Scoped to that one repo; a token would give
   the homelab write access to every repo on the account.

   ```sh
   sudo install -d -o root -g root -m 0755 /etc/silverbullet
   sudo ssh-keygen -t ed25519 -N '' -C 'silverbullet-archive' \
       -f /etc/silverbullet/deploy_key
   sudo cat /etc/silverbullet/deploy_key.pub
   ```

   Add that public key to the repo under **Settings → Deploy keys**, with
   **Allow write access** checked. The playbook chowns the private half to
   appuser and enforces `0600` itself.

Then run the playbook. It initialises `/srv/space` as a repo, installs the
units, and runs the archive once so you find out immediately whether the key
was registered — rather than six hours later, in a journal.

Nothing else is needed: the first commit and push happen inside that first run,
through the same script that will run four times a day forever after.

### Restoring

The whole point of a plain markdown space is that restore is a clone:

```sh
sudo git clone git@github.com:<you>/silverbullet-space.git /srv/space
sudo chown -R 2000:2000 /srv/space
ansible-playbook playbooks/silverbullet.yml
ansible-playbook playbooks/silverbullet-git.yml
```

For a single page as of some point in time:

```sh
git -C /srv/space log --oneline -- 'Journal/2026-08-06.md'
git -C /srv/space show <sha>:'Journal/2026-08-06.md'
```

Writing a recovered file straight back into the space works, but the browser
keeps its own client-side index under v2 and will keep showing the old content
until it resyncs — reload the page, and run "Space: Reindex" if it persists.

That client-side index is also why this is one-way. SilverBullet does not watch
the space directory for changes made underneath it, so commits pulled down from
GitHub would be invisible until a manual reindex. Edit in SilverBullet, read
history on GitHub.

### Checking on it

```sh
systemctl list-timers silverbullet-git.timer
journalctl -u silverbullet-git.service -n 50
sudo systemctl start silverbullet-git.service   # force a pass now
```

If a push fails with `ERROR: Repository not found.`, ask the key which repo it
is actually bound to — a deploy key sees exactly one repository and reports
every other one, including ones your account can see, as nonexistent:

```sh
sudo -u appuser ssh -i /etc/silverbullet/deploy_key -o IdentitiesOnly=yes \
    -o UserKnownHostsFile=/etc/silverbullet/known_hosts \
    -o StrictHostKeyChecking=yes -T git@github.com
```

GitHub answers `Hi <owner>/<repo>! You've successfully authenticated`, naming
the repo. If that is not `silverbullet_git_remote`, the remote is wrong (this
caught a `silverbullet-space` / `SilverBullet_space` mismatch once). Exit status
1 is expected — GitHub does not offer shell access.

Note the options go on `ssh` itself. Putting them in `GIT_SSH_COMMAND` and then
running `ssh -T` tests nothing: only `git` reads that variable, so the bare
`ssh` runs with no key and no pinned host key and fails on host key
verification, which looks like a much worse problem than it is.

`Permission denied (publickey)` instead means the public half was never
registered; `remote: Write access to repository not granted` means it was
registered without **Allow write access** ticked.

## Verifying

```sh
sudo docker compose -f /opt/silverbullet/compose.yaml ps      # wait for (healthy)
sudo docker compose -f /opt/silverbullet/compose.yaml logs -f # "boot mode: single-space"
ls -la /srv/space                                             # owned by 2000
```

The healthcheck is the image's own and curls `http://localhost:$SB_PORT/.instance`,
which is why `SB_PORT` is set explicitly in the compose file rather than left to
the image default — an unset `SB_PORT` turns the health status into a lie.
