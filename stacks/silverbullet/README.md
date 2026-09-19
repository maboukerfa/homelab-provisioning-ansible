# silverbullet

Personal notes, served over HTTP. Deployed with
`ansible-playbook playbooks/silverbullet.yml`.

| | |
|---|---|
| Host path | `/opt/silverbullet/` (compose file, root-owned) |
| Data | `/srv/silverbullet/` (bind mount, uid 2000 / appuser) — server root |
| Notes | `/srv/silverbullet/spaces/notes/` — the space itself |
| Published on | `http://<host>:8002/` |
| Secrets | `/srv/silverbullet/users.json`, written by `silverbullet setup` — no `.env` |
| Archived | SilverBullet's own git sync, to a private repo — see [Revisions and git sync](#revisions-and-git-sync) |

This runs SilverBullet in **multi-space** mode: one server hosting any number
of spaces, configured through a web dashboard rather than through environment
variables. There is exactly one space, `Notes`, bound to `/` — so from a
browser it behaves like an ordinary single-user notes server. Why that shape
and not the simpler one SilverBullet still offers:
[Why multi-space, with one space](#why-multi-space-with-one-space).

```
/srv/silverbullet/            the server root, mounted at /data
  users.json                  accounts: argon2 hashes, admin flag, API tokens
  spaces.json                 the space registry, rewritten by the dashboard
  server.json
  git-keys/                   per-space deploy keys for git sync
  .silverbullet.session.json  JWT signing secret
  spaces/
    notes/                    the space: a flat tree of markdown
      .git/                   SilverBullet's own, committed and pushed by it
```

Credentials live in the server root, notes one directory below it. That split
is what keeps the deploy key and the admin's password hash out of the
repository SilverBullet pushes: it only ever commits inside `spaces/notes`.

## Setup

The admin account is the one manual step, deliberately not in git. The playbook
refuses to deploy without it, because a server root with no `users.json` boots
into the setup wizard: an unauthenticated *create the first administrator* form
on port 8002, where whoever loads it first owns the server.

Replacing an instance rather than building the first one? Clear the old state
first — `sudo rm -rf /srv/silverbullet` — and empty the repo at the forge, or
delete and recreate it. Everything below assumes both are clean: a leftover
`users.json` makes `setup` refuse to run, and a repo with unrelated history
rejects the first push.

On the host:

```sh
sudo install -d -o root -g root -m 0755 /opt/silverbullet
sudo install -d -o 2000 -g 2000 -m 0750 /srv/silverbullet

read -rsp 'SilverBullet password: ' SBPW && echo
sudo docker run --rm -u 2000:2000 -v /srv/silverbullet:/data \
    --entrypoint /silverbullet zefhemel/silverbullet:2.11.0-slim \
    setup /data --admin "silverbullet:$SBPW" \
    --space Notes --space-folder spaces/notes
unset SBPW
```

Then from this repo: `ansible-playbook playbooks/silverbullet.yml`.

`setup` is the scriptable twin of the web wizard, and the reason to prefer it
is `--space-folder`: the wizard does not ask where a space lives and always
picks `spaces/<uuid>`. A name you can type is worth one command. `--at`
defaults to `/`, so the space is bound to the server root and
`http://<host>:8002/` opens the notes rather than the dashboard.

It writes `users.json`, `spaces.json` and `server.json`, creates
`spaces/notes`, and seeds an index page into it. Revisions come out
**managed**, which is what this stack wants — see
[Revisions and git sync](#revisions-and-git-sync).

`read -rsp` rather than typing the password as an argument keeps it out of your
shell history — though not out of this container's argv, which is visible to
root on the host for the second the command runs. Acceptable for a one-time
bootstrap on a box you already own; it is the same trade the upstream wizard
makes over HTTP.

`--entrypoint /silverbullet` bypasses the image's entrypoint, which exists to
resolve the data folder and drop privileges. Neither is wanted here: the folder
is given explicitly and `-u` already pins the uid.

`docker run` with the tag spelled out, rather than `docker compose run`, because
this happens before the playbook has put a compose file on the host — which is
also why the version is repeated here and has to be bumped alongside
`compose.yaml`.

Keep the password in your password manager. It is the only credential that
cannot be regenerated from this repo. For programmatic access, issue a token
per account from the dashboard under the account menu — they are revoked
individually, without restarting the server.

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

Since 2.11 the entrypoint does remap PUID/PGID — but only when it starts as
root, and it does not here: the uid is set in the compose file
(`user: "2000:2000"`), so the binary execs directly and the space has to be
owned to match.

The playbook creates `/srv/silverbullet` and the space folder under it owned by
2000 **before** compose runs, which is the whole reason that step exists. Left
to Docker, a missing bind-mount source is created as `root:root` — SilverBullet
then starts, passes its healthcheck, and cannot write a byte. That failure looks
like a working instance right up until the first save.

`/srv/silverbullet` rather than `/srv/space`, which is the name this would have
had under single-space mode — there "space" was SilverBullet's own word for its
data directory. In multi-space the data directory is the *server root* and a
space is a subfolder of it, so the stack sits on this repo's ordinary
`/srv/<stack>` convention and `spaces/notes` says what it is.

## Why multi-space, with one space

SilverBullet still offers a single-space mode: point `SB_FOLDER` at a directory
of markdown, set `SB_USER`, done. It is genuinely simpler than what is set up
here, and for one person with one space it is all you need.

It is also deprecated. 2.11 labelled every `SB_*` variable that configures a
space as legacy, moved revision configuration, git sync and OIDC to
multi-space only, and upstream expects to delete single-space entirely at 3.0.
Starting a new instance on it means scheduling a migration you have not done
yet.

So this runs the multi-space server with exactly one space in it. The cost is
a dashboard to click through once at setup and a directory of server state
next to the notes. What it buys, beyond not being deprecated:

- **Credentials sit outside the pushed tree.** In single-space mode the session
  secret lives in the space folder — the directory git sync pushes — safe only
  for as long as a gitignore entry holds. Here accounts, session state and the
  sync deploy key are all in the server root, which SilverBullet never commits.
- **API access is per-account.** Tokens are issued and revoked individually
  from the dashboard, rather than one shared bearer token in an `.env` that
  can only be rotated by restarting the server.
- **A second space costs nothing.** If one ever makes sense — something shared
  read-only, something scratch — it is a form, not a second container.

The space is bound to `/`, so none of this is visible from the browser: the
notes are still at `http://<host>:8002/`.

## Revisions and git sync

SilverBullet keeps the git history itself, per space, in one of three modes:

| | |
|---|---|
| `managed` | creates a repo in the space folder if absent, and commits for you |
| `unmanaged` | reads the history of a repo already there, and **never** commits |
| `disabled` | no history read or written, `Revision: *` commands hidden |

This space is **managed**, which is what `silverbullet setup` sets and what
this stack wants. Commits land about 30 seconds after typing stops, and at
least every five minutes during a long session — **Commit frequency** in the
space settings trades that against commit count. Each one is attributed to the
account that made the change, so `git log` names a person rather than a cron
job.

**Revision: Page History** and **Revision: Space History** read it back from
inside the editor, with a colour-coded diff per revision and a **Restore** that
lands as a single undo step. Uncommitted changes head both views.

Letting the application own its own history is the whole reason this is not a
systemd timer running `git commit` on a schedule: per-edit granularity instead
of a few commits a day, authorship that names a person, and a conflict UI when
a push and a local edit disagree. What it costs is that the remote is
configured in the dashboard rather than in this repo — the one piece of this
deployment Ansible does not describe.

### Connecting the remote

A private repo, empty, at the forge. Then in the space settings, **Connect
repository**:

1. Paste the repository URL. A web address is converted to its clone address,
   and the effective one is shown before testing.
2. Choose **Deploy key for this space**. SilverBullet generates the key and
   stores it in `/srv/silverbullet/git-keys/<space-id>` — inside the server
   root, above anything it commits.
3. Copy the public half and install it at the forge with **write access**. It
   has to be there before the check can pass.
4. **Check connection**, then review the destination, branch and frequency, and
   **Enable sync**.

Private, not internal or public: the repository is a verbatim copy of your
notes. A deploy key rather than a token because it is scoped to one repo, where
a token would hand the homelab write access to every repo on the account.

The repo must be **empty** at first connection. Pushing a fresh space into one
that already carries unrelated history needs a one-time choice about combining
them, which is a decision to make deliberately rather than discover.

This is a **backup mechanism, not collaboration** — upstream is explicit about
that, and it is also not a replacement for restic: binary attachments live in
this tree too and git stores them badly. It buys per-edit history for text and
an offsite copy.

### Checking on it

**Git: View status** and **Git: Sync now** from the command palette; Space
History shows sync state as well. Pulled changes reach open editors through the
normal file-change mechanism, and conflicts surface in **Git: Review
conflicts** — for markdown, with a keep-this / keep-that / edit-manually
choice per file.

Unknown or unavailable status is not the same as up to date. Network failures
retry with backoff; an authentication or configuration problem needs the
connection repaired rather than waiting.

If the key stops working, sync stops rather than falling back to the server's
own SSH identities — deliberate, so a deleted key fails loudly instead of
quietly pushing as somebody else.

### Restoring

The whole point of a plain markdown space is that restore is a clone. The notes
come back from the forge; the server configuration does not, because it holds
credentials and is not pushed. So a bare-metal restore is a clone plus the
[Setup](#setup) steps:

```sh
sudo install -d -o 2000 -g 2000 -m 0750 /srv/silverbullet /srv/silverbullet/spaces
sudo git clone git@github.com:<you>/<repo>.git /srv/silverbullet/spaces/notes
sudo chown -R 2000:2000 /srv/silverbullet/spaces/notes
```

Then run [Setup](#setup) as written, minus the `rm -rf` — `silverbullet setup`
with the same `--space-folder spaces/notes`, deploy, reconnect the remote. The
clone is already there and already a repository, so the space comes back
populated with its history intact and `setup` leaves it alone: the index page
is only seeded into a space with no markdown in it.

Reconnecting means a new deploy key, because the old one was in
`/srv/silverbullet/git-keys/` and went down with the server root. Register the
new public half and delete the old key at the forge.

Losing `users.json` costs one password reset and a few minutes of clicking.
That is the deliberate trade for keeping the admin's password hash out of a git
repository — and the notes, the part that cannot be recreated, are the part
that is backed up.

For a single page as of some point in time, from the editor: **Revision: Page
History**, pick a revision, **Restore**. From a shell:

```sh
cd /srv/silverbullet/spaces/notes
git log --oneline -- 'Journal/2026-08-06.md'
git show <sha>:'Journal/2026-08-06.md'
```

Writing a recovered file straight back into the space works and shows up in the
editor within moments: SilverBullet watches the space folder and pushes changes
made underneath it to open clients (`SB_FS_WATCH`, `auto` by default). A change
it detected but did not make is attributed to **External** in the history. If
one ever does not appear, "Space: Reindex" forces the issue.

## Verifying

```sh
sudo docker compose -f /opt/silverbullet/compose.yaml ps      # wait for (healthy)
sudo docker compose -f /opt/silverbullet/compose.yaml logs -f # "boot mode: multi-space"
ls -la /srv/silverbullet /srv/silverbullet/spaces/notes       # owned by 2000
```

The healthcheck is the image's own and curls `http://localhost:$SB_PORT/.instance`,
which is why `SB_PORT` is set explicitly in the compose file rather than left to
the image default — an unset `SB_PORT` turns the health status into a lie.
