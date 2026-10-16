#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Black-box tests for join_template_history.py.

Each test builds real git repos under a temp dir (a "template" and a "student"
repo, shaped like Classroom50 actually produces them) and runs the script as a
subprocess against them, the same way a student would. This is deliberately
not mocked: the bugs this script is sensitive to (join-point ordering, branch
resolution, ...) only show up against real git behavior.

Run with: python3 test_join_template_history.py [-v]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().with_name("join_template_history.py"))


class Clock:
    """A mutable, steadily-advancing clock. Each Repo owns its own by default; pass an
    existing one (e.g. another Repo's `.clock`) to share it between them instead, so
    their commits land on one single chronological timeline -- in the order they're
    actually issued in test code -- rather than each repo's own independent one. That
    makes events across repos readable in the order they happen instead of grouped by
    repo, but it does mean code order must then match chronological order: a commit
    meant to represent something that happens "later" has to be issued later in the
    test too, or it'll tick an earlier timestamp than it should."""

    STEP = timedelta(minutes=1)

    def __init__(self) -> None:
        self.now = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def tick(self) -> datetime:
        self.now += self.STEP
        return self.now

    def set(self, when: datetime) -> None:
        self.now = when


class Repo:
    """Thin wrapper around a repo directory with the git operations tests need
    repeatedly -- shared by both the template repo (via TemplateBuilder) and the
    student repo, since both are just "a repo tests poke at" underneath.

    Every commit gets an explicit, steadily-advancing timestamp instead of wall-clock
    "now" (see Clock), so a whole test's commits land on a deterministic timeline
    regardless of how fast the test runs -- rather than jumping to the real current
    date the moment some ad-hoc commit forgets to pass `when`. Passing an explicit
    `when` -- as make_accepted_student_repo does when anchoring the student's
    accept-time commits to a specific moment in the template's own history (its
    shared_clock=False path) -- also re-seeds the clock to continue from there, so
    later auto-ticked commits stay consistent with it."""

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock if clock is not None else Clock()
        self.shas: dict[str, str] = {}

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        full_env = {**os.environ, **env} if env else None
        result = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, env=full_env)
        if result.returncode != 0:
            raise RuntimeError(f"git {args} failed in {self.path}:\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def write_file(self, name: str, content: str) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit_file(self, filename: str, content: str, message: str) -> "Commit":
        """Writes `filename` and commits it in one step -- the common case for both
        the template and the student repo, which only ever differ in how they
        auto-tick the clock (see Clock/commit's docstrings), not in this pattern
        itself. Tracks the resulting sha by message, like TemplateBuilder always has,
        for tests that want to assert on a specific earlier commit later."""
        self.write_file(filename, content)
        sha = self.commit(message)
        self.shas[message] = sha
        return Commit(self, sha, self.clock.now)

    def commit(self, message: str, empty: bool = False, when: datetime | None = None) -> str:
        if when is None:
            when = self.clock.tick()
        else:
            self.clock.set(when)
        env = {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}
        if empty:
            self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        else:
            self.git("add", "-A")
            self.git("commit", "-q", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def checkout(self, ref: str, new_branch: bool = False, start_point: str | None = None) -> None:
        args = ["checkout", "-q"]
        if new_branch:
            args.append("-b")
        args.append(ref)
        if start_point is not None:
            args.append(start_point)
        self.git(*args)

    def merge(self, ref: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "merge", "--no-edit", ref], cwd=self.path,
                               capture_output=True, text=True)

    def tip(self) -> str:
        return self.git("rev-parse", "HEAD")

    def branch_off(self, new_branch: str, from_ref: str = "HEAD") -> str:
        self.git("branch", new_branch, from_ref)
        return new_branch

    def status(self) -> str:
        return self.git("status", "--porcelain")

    def push(self, *refs: str, remote: str = "origin") -> None:
        self.git("push", "-q", remote, *refs)

    def fetch(self, ref: str, remote: str = "origin") -> None:
        self.git("fetch", "-q", remote, ref)

    def diff_files(self, *args: str) -> list[str]:
        return self.git("diff", "--name-only", *args).splitlines()

    def parents_of_head(self) -> list[str]:
        return self.git("log", "--format=%P", "-1", "HEAD").split()

    @staticmethod
    def init(path: Path, branch: str = "main", remote: Path | None = None,
             clock: Clock | None = None) -> "Repo":
        """Creates a fresh git repo at `path` and wraps it. A staticmethod, not a
        classmethod: it always hands back a plain Repo, since a subclass with its own
        extra state to set up (e.g. TemplateBuilder) can't just delegate construction
        to cls(path) here -- it calls this for the git-init plumbing only and builds
        itself separately.

        `remote`, when given, is wired in immediately as "origin" -- for a repo that's
        meant to track one from the start (e.g. the student repo, created knowing the
        GitHub remote already exists), as opposed to a remote added onto a repo after
        the fact (which a plain `repo.git("remote", "add", ...)` still covers). It's a
        bare Path, not a Repo: the remote in practice is always a bare repo (see
        init_bare_repo below), which has no working tree and so can't support most of
        Repo's own methods -- wrapping it as a Repo would be misleading.

        `clock`, when given, makes this repo share an existing Clock instead of
        getting its own -- see Clock's docstring."""
        path.mkdir(parents=True, exist_ok=True)
        repo = Repo(path, clock=clock)
        repo.git("init", "-q", "-b", branch)
        repo.git("config", "user.email", "t@example.com")
        repo.git("config", "user.name", "tester")
        if remote is not None:
            repo.git("remote", "add", "origin", str(remote))
        return repo


def init_bare_repo(path: Path) -> Path:
    """A bare repo has no working tree, so it isn't a Repo -- here, it only ever
    serves as a push/fetch target (simulating the GitHub remote), so a bare Path is
    all that's needed; none of Repo's methods would work on it anyway."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


def run_script(
    student: Repo,
    template_url: str,
    template_branch: str | None = None,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("TEMPLATE_URL", None)
    env.pop("TEMPLATE_BRANCH", None)
    env.pop("FEEDBACK_BRANCH", None)
    env.pop("QUIET", None)
    env["TEMPLATE_URL"] = template_url
    if template_branch is not None:
        env["TEMPLATE_BRANCH"] = template_branch
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, SCRIPT, *(args or [])],
        cwd=student.path,
        env=env,
        capture_output=True,
        text=True,
    )


def run_sync_feedback_branch(student: Repo, template_url: str) -> subprocess.CompletedProcess[str]:
    return run_script(student, template_url, args=["--sync-feedback-branch"])


def sync_and_push_feedback(student: Repo, template_url: str) -> subprocess.CompletedProcess[str]:
    """Runs --sync-feedback-branch and, unless it reports nothing to do, actually runs
    the push command it prints -- the same two steps a student/professor would do by
    hand. Needed whenever a later step depends on FEEDBACK_BRANCH's state on the
    remote actually having moved (idempotency checks, multiple sync rounds, or
    confirming the push itself succeeds) -- see sync_feedback_branch_commit for
    tests that only need to inspect the computed commit's content once, which that
    commit's sha already lets them do without ever pushing it anywhere. Returns the
    sync command's own result (its stdout still has "already in sync" when nothing
    was pushed, as the single thing callers need to branch on)."""
    result = run_sync_feedback_branch(student, template_url)
    assert result.returncode == 0, result.stderr
    # The only other successful outcome prints no push command at all ("already in
    # sync"), so a missing match here just means there's nothing to push.
    match = re.search(r"git push (\S+) (\S+):refs/heads/(\S+)", result.stdout)
    if match:
        remote, sha, branch = match.groups()
        student.push(f"{sha}:refs/heads/{branch}", remote=remote)
    return result


def sync_feedback_branch_commit(student: Repo, template_url: str) -> str:
    """Runs --sync-feedback-branch and returns the sha of the commit it computed,
    without pushing it anywhere. That commit already exists as a real object in
    student's local repo the moment the script finishes -- pushing it only matters
    for tests that care about the remote's state afterward (see
    sync_and_push_feedback); a test that just wants to inspect the resulting content
    (e.g. via `student.git("diff", f"{sha}...main")`) doesn't need it published."""
    result = run_sync_feedback_branch(student, template_url)
    assert result.returncode == 0, result.stderr
    match = re.search(r"git push \S+ (\S+):refs/heads/\S+", result.stdout)
    assert match, f"no computed commit found in:\n{result.stdout}"
    return match.group(1)


