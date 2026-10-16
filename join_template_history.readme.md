# join_template_history.py

A repo distributed by Classroom50 has no git history in common with the
template repo it came from. If the instructor later updates the template, a
plain `git merge` fails, and `git merge --allow-unrelated-histories` works but
flags every changed file as a conflict, even ones you never touched.

This script links your repo to the template once, so future updates merge
cleanly.

## Usage

```
TEMPLATE_URL=<template repo URL> uv run https://gist.githubusercontent.com/c-meier/c3765656d48eecb25aeb3e172111d993/raw/join_template_history.py
```

- `TEMPLATE_URL` (required): URL of the template repo.
- `TEMPLATE_BRANCH` (optional, default `main`): template branch to follow.
- `--quiet` / `-q` (or `QUIET=1`): only print errors and the result.

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and `git`.

### Single-branch templates only

This script always joins against one fixed template branch (`TEMPLATE_BRANCH`,
`main` by default), no matter which local branch you run it on. It does **not**
support Classroom50's "copy every branch of the template" setting — where a
student branch like `dev` should join against `template/dev` rather than
`template/main` — because there's no reliable way to map a student's branch name
back to the right template branch in general (e.g. a student could create a local
branch off a non-`main` branch, which still shares that branch's history and name
only by coincidence). Rather than guess and risk a silent wrong join, it just fails
with a clear error if no match is found on `TEMPLATE_BRANCH`.

## What it does

The script looks through the template's history for the commit whose content
matches your root commit exactly. If found, it links the two histories
without touching your commits or working directory: this only works because
the content is identical before and after the join, so there is nothing to
actually merge, just a link added to the history.

If no match is found, it stops without changing anything.

Idempotent: running it again after a successful join has no effect.

## Which branch to run it on

It only links the current branch, never `main` on your behalf. You can run it
on several branches separately (each ends up linked to the same template
commit), then merge them into `main` normally: no particular conflict results
from that.

The link only survives if the merge into `main` is a real merge. A squash
merge or a rebase loses it (they don't keep the ancestor the script added). If
that happens, just rerun the script on `main`: idempotent, it fixes the
situation immediately.

## After running it

Push the result:

```
git push origin <your-branch>
```

## Keeping the Feedback PR's diff clean: `--sync-feedback-branch`

Classroom50's "feedback" branch is frozen at accept time. As you merge template
updates into your work, the Feedback PR's "Files changed" (which compares `feedback`
against your branch) starts showing those template updates too, not just your own
work.

Here's the shape of history after the one-time join, two rounds of merging in a
template update, and running `--sync-feedback-branch` after each:

```
template/main:   T0 ──────────── T1 ──────────── T2
                 (accept point)   (template         (template
                                    update)            update)

main:        A ── B ── J ── M1 ── W1 ── M2 ── W2
                       │     │           │
                      2nd   2nd         2nd
                    parent parent     parent
                       ↓     ↓           ↓
                       T0    T1          T2

feedback:    A ── B ──────── F1 ──────────────── F2
                               │                    │
                              2nd                  2nd
                            parent                parent
                               ↓                    ↓
                               T1                    T2
```

- `A`, `B`: Classroom50's accept-time commits — identical on `main` and
  `feedback` until anything below happens.
- `J`: the one-time join (running this script with no flags).
- `M1`, `M2`: your own `git merge template/main`.
- `W1`, `W2`: your own work, committed normally.
- `F1`, `F2`: the sync commits this flag computes — each one you push yourself
  with the `git push ...` command it prints.

Every commit on `feedback`'s own line is a real ancestor of the next: each sync
only ever fast-forwards it, never rewrites it. Each sync commit's second parent
pins it to exactly the template content merged into `main` at that point, so a
diff of `feedback` against `main` shows only `W1`/`W2`-style work, never `M1`/`M2`'s
template content.

```
TEMPLATE_URL=<template repo URL> uv run <URL of this script> --sync-feedback-branch
```

- `FEEDBACK_BRANCH` (optional, default `feedback`): the branch to sync.
- Run it from whichever branch has been merging in the template (usually `main`).
- It never pushes anything itself — it prints a `git push ...` command for you to run
  (or review first).
- That push is a normal, non-force push: it extends `FEEDBACK_BRANCH` forward, it
  never rewrites it.
- Safe to run after every merge from the template, as many times as you like — each
  run only needs to catch `FEEDBACK_BRANCH` up to whatever's newly merged since the
  last sync.

As a side effect, this also cleans up the PR's "Commits" tab: before any sync, it
already includes every one of the template's own commits once you've merged even
once (that's inherent to how the history link works, with or without this feature),
plus the one-time join/marker bookkeeping commits. Syncing excludes the template's
own commits going forward, leaving just those few bookkeeping commits and your own
real work.

**Known limitation:** Classroom50's own setup files (`.classroom50.yaml` and similar)
will still show up as "changed" in the Feedback PR even though you never touched
them. This is an accepted trade-off, not a bug — GitHub computes a PR's diff from the
merge-base of the two branches, and since the synced commit is never literally part
of your branch's own history, that merge-base can only resolve to the template's raw
commit, which never had those files either. The alternative (pointing `feedback`
straight at a commit that's already part of your branch's real history) would avoid
that, but it would also hide how you resolved any real merge conflicts along the
way — which matters more for a PR meant to be read and commented on.
