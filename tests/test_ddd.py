"""Tests for skills/ddd/scripts/ddd.py.

Run from the repo root:  python -m unittest discover -s tests -v

- MatchingEvals runs every scenario in evals/matching/ (before.md, after.md,
  expect.json); see evals/README.md for the format.
- ParserTests covers the doc conventions in references/doc-format.md.
- WorkflowTests drives the CLI end to end in a throwaway git repo: sync,
  rank, start, commit, finish, status write, merge and squash-merge detection,
  plus `survey` and the Initialize draft structure.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "ddd", "scripts", "ddd.py")
EVALS = os.path.join(REPO, "evals", "matching")
sys.path.insert(0, os.path.dirname(SCRIPT))

import ddd  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "ddd-test", "GIT_AUTHOR_EMAIL": "ddd@example.invalid",
           "GIT_COMMITTER_NAME": "ddd-test", "GIT_COMMITTER_EMAIL": "ddd@example.invalid"}


def labels(doc):
    """Map 'Title' and 'Parent > Title' labels to parsed items."""
    by_key = {it["key"]: it for it in doc["items"]}
    out = {}
    for it in doc["items"]:
        parent = by_key.get(it["parent"]) if it["parent"] else None
        out.setdefault(it["title"], []).append(it)
        if parent:
            out.setdefault("%s > %s" % (parent["title"], it["title"]), []).append(it)
    return out


def fresh_state():
    root = tempfile.mkdtemp(prefix="ddd-eval-")
    os.makedirs(os.path.join(root, ".ddd", "tmp"))
    return ddd.State(root)


def sync(state, text):
    doc = ddd.parse_doc(text, state.status_title())
    result = ddd.compute_diff(state.items, doc)
    return doc, result


class MatchingEvals(unittest.TestCase):
    def test_scenarios(self):
        names = sorted(n for n in os.listdir(EVALS) if os.path.isdir(os.path.join(EVALS, n)))
        self.assertGreater(len(names), 10)
        for name in names:
            with self.subTest(scenario=name):
                self.run_scenario(name)

    def run_scenario(self, name):
        d = os.path.join(EVALS, name)
        with open(os.path.join(d, "expect.json"), encoding="utf-8") as f:
            exp = json.load(f)
        before = ddd.read_text(os.path.join(d, "before.md"))
        after = ddd.read_text(os.path.join(d, "after.md"))
        st = fresh_state()
        try:
            doc0, res0 = sync(st, before)
            ddd.apply_diff(st, doc0, res0, before)
            doc, res = sync(st, after)
            rep, matches = res["report"], res["matches"]
            lab = labels(doc)

            def item(label):
                self.assertIn(label, lab, "no item labelled %r" % label)
                self.assertEqual(len(lab[label]), 1, "label %r is ambiguous" % label)
                return lab[label][0]

            for label, iid in exp.get("matched", {}).items():
                m = matches.get(item(label)["key"])
                self.assertIsNotNone(m, "%r should match %s" % (label, iid))
                self.assertEqual(m["id"], iid)
                self.assertTrue(m["confident"], "%r -> %s should not need confirmation" % (label, iid))
            changed = {c["key"]: c["types"] for c in rep["changed"]}
            for label, types in exp.get("changes", {}).items():
                got = changed.get(item(label)["key"], [])
                for t in types:
                    self.assertIn(t, got, "%r should be %s (got %s)" % (label, t, got))
            if "new" in exp:
                self.assertEqual(sorted(n["title"] for n in rep["new"]), sorted(exp["new"]))
            if "removed" in exp:
                self.assertEqual(sorted(r["id"] for r in rep["removed"]), sorted(exp["removed"]))
            asks = {(p["key"], p["id"]) for p in rep["proposed"]} | \
                   {(p["key"], p["id"]) for p in rep["possible"]}
            for a in exp.get("ask", []):
                self.assertIn((item(a["label"])["key"], a["id"]), asks,
                              "agent should ask whether %r is %s" % (a["label"], a["id"]))
            if exp.get("ask") == []:
                self.assertFalse(rep["proposed"], "unexpected uncertain matches: %s" % rep["proposed"])
                self.assertFalse(rep["possible"], "unexpected hints: %s" % rep["possible"])
            for s in exp.get("splits", []):
                self.assertIn((s["from"], item(s["label"])["key"]),
                              {(x["from_id"], x["key"]) for x in rep["splits"]})
            for iid in exp.get("no_hint_for", []):
                self.assertNotIn(iid, [p["id"] for p in rep["possible"]])
            if exp.get("no_changes"):
                self.assertEqual(rep["changed"], [])
                self.assertEqual(rep["summary"]["unchanged"], len(doc["items"]))
            if exp.get("same_hash"):
                self.assertEqual(doc0["hash"], doc["hash"])
            aa = exp.get("after_accept")
            if aa:
                key_to_id = ddd.apply_diff(st, doc, res, after)
                for label, iid in aa.get("ids", {}).items():
                    self.assertEqual(key_to_id[item(label)["key"]], iid)
                for iid, aliases in aa.get("aliases", {}).items():
                    self.assertEqual(st.items[iid].get("aliases"), aliases)
                for label, state in aa.get("states", {}).items():
                    self.assertEqual(st.items[key_to_id[item(label)["key"]]]["state"], state)
        finally:
            shutil.rmtree(st.root, ignore_errors=True)


class ParserTests(unittest.TestCase):
    def parse(self, text):
        return {it["title"]: it for it in ddd.parse_doc(text)["items"]}

    def test_markers_tags_and_inheritance(self):
        items = self.parse("# Plan\n\n## Auth {#auth} [next]\n\n- [ ] Login\n- [x] Logout\n"
                           "\n## Dark mode [blocked]\n\n## Ideas [notes]\n\n- [ ] CLI\n")
        self.assertEqual(items["Auth"]["tag"], "auth")
        self.assertEqual(items["Auth"]["markers"], ["next"])
        self.assertEqual(items["Login"]["inherited"], ["next"])
        self.assertTrue(items["Logout"]["checked"])
        self.assertEqual(items["Dark mode"]["markers"], ["blocked"])
        self.assertEqual(items["CLI"]["inherited"], ["notes"])

    def test_acceptance_and_nested_tasks(self):
        items = self.parse("## Export\n\nDone when: CSV downloads.\n\n- [ ] Button\n"
                           "  - Done when: it is visible\n  - [ ] Icon\n")
        self.assertEqual(items["Export"]["acceptance"], ["CSV downloads."])
        self.assertEqual(items["Button"]["acceptance"], ["it is visible"])
        self.assertEqual(items["Icon"]["parent"], items["Button"]["key"])

    def test_code_fences_are_body(self):
        items = self.parse("## Setup\n\n```\n# not a heading\n- [ ] not a task\n```\n")
        self.assertEqual(list(items), ["Setup"])

    def test_duplicate_tag_warns(self):
        doc = ddd.parse_doc("## A {#x}\n\n## B {#x}\n")
        self.assertEqual(len(doc["warnings"]), 1)
        self.assertIsNone(doc["items"][1]["tag"])

    def test_status_section_excluded_from_items_and_hash(self):
        base = "## A\n\nbody\n"
        with_status = base + "\n## DDD Status\n\n- [ ] not an item\n"
        self.assertEqual([i["title"] for i in ddd.parse_doc(with_status)["items"]], ["A"])
        self.assertEqual(ddd.doc_hash(base, "DDD Status"), ddd.doc_hash(with_status, "DDD Status"))

    def test_status_section_in_middle_ends_at_next_heading(self):
        text = "## A\n\n## DDD Status\n\n- stuff\n\n## B\n"
        self.assertEqual([i["title"] for i in ddd.parse_doc(text)["items"]], ["A", "B"])

    def test_replace_status_keeps_rest_of_doc(self):
        text = "## A\n\nbody\n\n## DDD Status\n\nold\n\n## B\n\nmore\n"
        out = ddd.replace_status(text, "## DDD Status\n\nnew\n", "DDD Status")
        self.assertIn("new", out)
        self.assertNotIn("old", out)
        self.assertTrue(out.rstrip().endswith("more"))
        appended = ddd.replace_status("## A\n", "## DDD Status\n\nnew\n", "DDD Status")
        self.assertEqual(appended, "## A\n\n## DDD Status\n\nnew\n")


class WorkflowTests(unittest.TestCase):
    PLAN = ("# Plan\n\n## Sign-in {#auth} [next]\n\n- [ ] Login form {#login}\n"
            "  - Done when: bad passwords show an error\n- [ ] Logout button\n\n## Dark mode\n\nTheme toggle.\n")

    def setUp(self):
        self.env = dict(os.environ, **GIT_ENV)
        self.root = tempfile.mkdtemp(prefix="ddd-wf-")
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.write("plan.md", self.PLAN)
        self.write("app.txt", "v0\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *argv):
        out = subprocess.run(["git"] + list(argv), cwd=self.root, env=self.env, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return out.stdout.decode("utf-8", "replace").strip()

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def ddd(self, *argv, code=0):
        out = subprocess.run([sys.executable, SCRIPT] + list(argv), cwd=self.root, env=self.env,
                             capture_output=True)
        text = out.stdout.decode("utf-8")
        self.assertEqual(out.returncode, code, text + out.stderr.decode("utf-8", "replace"))
        return json.loads(text)

    def init_and_sync(self):
        self.ddd("init", "--doc", "plan.md", "--test", "true")
        self.ddd("diff")
        self.ddd("accept")

    def build(self, iid, content):
        started = self.ddd("start", iid)
        self.write("app.txt", content)
        self.git("add", "app.txt")
        self.ddd("commit", iid, "-m", "Build %s" % iid)
        return started

    def test_full_cycle_merge(self):
        self.init_and_sync()
        self.assertEqual(self.ddd("config", "get", "base_branch")["value"], "main")
        queued = self.ddd("rank", "--queued")["candidates"]
        self.assertEqual([c["id"] for c in queued], ["login", "logout-button"])

        started = self.build("login", "v1\n")
        self.assertEqual(started["branch"], "ddd/login")
        self.assertEqual(started["acceptance"], ["bad passwords show an error"])
        self.assertIn("DDD-Item: login", self.git("log", "-1", "--format=%B"))
        self.ddd("finish", "login", code=1)  # no --tests-passed
        fin = self.ddd("finish", "login", "--tests-passed", "true")
        self.assertEqual(fin["now_on"], "main")
        self.assertEqual(self.ddd("show", "login")["state"], "awaiting-review")
        # .ddd/ was never committed on the item branch
        self.assertEqual(self.git("diff", "--name-only", "main", "ddd/login"), "app.txt")

        self.assertTrue(self.ddd("commit-state")["committed"])
        self.assertEqual(self.ddd("sync-git")["merged"], [])
        self.git("merge", "-q", "--no-ff", "ddd/login", "-m", "merge login")
        merged = self.ddd("sync-git")["merged"]
        self.assertEqual([m["id"] for m in merged], ["login"])
        self.assertEqual(self.ddd("show", "login")["state"], "done")

    def test_squash_merge_is_detected_by_trailer(self):
        self.init_and_sync()
        self.build("logout-button", "v2\n")
        self.ddd("finish", "logout-button", "--tests-passed", "true")
        self.ddd("commit-state")
        self.git("merge", "-q", "--squash", "ddd/logout-button")
        self.git("commit", "-qm", "Squashed\n\nDDD-Item: logout-button")
        merged = self.ddd("sync-git")["merged"]
        self.assertEqual(merged[0]["how"], "squash-merged")

    def test_stacked_item_not_done_when_parent_merges(self):
        """An item built --from another item's branch must not count as merged
        when only the parent item is merged."""
        self.init_and_sync()
        self.build("login", "v1\n")
        self.ddd("finish", "login", "--tests-passed", "true")
        self.ddd("start", "logout-button", "--from", "ddd/login")
        self.write("app.txt", "v1+logout\n")
        self.git("add", "app.txt")
        self.ddd("commit", "logout-button", "-m", "Build logout")
        fin = self.ddd("finish", "logout-button", "--tests-passed", "true")
        self.assertEqual(len(fin["commits"]), 1)
        self.ddd("commit-state")
        self.git("merge", "-q", "--no-ff", "ddd/login", "-m", "merge login only")
        merged = [m["id"] for m in self.ddd("sync-git")["merged"]]
        self.assertEqual(merged, ["login"])
        self.assertEqual(self.ddd("show", "logout-button")["state"], "awaiting-review")

    def test_trailer_match_is_exact(self):
        self.init_and_sync()
        self.build("login", "v1\n")
        self.ddd("finish", "login", "--tests-passed", "true")
        self.ddd("commit-state")
        self.write("app.txt", "other\n")
        self.git("commit", "-qam", "Unrelated\n\nDDD-Item: login-extra")
        rec = json.loads(self.read(".ddd/items.json"))
        rec["items"]["login"]["commits"] = []  # force the trailer path
        self.write(".ddd/items.json", json.dumps(rec))
        self.assertEqual(self.ddd("sync-git")["merged"], [])

    def test_state_floats_across_branches(self):
        """Resuming an old item branch after state was committed on main keeps the newest state."""
        self.init_and_sync()
        self.build("login", "v1\n")
        self.ddd("finish", "login", "--tests-passed", "true")
        self.ddd("commit-state")  # main now tracks .ddd/
        self.ddd("set", "dark-mode", "--note", "made on main")
        self.ddd("checkout", "ddd/login")  # branch forked before .ddd/ was tracked
        self.assertIn("made on main", self.read(".ddd/items.json"))
        self.ddd("checkout", "main")
        self.assertIn("made on main", self.read(".ddd/items.json"))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_auto_builds_second_item_after_status_write(self):
        """Regression (found in a live `ddd auto` run): writing the status section into a
        git-tracked doc made the tree dirty, so `start` refused every item after the first."""
        self.init_and_sync()
        self.build("login", "v1\n")
        self.ddd("finish", "login", "--tests-passed", "true")
        self.ddd("status-block", "--write", "plan.md")
        self.ddd("commit-state")
        started = self.build("logout-button", "v2\n")
        self.assertEqual(started["branch"], "ddd/logout-button")
        # the status section floats with the working tree and was never committed on the item branch
        self.assertIn("## DDD Status", self.read("plan.md"))
        self.assertNotIn("plan.md", self.git("diff", "--name-only", "main", "ddd/logout-button"))
        self.ddd("finish", "logout-button", "--tests-passed", "true")
        self.assertIn("## DDD Status", self.read("plan.md"))

    def test_developer_doc_edits_survive_branch_switches(self):
        """The developer edits the plan (uncommitted) while auto mode switches branches."""
        self.init_and_sync()
        self.build("login", "v1\n")
        self.ddd("finish", "login", "--tests-passed", "true")
        self.ddd("commit-state")
        self.write("plan.md", self.read("plan.md") + "\n## Billing\n\nStripe checkout.\n")
        self.git("commit", "-qam", "developer commits a doc change on main")
        self.write("plan.md", self.read("plan.md").replace("Theme toggle.", "Theme toggle, uncommitted."))
        self.ddd("checkout", "ddd/login")  # branch has the older committed doc
        self.assertIn("uncommitted", self.read("plan.md"))
        self.assertIn("Billing", self.read("plan.md"))
        self.ddd("checkout", "main")
        self.assertIn("uncommitted", self.read("plan.md"))
        self.assertEqual(self.git("diff", "--name-only"), "plan.md")

    def test_untracked_files_do_not_block_start(self):
        self.init_and_sync()
        os.makedirs(os.path.join(self.root, "__pycache__"))
        self.write("__pycache__/x.pyc", "junk")
        self.ddd("start", "login")
        self.write("app.txt", "v1\n")
        self.write("new_module.txt", "forgot to add me\n")
        self.git("add", "app.txt")
        self.ddd("commit", "login", "-m", "Build login")
        fin = self.ddd("finish", "login", "--tests-passed", "true")
        self.assertIn("new_module.txt", fin["untracked"])  # surfaced, so a forgotten file is noticed

    def test_start_refuses_dirty_tree(self):
        self.init_and_sync()
        self.write("app.txt", "uncommitted\n")
        out = self.ddd("start", "login", code=2)
        self.assertIn("app.txt", " ".join(out["dirty"]))

    def test_commit_refuses_staged_state(self):
        self.init_and_sync()
        self.ddd("start", "login")
        self.write("app.txt", "v1\n")
        self.git("add", "app.txt", ".ddd")
        self.ddd("commit", "login", "-m", "x", code=1)

    def test_uncertain_match_blocks_accept(self):
        self.init_and_sync()
        self.write("plan.md", self.PLAN.replace("## Dark mode\n\nTheme toggle.",
                                                "## Night theme\n\nTheme toggle."))
        rep = self.ddd("diff")
        self.assertTrue(rep["needs_input"])
        key = rep["proposed"][0]["key"]
        out = self.ddd("accept", code=2)
        self.assertEqual(out["proposed"][0]["id"], "dark-mode")
        self.ddd("accept", "--match", "%s=dark-mode" % key)
        self.assertEqual(self.ddd("show", "dark-mode")["title"], "Night theme")

    def test_reject_proposed_match(self):
        self.init_and_sync()
        self.write("plan.md", self.PLAN.replace("## Dark mode\n\nTheme toggle.",
                                                "## Night theme\n\nTheme toggle."))
        key = self.ddd("diff")["proposed"][0]["key"]
        self.ddd("accept", "--new", key)
        self.assertEqual(self.ddd("show", "dark-mode")["state"], "removed")
        self.assertEqual(self.ddd("show", "night-theme")["state"], "todo")

    def test_status_write_guards_against_concurrent_edits(self):
        self.init_and_sync()
        self.ddd("start", "login")
        self.ddd("checkout", "main")
        out = self.ddd("status-block", "--write", "plan.md")
        self.assertIn("Login form {#login} - branch `ddd/login`", self.read("plan.md"))
        self.assertIn("**In progress**", out["block"])
        # status writes never count as doc edits
        self.assertTrue(self.ddd("diff")["doc_unchanged"])
        # a developer edit since the last sync blocks the write
        self.write("plan.md", self.read("plan.md").replace("Theme toggle.", "Theme toggle!"))
        self.ddd("status-block", "--write", "plan.md", code=3)

    def test_status_write_keeps_crlf(self):
        self.write("plan.md", self.PLAN.replace("\n", "\r\n"))
        self.init_and_sync()
        self.ddd("status-block", "--write", "plan.md")
        with open(os.path.join(self.root, "plan.md"), "rb") as f:
            raw = f.read()
        self.assertIn(b"## DDD Status", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_changed_after_build_is_a_conflict(self):
        self.init_and_sync()
        self.build("login", "v1\n")
        self.ddd("finish", "login", "--tests-passed", "true")
        self.write("plan.md", self.PLAN.replace("Login form {#login}", "Login form with SSO {#login}"))
        rep = self.ddd("diff")
        self.assertEqual([c["type"] for c in rep["conflicts"]], ["changed-after-build"])
        self.ddd("accept")
        self.assertIn("changed-after-build", self.ddd("show", "login")["flags"])

    def test_removed_while_active_is_kept(self):
        self.init_and_sync()
        self.ddd("start", "logout-button")
        self.ddd("checkout", "main")
        self.write("plan.md", self.PLAN.replace("- [ ] Logout button\n", ""))
        rep = self.ddd("diff")
        self.assertEqual(rep["conflicts"][0]["type"], "removed-while-active")
        self.ddd("accept")
        rec = self.ddd("show", "logout-button")
        self.assertEqual(rec["state"], "in-progress")
        self.assertIn("removed-from-doc", rec["flags"])

    def test_gdoc_requires_fetched_text(self):
        self.ddd("init", "--doc", "https://docs.google.com/document/d/abc123_X/edit", "--force")
        self.assertEqual(self.ddd("info")["config"]["doc"]["id"], "abc123_X")
        self.ddd("diff", code=1)
        os.makedirs(os.path.join(self.root, ".ddd", "tmp"), exist_ok=True)
        self.write(".ddd/tmp/doc.md", self.PLAN)
        self.assertEqual(self.ddd("diff", ".ddd/tmp/doc.md")["summary"]["new"], 5)

    def test_survey_reports_project_facts(self):
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.root, "tests"))
        os.makedirs(os.path.join(self.root, "node_modules", "dep"))
        os.makedirs(os.path.join(self.root, ".github", "workflows"))
        self.write("package.json", '{"name": "app", "scripts": {"test": "jest", "build": "tsc"}}')
        self.write("src/app.js", "// TODO: handle empty input\nlet TODO_LIST = [];\n// FIXME(sam) leaks\n")
        self.write("tests/app.test.js", "test('x', () => {});\n")
        self.write("node_modules/dep/index.js", "// TODO: vendored, must be skipped\n")
        self.write(".github/workflows/ci.yml", "on: push\n")
        self.write("ROADMAP.md", "# Roadmap\n")
        self.write("README.md", "# App\n")
        with open(os.path.join(self.root, "blob.bin"), "wb") as f:
            f.write(b"\0TODO: binary\n")
        self.write(".gitignore", "ignored/\n")
        os.makedirs(os.path.join(self.root, "ignored"))
        self.write("ignored/x.js", "// TODO: gitignored\n")
        self.git("checkout", "-q", "-b", "wip")
        self.git("commit", "-q", "--allow-empty", "-m", "wip")
        self.git("checkout", "-q", "main")
        s = self.ddd("survey")
        self.assertFalse(s["ddd_initialised"])
        self.assertEqual(s["git"]["branch"], "main")
        self.assertEqual(s["git"]["unmerged_branches"], ["wip"])
        self.assertIn("package.json", s["manifests"])
        self.assertEqual(s["package_scripts"], ["build", "test"])
        self.assertIn("tests/app.test.js", s["tests"]["sample"])
        self.assertEqual(s["ci"], [".github/workflows/ci.yml"])
        self.assertIn("README.md", s["readme"])
        self.assertIn("ROADMAP.md", s["plan_candidates"])
        self.assertIn("plan.md", s["plan_candidates"])
        found = [(m["path"], m["line"], m["tag"], m["text"]) for m in s["code_markers"]["items"]]
        self.assertEqual(found, [("src/app.js", 1, "TODO", "handle empty input"),
                                 ("src/app.js", 3, "FIXME", "leaks")])
        self.assertFalse(any(p.startswith(("node_modules/", "ignored/")) for p in s["top_level"]))
        self.ddd("init", "--doc", "plan.md")
        self.assertTrue(self.ddd("survey")["ddd_initialised"])

    def test_survey_without_git(self):
        plain = tempfile.mkdtemp(prefix="ddd-nogit-")
        try:
            os.makedirs(os.path.join(plain, "venv"))
            with open(os.path.join(plain, "main.py"), "w", encoding="utf-8") as f:
                f.write("# HACK: temporary\n")
            with open(os.path.join(plain, "venv", "lib.py"), "w", encoding="utf-8") as f:
                f.write("# TODO: skipped\n")
            out = subprocess.run([sys.executable, SCRIPT, "--root", plain, "survey"], capture_output=True)
            s = json.loads(out.stdout.decode("utf-8"))
            self.assertFalse(s["git"]["repo"])
            self.assertEqual(s["files"], 1)
            self.assertEqual(s["code_markers"]["total"], 1)
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_initialize_draft_structure_syncs(self):
        """The plan structure in references/initialize.md parses as the guide
        promises: context sections are notes, built parts start done, and only
        planned work is offered."""
        guide = ddd.read_text(os.path.join(REPO, "skills", "ddd", "references", "initialize.md"))
        draft = guide.split("```markdown\n", 1)[1].split("\n```", 1)[0] + "\n"
        doc = ddd.parse_doc(draft)
        self.assertEqual(doc["warnings"], [])
        items = {it["title"]: it for it in doc["items"]}
        for section in ("Overview", "Current features", "Open questions", "Ideas"):
            self.assertIn("notes", items[section]["markers"], section)
        tagged = next(it for it in doc["items"] if it["tag"] == "task-tag")
        self.assertEqual(tagged["acceptance"], ["<observable, testable result>"])
        by_key = {it["key"]: it for it in doc["items"]}
        self.assertEqual(by_key[tagged["parent"]]["tag"], "feature-tag")
        os.makedirs(os.path.join(self.root, "docs"))
        self.write("docs/plan.md", draft)
        self.ddd("init", "--doc", "docs/plan.md", "--force")
        self.assertFalse(self.ddd("diff")["needs_input"])
        self.ddd("accept")
        built = [r for r in self.ddd("items")["items"] if r["title"] == "<Part that already exists>"]
        self.assertEqual([r["state"] for r in built], ["done"])
        offered = {c["title"] for c in self.ddd("rank", "--limit", "50")["candidates"]}
        self.assertIn("<Task>", offered)
        self.assertTrue(any(t.startswith("<Fix") for t in offered))
        self.assertFalse(offered & {"Overview", "Current features", "Open questions", "Ideas"})
        self.assertEqual(self.ddd("rank", "--queued")["candidates"], [])


if __name__ == "__main__":
    unittest.main()
