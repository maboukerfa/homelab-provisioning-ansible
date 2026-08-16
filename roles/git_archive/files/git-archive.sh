#!/usr/bin/env bash
# Commit a directory and push it to a git remote. Deployed by
# roles/git_archive; every knob arrives as an environment variable set in the
# unit file, so this script is identical on every host and in this repo.
#
# Deliberately ONE-WAY (host -> remote). The typical subject is an application's
# data directory, and applications maintain their own index of it rather than
# watching for files that appear underneath them -- SilverBullet is the case
# this was written for. Pulling commits back down would leave that index lying
# about the contents until someone triggers a reindex by hand. The remote is an
# archive here, not a second place to edit from.
#
# NOT a replacement for a real backup tool. Binary attachments live in these
# trees too and git stores them badly. This gives per-edit history for the
# text; restic or equivalent remains the thing you restore from.
set -euo pipefail

ARCHIVE_DIR=${ARCHIVE_DIR:?ARCHIVE_DIR is not set}
BRANCH=${BRANCH:-main}
REMOTE_NAME=${REMOTE_NAME:-origin}
DEPLOY_KEY=${DEPLOY_KEY:?DEPLOY_KEY is not set}
KNOWN_HOSTS=${KNOWN_HOSTS:?KNOWN_HOSTS is not set}
QUIET_SECONDS=${QUIET_SECONDS:-30}
WARN_MB=${WARN_MB:-50}
FAIL_MB=${FAIL_MB:-95}

# Space-separated paths, relative to ARCHIVE_DIR, that must never be committed.
read -ra SECRETS <<<"${SECRETS:-}"

log() { echo "[$(date -Is)] $*"; }

# -c safe.directory keeps git from refusing on ownership grounds without
# needing global config on the host: the tree is owned by the uid the
# application runs as, which this timer matches but git cannot know that.
git() { command git -C "$ARCHIVE_DIR" -c safe.directory="$ARCHIVE_DIR" "$@"; }

# Everything under the tree except git's own metadata. Used three times below,
# and getting the -path prune wrong means walking .git on every pass.
tree_find() {
    find "$ARCHIVE_DIR" -type f -not -path "$ARCHIVE_DIR/.git/*" "$@"
}

[[ -d $ARCHIVE_DIR/.git ]] || { echo "no git repo at $ARCHIVE_DIR" >&2; exit 1; }
[[ -r $DEPLOY_KEY ]] || { echo "cannot read deploy key $DEPLOY_KEY" >&2; exit 1; }
[[ -r $KNOWN_HOSTS ]] || { echo "cannot read known_hosts $KNOWN_HOSTS" >&2; exit 1; }

# IdentitiesOnly stops ssh offering any other key it happens to find first and
# being rejected before it gets to this one. StrictHostKeyChecking=yes with an
# explicit UserKnownHostsFile is the point of pinning the key in the role:
# there is no shell here to accept a prompt at, and no ~/.ssh to fall back on.
export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes \
-o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS"

# A file saved seconds ago may still be mid-write. Nothing is lost by leaving
# it for the next pass, so skip the whole run rather than commit half a file.
if [[ -n $(tree_find -newermt "-$QUIET_SECONDS seconds" -print -quit) ]]; then
    log "written to within the last ${QUIET_SECONDS}s, skipping this pass"
    exit 0
fi

# Size guard, before anything is staged.
while read -r size path; do
    [[ -z $size ]] && continue
    if (( size > FAIL_MB * 1024 * 1024 )); then
        log "ERROR: $path is $((size / 1024 / 1024))MB - the remote will reject the push"
        log "ERROR: move it out of $ARCHIVE_DIR or track it with git-lfs, then rerun"
        exit 1
    fi
    log "WARNING: $path is $((size / 1024 / 1024))MB - large for a git repo"
done < <(tree_find -size +$((WARN_MB * 1024))k -printf '%s %p\n' | sort -rn)

# Belt and braces over .gitignore: if one of these is tracked or would be
# staged, stop before it reaches a commit. Once pushed it is leaked whatever
# you do to the history afterwards.
for secret in "${SECRETS[@]}"; do
    [[ -e $ARCHIVE_DIR/$secret ]] || continue
    if git ls-files --error-unmatch "$secret" >/dev/null 2>&1 \
        || ! git check-ignore --quiet "$secret" 2>/dev/null; then
        log "ERROR: $secret is not ignored and holds credentials or live state"
        log "ERROR: add it to $ARCHIVE_DIR/.gitignore, and 'git rm --cached' it if tracked"
        exit 1
    fi
done

# Cheap "is there anything to do at all" check. Deliberately NOT used to
# describe the commit: `git status --porcelain` collapses an untracked
# directory into a single `Journal/` line, so a first import of 200 notes
# reports as one change and the body names no files at all.
if [[ -n $(git status --porcelain) ]]; then
    git add -A

    # Described from the index instead, once everything is staged. --name-status
    # expands directories to their actual files and prefixes each with A/M/D,
    # which is what makes the body worth reading when you are hunting for the
    # commit that last touched a page. Works on an unborn HEAD too, so the
    # initial import is described the same way as every commit after it.
    summary=$(git diff --cached --name-status)

    if [[ -z $summary ]]; then
        # Staging turned out to be a no-op: something changed and changed back
        # between the two calls. Bailing out here rather than letting `git
        # commit` fail on an empty index and take the push with it.
        log "no changes once staged"
    else
        changed=$(wc -l <<<"$summary" | tr -d ' ')
        log "committing $changed file(s)"
        # Subject stays scannable in `git log --oneline`; the body carries the
        # file list.
        git commit --quiet -m "archive: $changed file(s) changed" -m "$summary"
    fi
else
    log "no changes"
fi

# Push unconditionally rather than only after a commit: an earlier run may have
# committed and then failed to push -- network down, key rotated -- and that
# commit must not sit here unnoticed until the next edit.
if git push --quiet "$REMOTE_NAME" "$BRANCH"; then
    log "pushed $(git rev-parse --short HEAD) to $REMOTE_NAME/$BRANCH"
else
    unpushed=$(git rev-list --count "$REMOTE_NAME/$BRANCH..$BRANCH" 2>/dev/null || echo '?')
    log "ERROR: push failed - $unpushed commit(s) unpushed"
    exit 1
fi

# Many small commits leave a lot of loose objects; let git repack when it
# decides it is worth it.
git gc --auto --quiet