class Commit:
    def __init__(self, repo: Repo, sha: str, when: datetime) -> None:
        self.repo = repo
        self.sha = sha
        self.when = when


def make_accepted_student_repo(
    tmp: Path,
    template_tree_commit: Commit,
    branch: str = "main",
    with_feedback_marker: bool = True,
    with_setup_commit: bool = True,
    origin: Path | None = None,
    shared_clock: bool = True,
) -> Repo:
    """Mimics what Classroom50 actually produces when a student accepts: a fresh repo
    with the template's tree as the first commit's content but no shared history,
    plus Classroom50's own bookkeeping commits on top. When `with_feedback_marker` is
    set, also creates the "feedback" branch Classroom50 itself creates alongside the
    marker commit -- frozen one commit behind it, at the classroom-init commit -- since
    the two are the same real feature, not independent choices. When `origin` is given,
    pushes straight to it: that's how Classroom50 actually hands students a repo, with
    the content already on GitHub, not a separately-pushed afterthought.

    `with_setup_commit` mimics an assignment with `no_autograder: true` (or
    `empty_repo: true`) -- confirmed against a real accepted repo and Classroom50's
    own source (foundation50/classroom50, "skip .classroom50.yaml for no_autograder
    assignments", v1.56.0): `acceptWithoutSetupCommit` writes no files at all, so the
    root commit (template content, unmodified) is immediately followed by the "Open
    Feedback PR" marker with nothing in between -- `feedback` ends up frozen directly
    on the root commit itself, not on a later classroom-init commit.

    `shared_clock` makes the student repo tick the SAME Clock as the template
    `template_tree_commit` came from, so events across both repos read in the order
    they actually happen in test code instead of each repo having its own independent
    timeline -- see Clock's docstring for the trade-off (code order must then match
    chronological order). Pass False to opt back into an independent clock instead,
    with the accept-time commits anchored to accept_point's own timestamp -- that
    stays correct regardless of what the caller does with `tpl` afterward, for a test
    that can't (or shouldn't have to) keep its code order matching real chronological
    order."""
    clock = template_tree_commit.repo.clock if shared_clock else None
    student = Repo.init(tmp / "student", branch=branch, remote=origin, clock=clock)
    # Re-materialize the template's tree into the (unrelated) student repo.
    subprocess.run(
        f"git -C {template_tree_commit.repo.path} archive {template_tree_commit.sha} | tar -x -C {student.path}",
        shell=True, check=True,
    )
    # The three bookkeeping commits below are genuinely simultaneous in real life
    # (Classroom50 creates them all in one programmatic step at accept time), so they
    # always share one timestamp -- ticked once here, not once per commit.
    if shared_clock:
        accept_time = student.clock.tick()
    else:
        # Anchored to accept_point's own commit time rather than wall-clock "now" or
        # the student's own (independent) clock: a fast-running test creates every
        # commit within the same wall-clock second, which would make find_join_point's
        # marker-based dated lookup see LATER template commits as "before the marker"
        # too, and try one of those first instead of the real accept_point. A real
        # accept always happens at an actual point in time, strictly before any future
        # template commits. TemplateBuilder's one-minute-per-commit clock guarantees
        # this +1s lands correctly between accept_point and whatever comes after it,
        # regardless of whether that happens before or after this call in test code.
        accept_time = template_tree_commit.when + timedelta(seconds=1)
    student.commit("Initial commit", when=accept_time)
    if with_setup_commit:
        # Real shape of both files, from Classroom50's own source
        # (createClassroom50Yaml / defaultAutograderWorkflow in
        # web/src/domain/assignments/autograderYaml.ts, foundation50/classroom50) --
        # landed together in one commit, matching assignmentAcceptTree committing
        # both in a single tree. A static copy, not generated -- it can drift if
        # Classroom50 changes that rendering (it has before: v1.56.0 added the
        # no_autograder skip entirely). To check whether it still has:
        #   1. `gh student accept <org> <classroom> <assignment>` against a real
        #      assignment with autograding ON (no_autograder must be unset/false --
        #      that path commits nothing at all, see with_setup_commit=False below).
        #   2. Inspect the new repo's .classroom50.yaml and
        #      .github/workflows/autograde.yaml.
        #   3. Diff their shape against what's hardcoded here; if it moved, the
        #      source file above has the authoritative current rendering.
        student.write_file(".classroom50.yaml", (
            'schema: "classroom50/repo-config/v1"\n'
            'classroom: "bdr-a"\n'
            'assignment: "lab1"\n'
            "owner:\n"
            '  username: "student1"\n'
            "  id: 12345\n"
            f'  accepted_at: "{accept_time.isoformat().replace("+00:00", "Z")}"\n'
            "source:\n"
            '  owner: "org"\n'
            "  owner_id: 1\n"
            '  repo: "template"\n'
            '  branch: "main"\n'
        ))
        student.write_file(".github/workflows/autograde.yaml", (
            "name: Autograde\n"
            "\n"
            "on:\n"
            "  push:\n"
            '    branches: ["main"]\n'
            '    tags: ["submit/*"]\n'
            "\n"
            "jobs:\n"
            "  grade:\n"
            '    uses: "org/classroom50/.github/workflows/autograde-runner.yaml@main"\n'
            "    permissions:\n"
            "      contents: write\n"
            "      statuses: write\n"
            "      pull-requests: write\n"
        ))
        student.commit("[Classroom 50] Initialize .classroom50.yaml and autograde workflow (gh student accept)",
                        when=accept_time)
    branches_to_push = [branch]
    if with_feedback_marker:
        student.commit("[Classroom 50] Open Feedback PR (gh student accept)\n\n[skip ci]", empty=True,
                        when=accept_time)
        student.branch_off("feedback", "HEAD~1")
        branches_to_push.append("feedback")
    if origin is not None:
        student.push(*branches_to_push)
    return student


