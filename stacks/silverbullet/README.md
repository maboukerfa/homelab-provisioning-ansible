# silverbullet

Personal notes, served over HTTP. Deployed with
`ansible-playbook playbooks/silverbullet.yml`.

| | |
|---|---|
| Host path | `/opt/silverbullet/` (compose file, root-owned) |
| Data | `/srv/space` (bind mount, uid 2000 / appuser) |
| Published on | `http://<host>:8002/` |
| Secret | `/opt/silverbullet/.env` — `SB_USER`, see `.env.example` |

## Setup

The playbook creates and chowns `/srv/space` itself, so the only manual step is
the credential — it is deliberately not in git:

```sh
sudo install -d -o root -g root -m 0755 /opt/silverbullet
printf 'SB_USER=silverbullet:%s\n' '<password>' \
    | sudo install -o root -g root -m 0640 /dev/stdin /opt/silverbullet/.env

ansible-playbook playbooks/silverbullet.yml
```

Then log in at `http://<host>:8002/`. Keep the password in your password
manager: it is the only credential on the instance, and the compose file
refuses to start rather than exposing an unauthenticated SilverBullet without
it.

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

## Verifying

```sh
sudo docker compose -f /opt/silverbullet/compose.yaml ps      # wait for (healthy)
sudo docker compose -f /opt/silverbullet/compose.yaml logs -f # "boot mode: single-space"
ls -la /srv/space                                             # owned by 2000
```

The healthcheck is the image's own and curls `http://localhost:$SB_PORT/.instance`,
which is why `SB_PORT` is set explicitly in the compose file rather than left to
the image default — an unset `SB_PORT` turns the health status into a lie.
