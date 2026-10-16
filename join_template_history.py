#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
One-time setup for students: links a Classroom50-distributed repo (which has no
shared git history with the lab's template repo) to the template's real history,
so that later `git fetch template && git merge template/<branch>` produces precise
three-way merges instead of "both added" conflicts on every file that ever changed.

Meant to be run straight from wherever this script is hosted, e.g.:

    TEMPLATE_URL=<template repo URL> uv run <URL of this script>

TEMPLATE_URL (and optionally TEMPLATE_BRANCH, default "main") must be set per lab;
this script itself is lab-agnostic. Pass --quiet/-q (or set QUIET=1) to only print
errors and the final result.

Only single-branch templates are supported: this always joins against one fixed
template branch (TEMPLATE_BRANCH), regardless of which local branch the student is
on. Labs that copy every template branch into the student repo (so e.g. a "dev"
branch should join against template/dev, not template/main) aren't supported — the
mapping from student branch to template branch isn't reliable in general (a student
could branch off a non-main branch locally, etc.), so this refuses to guess rather
than risk a silent wrong join; it fails with a clear error instead.

Two subcommands:

  join              The one-time history link above (the default student flow).
  sync-feedback     Keeps Classroom50's "feedback" branch (FEEDBACK_BRANCH, default
                     "feedback") in sync with whatever template content has actually
                     been merged into the current branch, so the Feedback PR's "Files
                     changed" diff shows only the student's own work, not template
                     updates they pulled in along the way. Computes a ready-to-push
                     commit and prints the push command -- never pushes itself.