class TemplateBuilder(Repo):
    """Builds a template repo. commit_file (and the shas tracking it relies on) and
    commit dating are both inherited from Repo -- see their docstrings. The dating
    matters here specifically because find_join_point has a commit-date-based fast
    path, and real commit timestamps only have 1-second resolution -- wall-clock time
    alone can't be trusted to keep same-test commits apart or in the right order."""

    def __init__(self, path: Path, branch: str = "main") -> None:
        super().__init__(Repo.init(path, branch=branch).path)
        self.branch = branch


class JoinTemplateHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    # ---- template had multiple commits before the student ever accepted ----

    def test_template_with_history_before_accept(self) -> None:
        # shared_clock + this ordering: events read in the order they actually happen
        # (scaffold, readme, accept, *then* the template moves on) rather than
        # grouped by repo with the template's "future" built up front.
        tpl = TemplateBuilder(self.tmp / "template")
        tpl.commit_file("README.md", "v0\n", "chore: scaffold")
        tpl.commit_file("README.md", "v1\n", "docs: write readme")
        accept_point = tpl.commit_file("app.py", "print('hi')\n", "feat: add app")

        student = make_accepted_student_repo(self.tmp, accept_point)
        tpl.commit_file("app.py", "print('hi there')\n", "feat: tweak app")

        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())
        # The template already moved past accept_point by the time this ran: the
        # student should be told about it on the very first (fresh) join too, not
        # just on later already-joined runs.
        self.assertIn("the template already has new commits", result.stdout)

    def test_sync_feedback_branch_unaffected_by_template_history_predating_accept(self) -> None:
        # single_root_tree() (used for FEEDBACK_BRANCH's base on its first sync) looks
        # for a single root commit reachable from FEEDBACK_BRANCH's own tip, not from
        # the template -- the student's repo always starts fresh at accept with one
        # root, regardless of how many commits deep into the template's own history
        # accept_point sits. This pins that down: template content from *before*
        # accept_point (README.md here) must stay just as invisible to the Feedback
        # PR diff as template content added *after* it.
        tpl = TemplateBuilder(self.tmp / "template")
        tpl.commit_file("README.md", "v0\n", "chore: scaffold")
        tpl.commit_file("README.md", "v1\n", "docs: write readme")
        accept_point = tpl.commit_file("app.py", "print('hi')\n", "feat: add app")

        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        tpl.commit_file("app.py", "print('hi there')\n", "feat: tweak app")
        run_script(student, str(tpl.path))
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        student.commit_file("student_work.py", "print('mine')\n", "my own work")

        sync_and_push_feedback(student, str(tpl.path))

        student.fetch("feedback")
        diff_files = set(student.diff_files("FETCH_HEAD...main"))
        self.assertIn("student_work.py", diff_files)
        self.assertNotIn("app.py", diff_files)
        self.assertNotIn("README.md", diff_files)

    # ---- a fresh join must also warn if the template already moved on by then ----

    def test_fresh_join_warns_when_template_already_has_new_commits(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")

        student = make_accepted_student_repo(self.tmp, accept_point)
        tpl.commit_file("file.txt", "v2\n", "template moves on before the student even ran this")

        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())
        self.assertIn("the template already has new commits", result.stdout)
        self.assertIn("git merge template/main", result.stdout)

    def test_fresh_join_stays_quiet_when_template_has_no_new_commits(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")

        student = make_accepted_student_repo(self.tmp, accept_point)
        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())
        self.assertNotIn("new commits", result.stdout)

    # ---- repeated tree in template history: must pick the newest match ----
    #
    # When multiple commits share the exact same tree, picking any of them as the join
    # point is equally correct for merge purposes -- matching trees are byte-identical,
    # so a future 3-way merge diffs the same content regardless of which one was chosen
    # (verified both directions below). But picking the *newest* match minimizes how
    # many already-neutralized commits show up later in a `git log HEAD..template/...`
    # preview, so that's the tie-breaker the fallback scan uses.

    def test_picks_newest_matching_commit_when_tree_recurs(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        tpl.commit_file("file.txt", "hello\n", "A: initial")
        tpl.commit_file("file.txt", "changed\n", "B: change")
        revert_point = tpl.commit_file("file.txt", "hello\n", "C: revert back to A's tree")

        # No feedback-PR marker here on purpose: this isolates the full-history
        # fallback scan (find_join_point's second half), which has no commit date
        # to disambiguate with and must rely on tree-match order alone. The dated
        # marker fast-path is covered separately and is not what's under test here.
        student = make_accepted_student_repo(self.tmp, revert_point, with_feedback_marker=False)
        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(revert_point.sha, student.parents_of_head())
        self.assertNotIn(tpl.shas["A: initial"], student.parents_of_head())

    # ---- accepting exactly AT the revert commit vs before it ----

    def test_accept_at_revert_commit_with_marker_resolves_exactly(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        tpl.commit_file("file.txt", "hello\n", "A: initial")
        tpl.commit_file("file.txt", "changed\n", "B: change")
        revert_point = tpl.commit_file("file.txt", "hello\n", "C: revert back to A's tree")

        # Realistic case: marker present. Its dated lookup must resolve to C exactly --
        # the commit the student actually accepted at -- regardless of the fallback
        # scan's tie-breaking rule (not exercised here at all).
        student = make_accepted_student_repo(self.tmp, revert_point, with_feedback_marker=True)
        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(revert_point.sha, student.parents_of_head())

    def test_accept_at_first_occurrence_without_marker_still_merges_correctly(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "orig\n", "A: initial")
        tpl.commit_file("file.txt", "temp\n", "B: change")
        revert_point = tpl.commit_file("file.txt", "orig\n", "C: revert back to A's tree")

        # Student's real accept was at A, but no marker means the fallback scan has no
        # date info and picks the newest match, C, instead. That's fine -- A and C have
        # byte-identical trees, so git's merge-base diff computation is unaffected --
        # but it's worth proving a later merge, including one touching the very file
        # that was changed-then-reverted in between, still comes out correct.
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=False)
        run_script(student, str(tpl.path))
        self.assertIn(revert_point.sha, student.parents_of_head())
        self.assertNotIn(accept_point.sha, student.parents_of_head())

        student.commit_file("file.txt", "student's own edit\n", "student edits file.txt independently")

        run_script(student, str(tpl.path))  # already-joined no-op, just re-fetches
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        self.assertEqual((student.path / "file.txt").read_text(), "student's own edit\n")
        self.assertEqual(student.status(), "")

    def test_join_works_on_a_no_autograder_accept_with_no_setup_commit(self) -> None:
        # A `no_autograder: true` (or `empty_repo: true`) assignment's accept writes
        # no files at all (confirmed against Classroom50's own source and a real
        # accepted repo) -- the root commit is immediately followed by the "Open
        # Feedback PR" marker, with no .classroom50.yaml commit in between. The root
        # commit itself is still just the template's own unmodified content, so the
        # join must succeed exactly as in the normal case.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")

        student = make_accepted_student_repo(self.tmp, accept_point, with_setup_commit=False)
        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())

    def test_join_works_with_neither_setup_commit_nor_feedback_pr(self) -> None:
        # One step further than the no_autograder case above: with `feedback_pr: false`
        # too (and no Pages), Classroom50's needsBranch check goes false and the whole
        # feedback/setup step never runs at all -- `main` ends up with literally just
        # the root commit, nothing else, and no `feedback` branch is ever created.
        # Confirmed empirically (see git log in the commit that added this test). The
        # join only needs a single root commit reachable from HEAD, so this must still
        # succeed even though HEAD and that root are the very same, only commit.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")

        student = make_accepted_student_repo(
            self.tmp, accept_point, with_setup_commit=False, with_feedback_marker=False,
        )
        self.assertEqual(len(student.git("log", "--oneline", "--all").splitlines()), 1)
        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())

    # ---- template unchanged since accept: nothing to do, no spurious commit ----

    def test_no_new_template_commits_when_joining(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")

        student = make_accepted_student_repo(self.tmp, accept_point)
        first = run_script(student, str(tpl.path))
        self.assertEqual(first.returncode, 0, first.stderr)
        head_after_join = student.tip()

        second = run_script(student, str(tpl.path))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already joined and up to date", second.stdout)
        self.assertEqual(student.tip(), head_after_join)

    def test_local_student_commit_between_runs_does_not_confuse_already_joined(self) -> None:
        # The student moving HEAD forward with their own, unrelated work must not make
        # the second run think there's a fresh join to do, misreport the template as
        # having new commits, or touch HEAD beyond the student's own commit.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        run_script(student, str(tpl.path))

        student.commit_file("my_work.py", "print('hi')\n", "student's own local work")
        head_after_local_commit = student.tip()

        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already joined and up to date", result.stdout)
        self.assertEqual(student.tip(), head_after_local_commit)

    # ---- template gained exactly one commit after the join point ----

    def test_one_new_template_commit_after_join(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        run_script(student, str(tpl.path))

        tpl.commit_file("file.txt", "hello v2\n", "template moves on, one commit")
        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already joined, but the template has new commits", result.stdout)

    # ---- join + real merge: unrelated changes merge clean ----

    def test_merge_after_join_is_clean_when_files_dont_overlap(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        run_script(student, str(tpl.path))

        # Template changes a file the student never touched.
        tpl.commit_file("template_only.txt", "new from template\n", "template adds its own file")
        # Student changes a different file.
        student.commit_file("student_only.txt", "student work\n", "student does their own work")

        run_script(student, str(tpl.path))  # re-fetches template, no-ops the join (already joined)
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        self.assertEqual(student.status(), "")

    # ---- join + real merge: only the file touched on both sides conflicts ----

    def test_merge_after_join_conflicts_only_on_shared_file(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        run_script(student, str(tpl.path))

        tpl.commit_file("untouched.txt", "stays the same\n", "add untouched file")

        # Both sides edit shared.txt differently -> real conflict expected there, and
        # only there (not on untouched.txt, which neither side touched further).
        tpl.commit_file("shared.txt", "template's version\n", "template edits shared file")
        student.commit_file("shared.txt", "student's version\n", "student edits shared file")

        run_script(student, str(tpl.path))
        merge = student.merge("template/main")
        self.assertNotEqual(merge.returncode, 0)

        conflicted = student.diff_files("--diff-filter=U")
        self.assertEqual(conflicted, ["shared.txt"])

    # ---- Classroom50's feedback branch: same join point as main, not a separate template branch ----

    def test_feedback_branch_uses_main_not_its_own_name(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")

        # with_feedback_marker=True already creates "feedback" (first 2 commits only);
        # just switch into it.
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True)
        student.checkout("feedback")

        result = run_script(student, str(tpl.path))  # TEMPLATE_BRANCH left unset on purpose

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())

    # ---- multi-branch templates ("copy every branch" setting) are explicitly unsupported ----
    #
    # Auto-mapping a student's branch name to a same-named template branch was tried and
    # reverted: it breaks the (more common) case of a student branching locally off a
    # non-main branch, since that local branch's name has nothing to do with which
    # template branch its content actually came from. So instead of guessing, the script
    # always joins against one fixed TEMPLATE_BRANCH (default "main") and must fail
    # cleanly, not silently mis-join, when that branch's content doesn't match.

    def test_multi_branch_template_fails_safely_instead_of_guessing(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template", branch="main")
        tpl.commit_file("common.txt", "shared base\n", "common ancestor")
        tpl.checkout("dev", new_branch=True)
        dev_point = tpl.commit_file("dev_only.txt", "dev content\n", "dev-specific work")
        tpl.checkout("main")

        # Student's repo was created from template's "dev", but nobody told the script
        # that (TEMPLATE_BRANCH left at its "main" default) -> dev's content never
        # appears in template/main's history, so this must fail, not guess.
        student = make_accepted_student_repo(self.tmp, dev_point, branch="dev", with_feedback_marker=False)
        head_before = student.tip()

        result = run_script(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no template commit matches", result.stderr)
        self.assertEqual(student.tip(), head_before)

    def test_explicit_template_branch_can_target_a_non_main_branch(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template", branch="main")
        tpl.commit_file("common.txt", "shared base\n", "common ancestor")
        tpl.checkout("other", new_branch=True)
        other_point = tpl.commit_file("other_only.txt", "other content\n", "other-specific work")
        tpl.checkout("main")

        # No auto-mapping, but a professor can still explicitly point a lab at a
        # non-main template branch via TEMPLATE_BRANCH.
        student = make_accepted_student_repo(self.tmp, other_point, branch="main", with_feedback_marker=False)
        result = run_script(student, str(tpl.path), template_branch="other")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(other_point.sha, student.parents_of_head())

    def test_existing_template_remote_with_corrupted_empty_url_is_reported_accurately(self) -> None:
        # `git remote get-url template` resolves a remote with an empty `url =` in
        # .git/config to the literal string "template" (the remote's own name) rather
        # than an empty string (verified empirically) -- reading the raw config value
        # via `git config --get remote.template.url` instead avoids that trap and
        # reports the actual (empty) value in the error.
        tpl = TemplateBuilder(self.tmp / "template", branch="main")
        c1 = tpl.commit_file("a.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, c1)
        with open(student.path / ".git" / "config", "a") as f:
            f.write('[remote "template"]\n\turl =\n')

        result = run_script(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists and points to ''", result.stderr)

    # ---- dirty working tree must be left untouched ----

    def test_dirty_working_tree_is_left_untouched(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)

        student.write_file("file.txt", "locally modified, not committed\n")  # dirty tracked file
        student.write_file("staged_new.txt", "staged but not committed\n")
        student.git("add", "staged_new.txt")
        student.write_file("untracked.txt", "just sitting here\n")
        status_before = student.status()

        result = run_script(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())
        self.assertEqual(student.status(), status_before)
        self.assertEqual((student.path / "file.txt").read_text(), "locally modified, not committed\n")
        self.assertEqual((student.path / "untracked.txt").read_text(), "just sitting here\n")

    # ---- a student branch split off main (not Classroom50's feedback branch) ----

    def test_branch_split_from_unjoined_main_joins_correctly(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)

        student.checkout("feature", new_branch=True)
        student.commit_file("feature.txt", "student's new feature\n", "start feature work")

        result = run_script(student, str(tpl.path))  # run directly on feature, main never joined

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())

    def test_branch_split_from_already_joined_main_is_already_joined(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        run_script(student, str(tpl.path))  # join main first
        main_head = student.tip()

        student.checkout("feature", new_branch=True)
        student.commit_file("feature.txt", "student's new feature\n", "start feature work")

        result = run_script(student, str(tpl.path))  # feature already inherits main's join

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already joined and up to date", result.stdout)
        self.assertIn(main_head, student.git("log", "--format=%H"))

    # ---- joining a deeply nested branch, then merging it back up through its ancestors ----

    def test_join_on_nested_branch_survives_merging_back_up(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)

        # main -> feature -> feature-sub, two levels deep. The join only ever runs on
        # the deepest branch; main and feature never touch the script directly.
        student.checkout("feature", new_branch=True)
        student.commit_file("feature.txt", "feature work\n", "feature work")
        student.checkout("feature-sub", new_branch=True)
        student.commit_file("feature_sub.txt", "nested work\n", "nested work")

        result = run_script(student, str(tpl.path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(accept_point.sha, student.parents_of_head())

        student.checkout("feature")
        merge1 = student.merge("feature-sub")
        self.assertEqual(merge1.returncode, 0, merge1.stdout + merge1.stderr)

        student.checkout("main")
        merge2 = student.merge("feature")
        self.assertEqual(merge2.returncode, 0, merge2.stdout + merge2.stderr)

        self.assertEqual(student.status(), "")
        result2 = run_script(student, str(tpl.path))  # the link survived the merges up
        self.assertIn("already joined and up to date", result2.stdout)

    # ---- joining two separate branches independently, then merging both into main ----

    def test_two_separately_joined_branches_merge_into_main_without_conflict(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        main_tip_before = student.tip()

        # Two branches off the same un-joined main, each run through the script on its
        # own -- exactly the README's "run it on several branches separately" case --
        # and each *also* merging the template for real at a different point in its
        # evolution (not just joining), to exercise the realistic full workflow.
        student.checkout("feature-a", new_branch=True, start_point=main_tip_before)
        student.commit_file("a.txt", "feature a\n", "feature a work")
        self.assertEqual(run_script(student, str(tpl.path)).returncode, 0)
        tpl.commit_file("t1.txt", "template file 1\n", "template adds t1")
        run_script(student, str(tpl.path))  # re-fetch
        merge_tpl_a = student.merge("template/main")
        self.assertEqual(merge_tpl_a.returncode, 0, merge_tpl_a.stdout + merge_tpl_a.stderr)

        student.checkout("feature-b", new_branch=True, start_point=main_tip_before)
        student.commit_file("b.txt", "feature b\n", "feature b work")
        self.assertEqual(run_script(student, str(tpl.path)).returncode, 0)
        tpl.commit_file("t2.txt", "template file 2\n", "template adds t2")
        run_script(student, str(tpl.path))  # re-fetch
        merge_tpl_b = student.merge("template/main")
        self.assertEqual(merge_tpl_b.returncode, 0, merge_tpl_b.stdout + merge_tpl_b.stderr)

        student.checkout("main")
        merge1 = student.merge("feature-a")
        self.assertEqual(merge1.returncode, 0, merge1.stdout + merge1.stderr)

        # By now main has diverged (gained feature-a's work), so merging feature-b in
        # forces a real 3-way merge -- not a fast-forward -- against a second,
        # independently-created join commit that happens to share the same template
        # parent, *and* against template content (t1.txt) feature-b also has but
        # pulled in via its own separate merge. That must not confuse git into
        # treating template content as conflicting "both added" changes.
        merge2 = student.merge("feature-b")
        self.assertEqual(merge2.returncode, 0, merge2.stdout + merge2.stderr)

        self.assertEqual(student.status(), "")
        for name in ("a.txt", "b.txt", "t1.txt", "t2.txt"):
            self.assertTrue((student.path / name).exists(), name)

    def test_two_branches_merging_conflicting_template_states_conflict_only_there(self) -> None:
        # If the two branches' separate template merges land on genuinely different
        # content for the SAME file (not just different files), combining them should
        # still produce an ordinary, isolated git conflict -- not something confused
        # or amplified by the shared-template-parent structure underneath.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point)
        main_tip_before = student.tip()

        student.checkout("feature-a", new_branch=True, start_point=main_tip_before)
        student.commit_file("a.txt", "feature a\n", "feature a work")
        run_script(student, str(tpl.path))
        tpl.commit_file("shared.txt", "template feature A\n", "template feature A")
        run_script(student, str(tpl.path))
        merge_tpl_a = student.merge("template/main")
        self.assertEqual(merge_tpl_a.returncode, 0, merge_tpl_a.stdout + merge_tpl_a.stderr)

        student.checkout("feature-b", new_branch=True, start_point=main_tip_before)
        student.commit_file("b.txt", "feature b\n", "feature b work")
        run_script(student, str(tpl.path))
        tpl.commit_file("shared.txt", "template feature B\n", "template feature B")
        run_script(student, str(tpl.path))
        merge_tpl_b = student.merge("template/main")
        self.assertEqual(merge_tpl_b.returncode, 0, merge_tpl_b.stdout + merge_tpl_b.stderr)

        student.checkout("main")
        merge1 = student.merge("feature-a")
        self.assertEqual(merge1.returncode, 0, merge1.stdout + merge1.stderr)

        merge2 = student.merge("feature-b")
        self.assertNotEqual(merge2.returncode, 0)  # a real, expected conflict

        self.assertEqual(student.diff_files("--diff-filter=U"), ["shared.txt"])
        self.assertTrue((student.path / "a.txt").exists())
        self.assertTrue((student.path / "b.txt").exists())

    # ---- --sync-feedback-branch ----

    def test_sync_feedback_branch_fails_if_current_branch_never_joined(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, origin=origin)

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no merged template history yet", result.stderr)

    def test_sync_feedback_branch_fails_cleanly_when_feedback_branch_never_existed(self) -> None:
        # With `feedback_pr: false` (see test_join_works_with_neither_setup_commit_nor_
        # feedback_pr), Classroom50 never creates a `feedback` branch at all -- there's
        # nothing to sync against, so this must fail clearly on the fetch rather than
        # crash or silently do nothing.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(
            self.tmp, accept_point, with_setup_commit=False, with_feedback_marker=False, origin=origin,
        )
        run_script(student, str(tpl.path))

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not fetch 'feedback' from 'origin'", result.stderr)

    def test_sync_feedback_branch_works_on_a_no_autograder_accept_with_no_setup_commit(self) -> None:
        # Same real scenario as test_join_works_on_a_no_autograder_accept_with_no_setup_commit
        # (a no_autograder/empty_repo assignment: no .classroom50.yaml, no classroom-init
        # commit -- feedback is frozen directly on the root commit itself). single_root_tree's
        # fallback (no SYNC_MARKER on a first sync) must still find that one root correctly
        # and sync cleanly, same as the normal with-setup-commit case.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "v1\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_setup_commit=False, origin=origin)
        run_script(student, str(tpl.path))

        tpl.commit_file("shared.txt", "v2\n", "template update")
        run_script(student, str(tpl.path))
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        student.commit_file("student_work.py", "print('mine')\n", "my own work")

        sync_and_push_feedback(student, str(tpl.path))

        student.fetch("feedback")
        diff_files = set(student.diff_files("FETCH_HEAD...main"))
        self.assertIn("student_work.py", diff_files)
        self.assertNotIn("shared.txt", diff_files)

    def test_sync_feedback_branch_fails_cleanly_on_its_own_merge_conflict(self) -> None:
        # Distinct from test_sync_feedback_branch_shows_students_conflict_resolution,
        # which covers a conflict in the STUDENT's own `git merge template/main` (on
        # main) being correctly visible afterward. This covers a conflict in
        # sync_feedback_branch's OWN internal three-way merge instead -- triggered by
        # something landing on FEEDBACK_BRANCH itself beyond Classroom50's setup
        # commits (here, simulating manual tampering) that collides with the
        # template's own change to the same file. That must fail clearly, not produce
        # a broken or silently-wrong sync.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        student.checkout("feedback")
        student.commit_file("shared.txt", "manually edited on feedback\n", "someone manually edited feedback")
        student.push("feedback")
        student.checkout("main")

        tpl.commit_file("shared.txt", "template v2\n", "template changes shared.txt")
        run_script(student, str(tpl.path))  # re-fetch
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not automatically sync", result.stderr)

    def test_sync_feedback_branch_reports_genuine_merge_base_error_distinctly(self) -> None:
        # `git merge-base --is-ancestor` returns 0 ("already in sync"), 1 ("not yet in
        # sync" -- the expected, common case needing a sync), or something else on a
        # genuine error (e.g. a corrupted/incomplete object store). The 1-vs-other
        # distinction matters: silently treating "other" the same as 1 would make the
        # script barrel ahead and build a sync commit from data it can't actually trust.
        # Reproduced here by corrupting a commit object that's a deep ancestor of
        # FEEDBACK_BRANCH's tip but is never reachable from current_branch (a past sync
        # commit, unique to feedback's own lineage) -- deleted from both the student's
        # local store and the bare "origin" so a fresh fetch can't silently heal it.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "v1\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        tpl.commit_file("shared.txt", "v2\n", "template round 1")
        run_script(student, str(tpl.path))
        student.merge("template/main")
        round1_result = sync_and_push_feedback(student, str(tpl.path))
        round1_sync_sha = re.search(r"git push \S+ (\S+):refs/heads/\S+", round1_result.stdout).group(1)

        tpl.commit_file("shared.txt", "v3\n", "template round 2")
        run_script(student, str(tpl.path))
        student.merge("template/main")
        sync_and_push_feedback(student, str(tpl.path))

        tpl.commit_file("shared.txt", "v4\n", "template round 3")
        run_script(student, str(tpl.path))
        student.merge("template/main")

        for repo_root in (student.path / ".git", origin):
            obj_path = repo_root / "objects" / round1_sync_sha[:2] / round1_sync_sha[2:]
            if obj_path.exists():
                obj_path.unlink()

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not determine whether 'feedback' is already in sync", result.stderr)

    def test_sync_feedback_branch_fails_cleanly_when_history_read_hits_a_missing_object(self) -> None:
        # Distinct from the previous test: here `merge-base --is-ancestor` succeeds
        # (it only needs to walk commit objects, so it correctly determines "not yet
        # synced" and returns 1) -- the corruption instead hits the later block that
        # reads old_feedback_head's/merge_point's actual tree content (needed to build
        # the sync commit), via a tree object missing from the store rather than a
        # commit object. That read must still fail cleanly via the try/except around
        # it, not crash with a raw traceback.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "v1\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        tpl.commit_file("shared.txt", "v2\n", "template round 1")
        run_script(student, str(tpl.path))
        student.merge("template/main")

        old_feedback_head = student.git("rev-parse", "feedback")
        tree_sha = student.git("rev-parse", f"{old_feedback_head}^{{tree}}")
        for repo_root in (student.path / ".git", origin):
            obj_path = repo_root / "objects" / tree_sha[:2] / tree_sha[2:]
            if obj_path.exists():
                obj_path.unlink()

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not read 'feedback'", result.stderr)

    def test_sync_feedback_branch_combines_non_overlapping_edits_to_the_same_file(self) -> None:
        # three_way_merge_tree uses git's real merge machinery (merge-tree, ort
        # strategy), not a purely structural tree merge -- so when FEEDBACK_BRANCH and
        # the template each edit a *different*, well-separated line of the same file,
        # this must combine both edits cleanly instead of reporting a false conflict.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "a\nb\nc\nd\ne\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        student.checkout("feedback")
        student.commit_file("shared.txt", "FEEDBACK-a\nb\nc\nd\ne\n", "feedback edits first line")
        student.push("feedback")
        student.checkout("main")

        tpl.commit_file("shared.txt", "a\nb\nc\nd\nTEMPLATE-e\n", "template edits last line")
        run_script(student, str(tpl.path))  # re-fetch
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)

        sha = sync_feedback_branch_commit(student, str(tpl.path))
        self.assertEqual(student.git("show", f"{sha}:shared.txt"), "FEEDBACK-a\nb\nc\nd\nTEMPLATE-e")

    def test_sync_feedback_branch_rejects_unrelated_two_parent_commit_on_feedback(self) -> None:
        # "Already synced before" is detected by an explicit marker in the sync
        # commit's own message, not by just checking "does the tip have 2 parents" --
        # because an ordinary 2-parent merge commit can land on FEEDBACK_BRANCH other
        # ways too (GitHub's "Update branch" button, a TA merging directly). If the
        # old parent-count-only heuristic were still in place, that commit's 2nd
        # parent would be wrongly trusted as "the template content from the last
        # sync". This must instead recognize the marker is missing and fail loudly
        # (not silently compute a wrong merge).
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        # Give feedback a unique commit of its own first, so merging main into it
        # produces a REAL merge commit rather than a fast-forward -- exactly what
        # GitHub's "Update branch" button would do.
        student.checkout("feedback")
        student.commit_file("TA_NOTE.txt", "fixed a typo\n", "TA fixes a typo directly on feedback")
        merge = student.merge("main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        self.assertEqual(len(student.parents_of_head()), 2)
        student.push("feedback")
        student.checkout("main")

        tpl.commit_file("shared.txt", "template v2\n", "template evolves")
        run_script(student, str(tpl.path))
        student.merge("template/main")

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("history doesn't look as expected", result.stderr)

    def test_sync_feedback_branch_not_blocked_by_unrelated_in_progress_merge(self) -> None:
        # The merge/rebase/cherry-pick/bisect "in progress" gate exists because
        # main() is about to run update-ref -- unsafe mid-operation. sync_feedback_
        # branch() never writes any local ref, so an unrelated in-progress merge
        # elsewhere must not block it, even though it correctly still blocks main().
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))
        tpl.commit_file("shared.txt", "template v2\n", "template evolves")
        run_script(student, str(tpl.path))
        student.merge("template/main")

        # An unrelated conflicting merge, left unresolved.
        student.checkout("other", new_branch=True)
        student.commit_file("x.txt", "branch version\n", "other branch work")
        student.checkout("main")
        student.commit_file("x.txt", "main version\n", "main work on same file")
        conflict = student.merge("other")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertTrue((student.path / ".git" / "MERGE_HEAD").exists())

        main_result = run_script(student, str(tpl.path))
        self.assertNotEqual(main_result.returncode, 0)
        self.assertIn("already in progress", main_result.stderr)

        sync_result = run_sync_feedback_branch(student, str(tpl.path))
        self.assertEqual(sync_result.returncode, 0, sync_result.stderr)

    def test_sync_feedback_branch_uses_the_one_real_remote_when_not_named_origin(self) -> None:
        # `git clone` auto-sets tracking only for the branch it checks out at clone
        # time; a locally-created branch (`git checkout -b <name>`) isn't auto-tracked,
        # so a missing branch.<name>.remote config is an expected case here, not a rare
        # one -- the "origin" fallback must actually be the student's real remote, not
        # a hardcoded guess that happens to be wrong whenever the remote has some other
        # name.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True)
        github = init_bare_repo(self.tmp / "github.git")
        student.git("remote", "add", "github", str(github))
        student.push("main", "feedback", remote="github")

        run_script(student, str(tpl.path))
        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git push github", result.stdout)

    def test_sync_feedback_branch_nothing_to_do_when_already_in_sync(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("file.txt", "hello\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, origin=origin)
        run_script(student, str(tpl.path))  # join main

        # A freshly joined feedback has never been synced at all, so "nothing to sync"
        # can't be true yet -- do one real sync first (publish it), then check that a
        # second run, with the template unchanged since, correctly says there's nothing
        # left to do.
        sync_and_push_feedback(student, str(tpl.path))

        result = run_sync_feedback_branch(student, str(tpl.path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already in sync", result.stdout)
        self.assertNotRegex(result.stdout, r"git push \S+ \S+:refs/heads/\S+")

    def test_sync_feedback_branch_push_succeeds_without_force_and_shows_student_work(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)

        run_script(student, str(tpl.path))  # join main
        tpl.commit_file("shared.txt", "template v2\n", "template adds a feature")
        run_script(student, str(tpl.path))  # re-fetch (no-op join)
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        student.commit_file("student_work.py", "print('my solution')\n", "my actual solution")

        # No --force anywhere here: a plain push succeeding (not just the command we
        # happen to run) is the proof this never needed one.
        sync_and_push_feedback(student, str(tpl.path))

        student.fetch("feedback")
        diff_files = set(student.diff_files("FETCH_HEAD...main"))
        self.assertIn("student_work.py", diff_files)
        # .classroom50.yaml is an accepted, documented false positive -- see the comment in
        # sync_feedback_branch(). shared.txt (pure template content) must NOT show up: the
        # student never touched it beyond what the template itself already changed.
        self.assertNotIn("shared.txt", diff_files)

    def test_sync_feedback_branch_shows_work_from_before_the_join_after_the_join_and_after_the_merge(self) -> None:
        # The join itself touches nothing (it only adds a second parent to whatever
        # HEAD already is) and the diff is a full-tree comparison either way, so none
        # of this should depend on exactly when a given piece of student work landed
        # relative to the join or the merge -- all three must show up regardless.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)

        student.commit_file("before_join.py", "print('before join')\n", "work done before ever joining")
        result = run_script(student, str(tpl.path))  # the first-ever join
        self.assertEqual(result.returncode, 0, result.stderr)

        student.commit_file("after_join.py", "print('after join')\n", "work done after joining, still before any merge")

        # Only now does the template move on and get merged in.
        tpl.commit_file("shared.txt", "template v2\n", "template adds a feature")
        run_script(student, str(tpl.path))  # re-fetch
        merge = student.merge("template/main")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)

        student.commit_file("after_merge.py", "print('after merge')\n", "work done after merging")

        # Only checking this computed commit's own content, not the remote afterward
        # -- no need to actually publish it.
        sha = sync_feedback_branch_commit(student, str(tpl.path))
        diff_files = set(student.diff_files(f"{sha}...main"))
        self.assertIn("before_join.py", diff_files)
        self.assertIn("after_join.py", diff_files)
        self.assertIn("after_merge.py", diff_files)

    def test_sync_feedback_branch_shows_students_conflict_resolution(self) -> None:
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "base\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)

        run_script(student, str(tpl.path))
        tpl.commit_file("shared.txt", "template v2\n", "template changes shared.txt")
        run_script(student, str(tpl.path))
        student.commit_file("shared.txt", "student's own version\n", "student edits shared.txt (will conflict)")
        merge = student.merge("template/main")
        self.assertNotEqual(merge.returncode, 0)  # real conflict, as intended
        student.write_file("shared.txt", "student's hand-resolved blend\n")
        student.git("add", "shared.txt")
        student.git("commit", "-q", "--no-edit")

        # Only checking this computed commit's own content, not the remote afterward
        # -- no need to actually publish it.
        sha = sync_feedback_branch_commit(student, str(tpl.path))
        # The student's actual resolution choice must be visible in the Feedback PR's
        # diff, not hidden behind the template's raw (unresolved) content.
        diff = student.git("diff", f"{sha}...main", "--", "shared.txt")
        self.assertIn("student's hand-resolved blend", diff)

    def test_sync_feedback_branch_handles_three_merge_rounds(self) -> None:
        # Subsumes the two-round scenario that originally pinned the fix for the bug
        # this is named after (one root commit assumed, breaks on the second sync) --
        # going one round further since "fixed for round 2" doesn't by itself prove
        # the fix generalizes to any N (a subtly wrong fix could plausibly resurface a
        # new failure only at round 3+, e.g. if old_feedback_head's parent count ever
        # grew instead of staying at exactly 2 after each sync). The explicit ls-tree
        # check at each round matters because diff_files alone can't tell the
        # difference: a three-dot diff's base always resolves to the template's own
        # raw commit (see sync_feedback_branch's docstring), so the accept-time files
        # would show up as "changed" there whether or not they're actually preserved
        # on FEEDBACK_BRANCH's own tree -- only ls-tree can confirm the latter.
        tpl = TemplateBuilder(self.tmp / "template")
        accept_point = tpl.commit_file("shared.txt", "v1\n", "initial")
        origin = init_bare_repo(self.tmp / "origin.git")
        student = make_accepted_student_repo(self.tmp, accept_point, with_feedback_marker=True, origin=origin)
        run_script(student, str(tpl.path))

        for i in (1, 2, 3):
            tpl.commit_file("shared.txt", f"v{i + 1}\n", f"template round {i}")
            run_script(student, str(tpl.path))
            merge = student.merge("template/main")
            self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
            student.commit_file(f"work{i}.py", f"print({i})\n", f"student work round {i}")

            result = sync_and_push_feedback(student, str(tpl.path))
            self.assertNotIn("already in sync", result.stdout)

            student.fetch("feedback")
            diff_files = set(student.diff_files("FETCH_HEAD...main"))
            expected = {".classroom50.yaml", ".github/workflows/autograde.yaml"} | \
                {f"work{j}.py" for j in range(1, i + 1)}
            self.assertEqual(diff_files, expected, f"round {i}")
            self.assertEqual(student.git("show", "FETCH_HEAD:shared.txt"), f"v{i + 1}", f"round {i}")
            self.assertEqual(len(student.git("log", "-1", "--format=%P", "FETCH_HEAD").split()), 2,
                              f"round {i}: feedback tip should always have exactly 2 parents")
            tree_paths = student.git("ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines()
            self.assertIn(".classroom50.yaml", tree_paths,
                          f"round {i}: .classroom50.yaml must still be literally on FEEDBACK_BRANCH's own tree")
            self.assertIn(".github/workflows/autograde.yaml", tree_paths,
                          f"round {i}: autograde.yaml must still be literally on FEEDBACK_BRANCH's own tree")

        self.assertIn("already in sync", sync_and_push_feedback(student, str(tpl.path)).stdout)


if __name__ == "__main__":
    unittest.main()
