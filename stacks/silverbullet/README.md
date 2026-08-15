# silverbullet

Personal notes, served over HTTP. Deployed with
`ansible-playbook playbooks/silverbullet.yml`.

| | |
|---|---|
| Host path | `/opt/silverbullet/` (compose file, root-owned) |
| Data | `/srv/space` (bind mount, uid 2000 / appuser) |
| Published on | `http://<host>:8002/` |
| Secrets | `/opt/silverbullet/.env` — `SB_USER` and `SB_AUTH_TOKEN`, see `.env.example` |

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

## Verifying

```sh
sudo docker compose -f /opt/silverbullet/compose.yaml ps      # wait for (healthy)
sudo docker compose -f /opt/silverbullet/compose.yaml logs -f # "boot mode: single-space"
ls -la /srv/space                                             # owned by 2000
```

The healthcheck is the image's own and curls `http://localhost:$SB_PORT/.instance`,
which is why `SB_PORT` is set explicitly in the compose file rather than left to
the image default — an unset `SB_PORT` turns the health status into a lie.
