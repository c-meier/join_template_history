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