"""
import argparse
import os
import shutil
import subprocess
import sys

TEMPLATE_URL = os.environ.get("TEMPLATE_URL", "REPLACE_ME")
TEMPLATE_BRANCH = os.environ.get("TEMPLATE_BRANCH", "main")
FEEDBACK_BRANCH = os.environ.get("FEEDBACK_BRANCH", "feedback")

# Tags a sync_feedback_branch() commit so a later run can tell "this is a commit this
# script made" apart from any other 2-parent merge commit someone else might put on
# FEEDBACK_BRANCH (a TA's manual merge, GitHub's "Update branch" button, resolving a
# conflict in the web UI) -- mirroring find_join_point's own marker-based approach
# below, rather than inferring that from commit-graph shape (parent count) alone.
SYNC_MARKER = "Synced-by: join_template_history.py"

QUIET = os.environ.get("QUIET", "") in ("1", "true", "True")


def log(message):
    if not QUIET:
        print(f"join_template_history.py: {message}", flush=True)


def fail(message):
    print(f"join_template_history.py: {message}", file=sys.stderr, flush=True)
    return 1


def git(*args, check=True):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, ["git", *args], result.stdout, result.stderr)
    return result


def git_ok(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).returncode == 0


def git_out(*args, check=True):
    return git(*args, check=check).stdout.strip()


def three_way_merge_tree(base_tree, ours_tree, theirs_tree):
    """Merges three trees using git's real merge machinery (the same recursive/ort
    strategy an actual `git merge` uses, including full textual line-merging for
    non-overlapping edits to the same file) and returns the resulting tree sha, or
    None if the merge has real conflicts.

    Implemented via three ephemeral commits -- base_tree as a root, ours_tree and
    theirs_tree each as its child -- fed to `git merge-tree --write-tree`, so git's
    own merge-base resolution lands on base_tree without us needing to special-case
    anything. These commits are plain objects in the local database: nothing is
    written to any ref, the working tree, or the real index.

    (An earlier version used `git read-tree -m`, which only merges at the whole-file
    level -- it can't combine two non-conflicting edits to different lines of the
    same file the way a real merge does, so it reported conflicts that `git merge`
    would have resolved cleanly.)"""
    base_commit = git_out("commit-tree", base_tree, "-m", "base")
    ours_commit = git_out("commit-tree", ours_tree, "-p", base_commit, "-m", "ours")
    theirs_commit = git_out("commit-tree", theirs_tree, "-p", base_commit, "-m", "theirs")
    result = subprocess.run(["git", "merge-tree", "--write-tree", ours_commit, theirs_commit],
                             capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def single_root_tree(ref, label):
    """Returns (error, tree): the tree of the single root commit reachable from
    `ref`, or a fail()'d error (naming `ref` as `label`) if there isn't exactly one."""
    roots = git_out("rev-list", "--max-parents=0", ref).splitlines()
    if len(roots) != 1:
        return fail(f"expected {label} to have exactly one root commit, found "
                    f"{len(roots)} — history doesn't look as expected."), None
    return None, git_out("rev-parse", f"{roots[0]}^{{tree}}")


def preflight():
    """Read-only checks shared by both main() and sync_feedback_branch(). Must all
    pass before either touches anything.

    Returns (error, existing_template_url): error is None on success, and
    existing_template_url (also None if the remote doesn't exist yet) is returned so
    callers don't need to re-query it.
    """
    if shutil.which("git") is None:
        return fail("git was not found on your PATH. Install it (https://git-scm.com/downloads) and try again."), None

    if TEMPLATE_URL == "REPLACE_ME":
        return fail("TEMPLATE_URL not set (pass it as an environment variable)."), None

    if git_out("rev-parse", "--is-inside-work-tree", check=False) != "true":
        return fail("not inside a git repository — aborting before touching anything."), None

    if git_out("rev-parse", "--abbrev-ref", "HEAD", check=False) in ("", "HEAD"):
        return fail("HEAD is detached (not on a branch) — checkout a branch first."), None

    # `git config --get`, not `git remote get-url`: the latter falls back to treating
    # a remote with an empty `url =` as if its URL were the remote's own name (verified
    # empirically), which would make a corrupted config silently pass as "doesn't exist
    # yet". `git config --get` reports the raw value (exit 1 if the key is unset at all,
    # exit 0 with empty output if it's set but empty), so the mismatch check below
    # catches the corrupted-but-present case correctly instead.
    remote_exists = git_ok("config", "--get", "remote.template.url")
    existing = git_out("config", "--get", "remote.template.url") if remote_exists else None
    if remote_exists and existing != TEMPLATE_URL:
        return fail(
            f"remote 'template' already exists and points to {existing!r}, "
            f"not {TEMPLATE_URL!r} — refusing to overwrite it."
        ), None

    return None, existing


def no_operation_in_progress():
    """main()-specific (not part of preflight(), which both entry points share):
    main() is about to run update-ref on the current branch, which is unsafe to do
    mid-merge/rebase/cherry-pick/bisect. sync_feedback_branch() never writes any
    local ref at all, so it doesn't need this gate -- applying it there would block a
    perfectly safe sync-feedback run over something happening on an unrelated
    branch."""
    git_dir = git_out("rev-parse", "--git-dir")
    in_progress = []
    if git_ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
        in_progress.append("merge")
    if git_ok("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"):
        in_progress.append("cherry-pick")
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")) or os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        in_progress.append("rebase")
    if os.path.exists(os.path.join(git_dir, "BISECT_LOG")):
        in_progress.append("bisect")
    if in_progress:
        return fail(f"a {'/'.join(in_progress)} is already in progress — resolve or abort it first.")
    return None


def ensure_template_fetched(existing_template_url):
    """Adds the 'template' remote (if `existing_template_url` is None) and fetches
    TEMPLATE_BRANCH. Shared by main() and sync_feedback_branch() -- callers must run
    preflight() (and, for main(), also no_operation_in_progress()) first, since this
    itself starts mutating things."""
    if existing_template_url is None:
        log(f"adding remote 'template' -> {TEMPLATE_URL}")
        try:
            git("remote", "add", "template", TEMPLATE_URL)
        except subprocess.CalledProcessError as e:
            return fail(f"could not add remote 'template': {e.stderr.strip()}")

    log(f"fetching template/{TEMPLATE_BRANCH}...")
    try:
        git("fetch", "-q", "template", TEMPLATE_BRANCH)
    except subprocess.CalledProcessError as e:
        return fail(f"could not fetch {TEMPLATE_URL!r} ({TEMPLATE_BRANCH}): {e.stderr.strip()}")
    return None


def find_join_point(root_tree):
    log("looking for the template commit matching this repo's initial state...")

    marker = git_out("log", "--grep=Open Feedback PR", "--format=%H", "-1", "HEAD")
    if marker:
        log("found the Classroom50 accept-time marker, trying its date first...")
        marker_date = git_out("show", "-s", "--format=%cI", marker)
        candidate = git_out("rev-list", "-1", f"--before={marker_date}", f"template/{TEMPLATE_BRANCH}")
        if candidate and git_out("rev-parse", f"{candidate}^{{tree}}") == root_tree:
            return candidate

    log("scanning the template's full history for an exact match (this can take a moment)...")
    # Newest-first (git's default order): if the template's tree recurs at several
    # points in history (e.g. a change that got reverted), picking the most recent
    # match doesn't change merge correctness -- matching trees are byte-identical, so
    # a future 3-way merge diffs the same content either way -- but it does minimize
    # how many already-neutralized commits show up later in `git log HEAD..template/...`
    # previews.
    for candidate in git_out("rev-list", f"template/{TEMPLATE_BRANCH}").splitlines():
        if git_out("rev-parse", f"{candidate}^{{tree}}") == root_tree:
            return candidate
    return None


def main():
    preflight_error, existing_template_url = preflight()
    if preflight_error is not None:
        return preflight_error

    in_progress_error = no_operation_in_progress()
    if in_progress_error is not None:
        return in_progress_error

    current_branch = git_out("rev-parse", "--abbrev-ref", "HEAD")

    fetch_error = ensure_template_fetched(existing_template_url)
    if fetch_error is not None:
        return fetch_error

    template_tip = git_out("rev-parse", f"template/{TEMPLATE_BRANCH}")
    merge_base = git_out("merge-base", "HEAD", f"template/{TEMPLATE_BRANCH}", check=False)
    if merge_base:
        if merge_base == template_tip:
            log("already joined and up to date, nothing to do.")
        else:
            print(f"join_template_history.py: already joined, but the template has new commits. "
                  f"Run: git merge template/{TEMPLATE_BRANCH}")
        return 0

    error, root_tree = single_root_tree("HEAD", "this repo")
    if error is not None:
        return error

    join_point = find_join_point(root_tree)

    if join_point is None:
        return fail(
            "no template commit matches this repo's initial content, refusing to guess. "
            "This can happen if the template's history was rewritten since your repo was created, "
            f"or if your current branch ({current_branch!r}) doesn't actually come from "
            f"template/{TEMPLATE_BRANCH}. You can try a manual merge or ask the assistant for help."
        )

    log(f"found matching template commit {join_point}; linking without touching your working tree...")
    old_head = git_out("rev-parse", "HEAD")
    head_tree = git_out("rev-parse", "HEAD^{tree}")
    # root_tree == tree(join_point) exactly (that's how join_point was found), so attaching
    # it as a second parent changes zero content: HEAD's tree is reused verbatim, which is
    # why this never needs a checkout, a stash, or any other working-tree operation.
    new_head = git_out("commit-tree", head_tree, "-p", old_head, "-p", join_point,
                        "-m", f"Join template history at {join_point}")
    try:
        git("update-ref", f"refs/heads/{current_branch}", new_head, old_head)
    except subprocess.CalledProcessError:
        return fail(f"{current_branch} moved while this ran — rerun from a stable state.")

    log("done — nothing in your working tree was touched.")
    if join_point != template_tip:
        print(f"join_template_history.py: the template already has new commits since your join point. "
              f"Run: git merge template/{TEMPLATE_BRANCH}")
    print(f"join_template_history.py: don't forget to push this: git push origin {current_branch}")
    return 0


def sync_feedback_branch():
    preflight_error, existing_template_url = preflight()
    if preflight_error is not None:
        return preflight_error

    current_branch = git_out("rev-parse", "--abbrev-ref", "HEAD")

    fetch_error = ensure_template_fetched(existing_template_url)
    if fetch_error is not None:
        return fetch_error

    merge_point = git_out("merge-base", current_branch, f"template/{TEMPLATE_BRANCH}", check=False)
    if not merge_point:
        return fail(
            f"{current_branch!r} has no merged template history yet — run the 'join' subcommand first."
        )

    # No branch.<current_branch>.remote config to go on: `git clone` auto-sets tracking
    # only for the branch it checks out at clone time, but this script may well be
    # running from a different, locally-created branch (`git checkout -b <name>`),
    # which git does not auto-track -- so a missing tracking config here is an expected
    # case, not a rare one. A bare "origin" guess is only right if that's genuinely the
    # student's one real remote (beyond "template", which we just added/confirmed
    # above), so check for that specifically before assuming it.
    remote = git_out("config", f"branch.{current_branch}.remote", check=False)
    if not remote:
        other_remotes = [r for r in git_out("remote").splitlines() if r != "template"]
        remote = other_remotes[0] if len(other_remotes) == 1 else "origin"

    log(f"fetching {FEEDBACK_BRANCH!r} from {remote!r}...")
    try:
        git("fetch", "-q", remote, FEEDBACK_BRANCH)
    except subprocess.CalledProcessError as e:
        return fail(f"could not fetch {FEEDBACK_BRANCH!r} from {remote!r}: {e.stderr.strip()}")
    old_feedback_head = git_out("rev-parse", "FETCH_HEAD")

    is_ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", merge_point, old_feedback_head],
                                  capture_output=True, text=True)
    if is_ancestor.returncode == 0:
        log(f"{FEEDBACK_BRANCH!r} is already in sync with {current_branch!r}, nothing to do.")
        return 0
    if is_ancestor.returncode != 1:
        # returncode 1 means "genuinely not an ancestor yet" (the expected, common
        # case -- a sync is needed); anything else is a real git error (e.g. a
        # shallow clone or a stale ref missing the objects needed to answer this),
        # which must fail loudly rather than being silently treated the same as
        # "not synced yet".
        return fail(f"could not determine whether {FEEDBACK_BRANCH!r} is already in sync: "
                    f"{is_ancestor.stderr.strip()}")

    try:
        old_feedback_message = git_out("log", "-1", "--format=%B", old_feedback_head)
        if SYNC_MARKER in old_feedback_message:
            # Already synced by this script before: its own 2nd parent is exactly the
            # template content it was synced to last time -- the correct 3-way-merge
            # base this time. (Checking for this marker, rather than just "does the
            # tip have 2 parents", matters because FEEDBACK_BRANCH could also gain an
            # unrelated 2-parent merge commit some other way -- a TA's manual merge,
            # GitHub's "Update branch" button, a web-UI conflict resolution -- whose
            # 2nd parent is not template content at all.)
            old_feedback_parents = git_out("log", "-1", "--format=%P", old_feedback_head).split()
            if len(old_feedback_parents) != 2:
                return fail(f"{FEEDBACK_BRANCH!r}'s tip looks like a previous sync by this script "
                            f"(by its commit message) but doesn't have the expected two parents — "
                            f"history doesn't look as expected.")
            base_tree = git_out("rev-parse", f"{old_feedback_parents[1]}^{{tree}}")
        else:
            error, base_tree = single_root_tree(old_feedback_head, f"{FEEDBACK_BRANCH!r}")
            if error is not None:
                return error
        ours_tree = git_out("rev-parse", f"{old_feedback_head}^{{tree}}")
        theirs_tree = git_out("rev-parse", f"{merge_point}^{{tree}}")
    except subprocess.CalledProcessError as e:
        return fail(f"could not read {FEEDBACK_BRANCH!r}'s history: {e.stderr.strip()}")

    # A plain tree swap to merge_point's tree would *lose* FEEDBACK_BRANCH's own non-template
    # content (e.g. Classroom50's .classroom50.yaml), making it spuriously reappear as a
    # "student change" later. A real three-way merge -- base: the original template content
    # both sides started from, ours: FEEDBACK_BRANCH's current content, theirs: merge_point's
    # template content -- keeps FEEDBACK_BRANCH's own additions while picking up the
    # template's new content, the same way an actual `git merge` would. This still correctly
    # shows a student's own conflict-resolution choices later (since "theirs" is the template's
    # raw content, not whatever the student resolved a real conflict to on their own branch).
    #
    # One known, accepted limitation: GitHub computes PR diffs from merge-base(base, head), and
    # since this new commit is never literally an ancestor of current_branch (current_branch was
    # already built without it), that merge-base can only resolve to merge_point itself -- not to
    # this commit's own (correctly merged) tree. So files unique to FEEDBACK_BRANCH, like
    # .classroom50.yaml, will still show up as "changed" in the actual Feedback PR even though
    # they're untouched. There's no way around this without either force-pushing a history
    # rewrite onto current_branch itself (unacceptably invasive) or fast-forwarding
    # FEEDBACK_BRANCH to a real commit already on current_branch's history (which would hide a
    # student's own conflict-resolution work instead) -- showing a couple of extra bookkeeping
    # files is the lesser problem.
    merged_tree = three_way_merge_tree(base_tree, ours_tree, theirs_tree)
    if merged_tree is None:
        return fail(
            f"could not automatically sync {FEEDBACK_BRANCH!r}: merging the template's new content "
            f"with what's already on {FEEDBACK_BRANCH!r} produced a conflict. This can happen if "
            f"something was committed to {FEEDBACK_BRANCH!r} beyond Classroom50's own setup commits. "
            f"Resolve it manually."
        )

    try:
        # old_feedback_head stays a real parent (so this is a normal, non-force-push-requiring
        # commit); merge_point becomes the second parent, same technique as the main join.
        new_feedback_head = git_out(
            "commit-tree", merged_tree, "-p", old_feedback_head, "-p", merge_point,
            "-m", f"Sync {FEEDBACK_BRANCH} to the template content merged into {current_branch}\n\n{SYNC_MARKER}")
    except subprocess.CalledProcessError as e:
        return fail(f"could not create the sync commit: {e.stderr.strip()}")

    # Preview using merge_point, not new_feedback_head: that's what GitHub's three-dot PR
    # diff will actually resolve its merge-base to (see the comment above), so this is the
    # honest preview of what Files changed will show -- including the known false positives.
    preview = git_out("diff", "--stat", merge_point, current_branch, check=False)
    log(f"preview of what the Feedback PR would show after pushing:\n{preview or '(nothing)'}")
    print(f"join_template_history.py: ready — this did not push anything. To publish it, run:\n"
          f"  git push {remote} {new_feedback_head}:refs/heads/{FEEDBACK_BRANCH}\n"
          f"Note: Classroom50's own setup files (e.g. .classroom50.yaml) may show up as "
          f"\"changed\" in the Feedback PR even though you never touched them — that's expected, ignore those.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", "-q", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "join", help="One-time history link (the default student flow). Run from the student repo.")

    subparsers.add_parser(
        "sync-feedback",
        help="Print a ready-to-push commit that updates FEEDBACK_BRANCH to the template content "
             "merged into the current branch. Never pushes anything itself.")

    args = parser.parse_args()
    if args.quiet:
        QUIET = True
    if args.command == "sync-feedback":
        sys.exit(sync_feedback_branch())
    else:
        sys.exit(main())
