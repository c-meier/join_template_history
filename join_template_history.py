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
"""
import argparse
import os
import shutil
import subprocess
import sys

TEMPLATE_URL = os.environ.get("TEMPLATE_URL", "REPLACE_ME")
TEMPLATE_BRANCH = os.environ.get("TEMPLATE_BRANCH", "main")

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
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result


def git_ok(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).returncode == 0


def git_out(*args, check=True):
    return git(*args, check=check).stdout.strip()


def preflight():
    """Read-only checks. Must all pass before we touch anything."""
    if shutil.which("git") is None:
        return fail("git was not found on your PATH. Install it (https://git-scm.com/downloads) and try again.")

    if TEMPLATE_URL == "REPLACE_ME":
        return fail("TEMPLATE_URL not set (pass it as an environment variable).")

    if git_out("rev-parse", "--is-inside-work-tree", check=False) != "true":
        return fail("not inside a git repository — aborting before touching anything.")

    if git_out("rev-parse", "--abbrev-ref", "HEAD", check=False) in ("", "HEAD"):
        return fail("HEAD is detached (not on a branch) — checkout a branch first.")

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

    if git_ok("remote", "get-url", "template"):
        existing = git_out("remote", "get-url", "template")
        if existing != TEMPLATE_URL:
            return fail(
                f"remote 'template' already exists and points to {existing!r}, "
                f"not {TEMPLATE_URL!r} — refusing to overwrite it."
            )

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
    for candidate in git_out("rev-list", f"template/{TEMPLATE_BRANCH}").splitlines():
        if git_out("rev-parse", f"{candidate}^{{tree}}") == root_tree:
            return candidate
    return None


def main():
    preflight_error = preflight()
    if preflight_error is not None:
        return preflight_error

    current_branch = git_out("rev-parse", "--abbrev-ref", "HEAD")

    if not git_ok("remote", "get-url", "template"):
        log(f"adding remote 'template' -> {TEMPLATE_URL}")
        git("remote", "add", "template", TEMPLATE_URL)
    log(f"fetching template/{TEMPLATE_BRANCH}...")
    try:
        git("fetch", "-q", "template", TEMPLATE_BRANCH)
    except subprocess.CalledProcessError as e:
        return fail(f"could not fetch {TEMPLATE_URL!r} ({TEMPLATE_BRANCH}): {e.stderr.strip()}")

    merge_base = git_out("merge-base", "HEAD", f"template/{TEMPLATE_BRANCH}", check=False)
    if merge_base:
        template_tip = git_out("rev-parse", f"template/{TEMPLATE_BRANCH}")
        if merge_base == template_tip:
            log("already joined and up to date, nothing to do.")
        else:
            print(f"join_template_history.py: already joined, but the template has new commits. "
                  f"Run: git merge template/{TEMPLATE_BRANCH}")
        return 0

    roots = git_out("rev-list", "--max-parents=0", "HEAD").splitlines()
    if len(roots) != 1:
        return fail(f"expected exactly one root commit, found {len(roots)} — history doesn't look as expected.")
    root_commit = roots[0]
    root_tree = git_out("rev-parse", f"{root_commit}^{{tree}}")

    join_point = find_join_point(root_tree)

    if join_point is None:
        return fail(
            "no template commit matches this repo's initial content, refusing to guess. "
            "This can happen if the template's history was rewritten since your repo was created. "
            "You can try a manual merge or ask the assistant for help."
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
    print(f"join_template_history.py: don't forget to push this: git push origin {current_branch}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()
    if args.quiet:
        QUIET = True
    sys.exit(main())
