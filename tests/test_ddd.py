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

    def test_tag_line_under_heading(self):
        items = self.parse("## Finish Clash\n\n{#clash} [next]\n\nWhy.\n\n- [ ] Kits\n\n"
                           "## Cards\n{\\#cards} \\[blocked\\]\nBody\n")
        self.assertEqual(items["Finish Clash"]["tag"], "clash")
        self.assertEqual(items["Finish Clash"]["markers"], ["next"])
        self.assertEqual(items["Finish Clash"]["body"], "Why.")
        self.assertEqual(items["Kits"]["inherited"], ["next"])
        self.assertEqual((items["Cards"]["tag"], items["Cards"]["markers"]), ("cards", ["blocked"]))

    def test_tag_line_only_right_under_heading(self):
        items = self.parse("## A\n\nBody first.\n\n{#late}\n\n## B\n\n- [ ] Task\n\n{#also-late}\n")
        self.assertIsNone(items["A"]["tag"])
        self.assertIn("{#late}", items["A"]["body"])
        self.assertIsNone(items["B"]["tag"])

    def test_tag_line_keeps_heading_tag(self):
        doc = ddd.parse_doc("## A {#first}\n\n{#second} [notes]\n")
        self.assertEqual(doc["items"][0]["tag"], "first")
        self.assertEqual(doc["items"][0]["markers"], ["notes"])
        self.assertEqual(len(doc["warnings"]), 1)

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


class DocInputTests(unittest.TestCase):
    """Doc text from files the agent did not have to retype: saved connector
    results and base64 exports."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ddd-input-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def test_inputs(self):
        md = "# Plan\n\n## A ✓\n\n- [ ] Task\n"
        b64 = ddd.base64.b64encode(md.encode("utf-8")).decode("ascii")
        cases = {
            "plain.md": md,
            "doc.b64": b64[:20] + "\n" + b64[20:] + "\n",
            "result.json": json.dumps({"content": b64, "mimeType": "text/markdown"}),
            "wrapped.json": json.dumps([{"type": "text", "text": json.dumps({"content": b64})}]),
            "text.json": json.dumps({"content": md}),
            "string.json": json.dumps(json.dumps({"content": b64})),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertEqual(ddd.read_doc_input(self.path(name, text)), md)

    def test_markdown_that_looks_like_json_is_kept(self):
        md = "[notes] intro\n\n## A\n"
        self.assertEqual(ddd.read_doc_input(self.path("doc.md", md)), md)

    def test_bad_base64(self):
        with self.assertRaises(ddd.DddError):
            ddd.read_doc_input(self.path("doc.b64", "not base64!"))


class GoogleImportTests(unittest.TestCase):
    """Real Google Docs round trips, see tests/fixtures/gdoc-import/README.md."""
    FIX = os.path.join(REPO, "tests", "fixtures", "gdoc-import")

    def load(self, name):
        return ddd.parse_doc(ddd.read_doc_input(os.path.join(self.FIX, name)))

    def test_heading_tags_are_cut_but_tag_lines_survive(self):
        source, export = self.load("placements.source.md"), self.load("placements.export.json")
        src = {it["title_norm"]: it for it in source["items"]}
        out = {it["title_norm"]: it for it in export["items"]}
        for title in ("alpha trailing", "beta before marker", "delta escaped", "iota trailing space"):
            self.assertTrue(src[title]["tag"])
            self.assertIsNone(out[title]["tag"], title)
        self.assertEqual(out["beta before marker"]["markers"], [])  # the marker after the tag is gone too
        self.assertEqual(out["epsilon tag line below"]["tag"], "epsilon")
        self.assertEqual(out["theta marker only"]["markers"], ["blocked"])
        tasks = {it["title"]: it for it in export["items"] if it["kind"] == "task"}
        self.assertEqual(tasks["Task with tag"]["tag"], "mu-task")
        self.assertEqual(tasks["Task with tag"]["acceptance"], ["it works"])
        self.assertTrue(tasks["Done task"]["checked"])

    def test_recommended_layout_round_trips(self):
        result = ddd.compare_docs(self.load("layout.source.md"), self.load("layout.export.json"))
        self.assertTrue(result["same"], result)
        tags = {it["tag"] for it in self.load("layout.export.json")["items"] if it["tag"]}
        self.assertEqual(tags, {"fixes", "fix-sims", "fixed-thing", "clash", "clash-kits", "clash-cards"})


class FakeDoc:
    """A model of one Docs tab, enough to apply the requests status-block emits
    and read the result back as documents.get JSON. It encodes the API rules the
    requests rely on (UTF-16 indexes from 1, a paragraph's style on its newline,
    a split paragraph's halves both keeping its style, the final newline
    undeletable). It checks the arithmetic, not Google: the live check against
    a real doc is eval 14 (gdoc-status-write-docs-connector) in evals/skill/evals.json.
    """

    def __init__(self, paras, tab="t.0", revision="rev-1"):
        self.tab, self.revision = tab, revision
        self.units, self.attrs = [], []
        for p in paras:
            text, style, bullet = (p + (False,))[:3] if isinstance(p, tuple) else (p, "NORMAL_TEXT", False)
            for u in self.encode(text):
                self.units.append(u)
                self.attrs.append(None)
            self.units.append("\n")
            self.attrs.append({"style": style, "bullet": bullet, "suggested": False})

    @staticmethod
    def encode(text):
        data = text.encode("utf-16-le")
        return [data[i:i + 2] for i in range(0, len(data), 2)]

    def paragraphs(self):
        out, start, buf = [], 1, []
        for i, (u, a) in enumerate(zip(self.units, self.attrs)):
            if a is None:
                buf.append(u)
                continue
            text = b"".join(buf).decode("utf-16-le")
            out.append((start, i + 2, text, a))
            start, buf = i + 2, []
        return out

    def body_end(self):
        return len(self.units) + 1

    def para_at(self, index):
        for i in range(index - 1, len(self.units)):
            if self.attrs[i] is not None:
                return i
        raise AssertionError("index %d is past the end" % index)

    def check_tab(self, where):
        assert where.get("tabId") == self.tab, where

    def apply(self, args):
        assert args["writeControl"]["requiredRevisionId"] == self.revision, "stale revision"
        for req in args["requests"]:
            (kind, body), = req.items()
            getattr(self, kind)(body)
        self.revision += "+"

    def deleteContentRange(self, body):
        r = body["range"]
        self.check_tab(r)
        a, b = r["startIndex"], r["endIndex"]
        assert 1 <= a < b <= len(self.units), "invalid deletion range %d-%d" % (a, b)
        del self.units[a - 1:b - 1]
        del self.attrs[a - 1:b - 1]

    def insertText(self, body):
        loc = body["location"]
        self.check_tab(loc)
        at = loc["index"]
        assert 1 <= at <= len(self.units), "insert outside the body"
        style = dict(self.attrs[self.para_at(at)])
        new_units, new_attrs = [], []
        for ch in body["text"]:
            for u in ([ch] if ch == "\n" else self.encode(ch)):
                new_units.append(u)
                new_attrs.append(dict(style) if ch == "\n" else None)
        self.units[at - 1:at - 1] = new_units
        self.attrs[at - 1:at - 1] = new_attrs

    def paras_in(self, r):
        self.check_tab(r)
        a, b = r["startIndex"], r["endIndex"]
        assert 1 <= a < b <= self.body_end(), "range %d-%d outside the body" % (a, b)
        return [self.attrs[i] for i in range(self.para_at(a), self.para_at(b - 1) + 1)
                if self.attrs[i] is not None]

    def updateParagraphStyle(self, body):
        for a in self.paras_in(body["range"]):
            a["style"] = body["paragraphStyle"].get("namedStyleType", "NORMAL_TEXT")

    def createParagraphBullets(self, body):
        for a in self.paras_in(body["range"]):
            a["bullet"] = True

    def deleteParagraphBullets(self, body):
        for a in self.paras_in(body["range"]):
            a["bullet"] = False

    def updateTextStyle(self, body):
        r = body["range"]
        self.check_tab(r)
        assert 1 <= r["startIndex"] < r["endIndex"] <= self.body_end() - 1, r

    def read(self):
        content = [{"endIndex": 1, "sectionBreak": {}}]
        for start, end, text, a in self.paragraphs():
            run = {"content": text + "\n", "textStyle": {"bold": True} if a["style"] != "NORMAL_TEXT" else {}}
            if a.get("suggested"):
                run["suggestedInsertionIds"] = ["s1"]
            para = {"elements": [{"startIndex": start, "endIndex": end, "textRun": run}],
                    "paragraphStyle": {"namedStyleType": a["style"]}}
            if a["bullet"]:
                para["bullet"] = {"listId": "l1"}
            content.append({"startIndex": start, "endIndex": end, "paragraph": para})
        return {"documentId": "doc1", "revisionId": self.revision,
                "tabs": [{"tabProperties": {"tabId": self.tab}, "documentTab": {"body": {"content": content}}}]}

    def texts(self):
        return [(p[2], p[3]["style"], p[3]["bullet"]) for p in self.paragraphs()]


class GdocStatusTests(unittest.TestCase):
    """status-block --gdoc-read: the Docs requests that replace the status section."""
    BLOCK = ("## DDD Status\n\n_Maintained by the ddd agent. Last sync: now._\n\n"
             "**Awaiting review**\n- Login form {#login} - branch `ddd/login`\n- Logout \U0001F642\n\n"
             "**Blocked / questions**\n- Export - question: CSV only?\n")
    DEV = [("Plan", "TITLE"), ("Sign-in \U0001F44D\U0001F3FD", "HEADING_2"), ("Login form", "NORMAL_TEXT", True),
           ("Logout", "NORMAL_TEXT", True), ("Dark mode", "HEADING_2"), "Theme toggle."]

    def write_section(self, fake, block=None, tab=None):
        args = ddd.gdoc_status_requests(fake.read(), block or self.BLOCK, "DDD Status", tab)
        fake.apply(args)
        self.assertEqual(ddd.gdoc_status_check(fake.read(), args["expected"], "DDD Status", tab), [])
        return args

    def section(self, fake):
        texts = fake.texts()
        start = [t[0] for t in texts].index("DDD Status")
        return texts[:start], texts[start:]

    def expected_section(self):
        return [("DDD Status", "HEADING_2", False),
                ("Maintained by the ddd agent. Last sync: now.", "NORMAL_TEXT", False),
                ("Awaiting review", "NORMAL_TEXT", False),
                ("Login form {#login} - branch ddd/login", "NORMAL_TEXT", True),
                ("Logout \U0001F642", "NORMAL_TEXT", True),
                ("Blocked / questions", "NORMAL_TEXT", False),
                ("Export - question: CSV only?", "NORMAL_TEXT", True)]

    def test_appends_when_missing(self):
        fake = FakeDoc(self.DEV)
        before = fake.texts()
        args = self.write_section(fake)
        self.assertFalse(args["replaced"])
        dev, section = self.section(fake)
        self.assertEqual(dev, before)
        self.assertEqual(section, self.expected_section())

    def test_replaces_section_in_the_middle(self):
        old = [("DDD Status", "HEADING_2"), "old note", ("old row", "NORMAL_TEXT", True)]
        fake = FakeDoc(self.DEV[:4] + old + self.DEV[4:])
        args = self.write_section(fake)
        self.assertTrue(args["replaced"])
        texts = fake.texts()
        self.assertEqual(texts[:4], FakeDoc(self.DEV[:4]).texts())
        self.assertEqual(texts[4:11], self.expected_section())
        self.assertEqual(texts[11:], FakeDoc(self.DEV[4:]).texts())

    def test_replaces_section_at_the_end_and_is_idempotent(self):
        fake = FakeDoc(self.DEV + [("DDD Status", "HEADING_2"), ("old row", "NORMAL_TEXT", True)])
        self.write_section(fake)
        once = fake.texts()
        self.write_section(fake)
        self.assertEqual(fake.texts(), once)
        self.assertEqual(once[:len(self.DEV)], FakeDoc(self.DEV).texts())
        self.assertEqual(once[len(self.DEV):], self.expected_section())

    def test_heading_only_section_and_empty_status(self):
        fake = FakeDoc(self.DEV + [("DDD Status", "HEADING_3")])
        args = self.write_section(fake, "## DDD Status\n\nNothing in progress.\n")
        self.assertEqual(fake.texts()[-2:], [("DDD Status", "HEADING_3", False),
                                             ("Nothing in progress.", "NORMAL_TEXT", False)])
        self.assertEqual(args["expected"][0]["style"], "HEADING_3")  # keeps the doc's level

    def test_pending_suggestion_refuses(self):
        fake = FakeDoc(self.DEV + [("DDD Status", "HEADING_2"), "old"])
        fake.attrs[-1]["suggested"] = True
        with self.assertRaises(ddd.DddError) as cm:
            ddd.gdoc_status_requests(fake.read(), self.BLOCK, "DDD Status")
        self.assertEqual(cm.exception.code, 3)

    def test_tabs_and_wrappers(self):
        fake = FakeDoc(self.DEV, tab="t.plan")
        read = fake.read()
        read["tabs"].insert(0, {"tabProperties": {"tabId": "t.notes"},
                                "documentTab": {"body": {"content": [{"endIndex": 1, "sectionBreak": {}}]}}})
        args = ddd.gdoc_status_requests(read, self.BLOCK, "DDD Status", "t.plan")
        fake.apply(args)  # every range carries tabId t.plan, checked by FakeDoc
        first = ddd.gdoc_status_requests(read, self.BLOCK, "DDD Status")
        self.assertEqual(first["tabId"], "t.notes")
        self.assertIn("2 tabs", first["warnings"][0])
        with self.assertRaises(ddd.DddError):
            ddd.gdoc_status_requests(read, self.BLOCK, "DDD Status", "t.missing")
        root = tempfile.mkdtemp(prefix="ddd-gdoc-")
        self.addCleanup(shutil.rmtree, root, True)
        raw = json.dumps(fake.read())
        # connector.json is what the Google Docs connector's read_doc returns:
        # the documents.get object under "content", not a string
        connector = json.dumps({"content": fake.read()})
        for name, data in (("raw.json", raw), ("content.json", json.dumps({"content": raw})),
                           ("connector.json", connector),
                           ("connector-blocks.json", json.dumps([{"type": "text", "text": connector}])),
                           ("blocks.json", json.dumps([{"type": "text", "text": raw}]))):
            path = os.path.join(root, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            self.assertEqual(ddd.load_gdoc_json(path)["revisionId"], fake.revision, name)
        with open(os.path.join(root, "md.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"content": "# Plan"}))
        with self.assertRaises(ddd.DddError):
            ddd.load_gdoc_json(os.path.join(root, "md.json"))

    def test_check_reports_differences(self):
        fake = FakeDoc(self.DEV)
        args = ddd.gdoc_status_requests(fake.read(), self.BLOCK, "DDD Status")
        self.assertEqual(ddd.gdoc_status_check(fake.read(), args["expected"], "DDD Status"),
                         ["no DDD Status section found"])
        fake.apply(args)
        fake.attrs[-1]["bullet"] = False
        self.assertEqual(len(ddd.gdoc_status_check(fake.read(), args["expected"], "DDD Status")), 1)


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

    def test_gdoc_version_skips_fetch(self):
        self.ddd("init", "--doc", "https://docs.google.com/document/d/abc123_X/edit", "--force")
        os.makedirs(os.path.join(self.root, ".ddd", "tmp"), exist_ok=True)
        self.write(".ddd/tmp/doc.md", self.PLAN)
        self.assertTrue(self.ddd("diff", "--doc-version", "v1")["fetch_needed"])  # never synced
        self.ddd("diff", ".ddd/tmp/doc.md", "--doc-version", "v1")
        self.assertTrue(self.ddd("diff", "--doc-version", "v1")["fetch_needed"])  # pending, not accepted
        self.ddd("accept")
        quick = self.ddd("diff", "--doc-version", "v1")
        self.assertTrue(quick["fetch_skipped"])
        self.assertTrue(quick["doc_unchanged"])
        self.assertFalse(quick["needs_input"])
        self.assertTrue(self.ddd("diff", "--doc-version", "v2")["fetch_needed"])
        # Same text under a new version (a comment, the agent's own status write):
        # the version is remembered without an accept.
        self.assertTrue(self.ddd("diff", ".ddd/tmp/doc.md", "--doc-version", "v2")["doc_unchanged"])
        self.assertTrue(self.ddd("diff", "--doc-version", "v2")["fetch_skipped"])

    def test_gdoc_status_write_cli(self):
        url = "https://docs.google.com/document/d/abc123_X/edit?tab=t.plan"
        self.assertEqual(self.ddd("init", "--doc", url, "--force")["config"]["doc"]["tab"], "t.plan")
        self.write(".ddd/tmp/doc.md", self.PLAN)
        self.ddd("diff", ".ddd/tmp/doc.md", "--doc-version", "v1")
        self.ddd("accept")
        self.ddd("set", "login", "awaiting-review", "--branch", "ddd/login")
        fake = FakeDoc([("Plan", "TITLE"), ("Sign-in", "HEADING_2"), "Login form"], tab="t.plan")
        self.write(".ddd/tmp/read.json", json.dumps([{"type": "text", "text": json.dumps(fake.read())}]))
        self.ddd("status-block", "--gdoc-check", ".ddd/tmp/read.json", code=1)  # nothing written yet
        args = self.ddd("status-block", "--gdoc-read", ".ddd/tmp/read.json")
        self.assertEqual(args["writeControl"], {"requiredRevisionId": "rev-1"})
        self.assertEqual(args["tabId"], "t.plan")
        fake.apply(args)
        self.assertIn(("Login form {#login} - branch ddd/login", "NORMAL_TEXT", True), fake.texts())
        self.write(".ddd/tmp/read.json", json.dumps(fake.read()))
        self.assertTrue(self.ddd("status-block", "--gdoc-check", ".ddd/tmp/read.json")["ok"])
        self.assertIn('"status-write"', self.read(".ddd/log.jsonl"))
        fake.attrs[-1]["bullet"] = False  # someone un-bulleted a row
        self.write(".ddd/tmp/read.json", json.dumps(fake.read()))
        self.assertIn("differences", self.ddd("status-block", "--gdoc-check", ".ddd/tmp/read.json", code=1))

    def test_test_command_warning(self):
        self.write("gradlew", "#!/bin/sh\n")
        warned = self.ddd("init", "--doc", "plan.md", "--test", "gradlew test --console=plain")
        self.assertEqual(len(warned["warnings"]), 1)
        self.assertIn("./gradlew", warned["warnings"][0])
        self.assertEqual(self.ddd("config", "set", "test_command", "./gradlew test")["warnings"], [])
        self.assertEqual(self.ddd("config", "set", "test_command", "git status")["warnings"], [])
        self.assertEqual(len(self.ddd("config", "set", "test_command", "gradlew test")["warnings"]), 1)

    def test_commit_state_off_base_explains(self):
        self.init_and_sync()
        self.git("checkout", "-q", "-b", "feature")
        err = self.ddd("commit-state", code=1)["error"]
        self.assertIn("main", err)
        self.assertIn("git add", err)

    def test_parse_outline_and_compare(self):
        self.write("a.md", "# Plan\n\n## Auth\n\n{#auth} [next]\n\n- [ ] Login\n  - Done when: it works\n")
        self.write("b.md", "# Plan\n\n## Auth\n\n- [x] Login\n  - Done when: it works\n")
        out = self.ddd("parse", "a.md", "--outline")["outline"]
        self.assertEqual(out, ["# Plan", "  # Auth {#auth} [next]", "    [ ] Login (done when: 1)"])
        cmp = self.ddd("parse", "a.md", "--compare", "b.md")
        self.assertFalse(cmp["same"])
        by_title = {d["title"]: d for d in cmp["differences"]}
        self.assertEqual(by_title["Auth"]["tag"], ["auth", None])
        self.assertEqual(by_title["Login"]["checked"], [False, True])
        self.assertTrue(self.ddd("parse", "a.md", "--compare", "a.md")["same"])

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
        self.assertEqual((s["git"]["base_guess"], s["git"]["on_base"]), ("main", True))
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
        guide = guide.replace("\r\n", "\n")  # a Windows checkout (text=auto) has CRLF
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
        built = [r for r in self.ddd("items")["items"] if r["title"] == "<Part that already exists on the base branch>"]
        self.assertEqual([r["state"] for r in built], ["done"])
        offered = {c["title"] for c in self.ddd("rank", "--limit", "50")["candidates"]}
        self.assertIn("<Task>", offered)
        self.assertTrue(any(t.startswith("<Fix") for t in offered))
        self.assertFalse(offered & {"Overview", "Current features", "Open questions", "Ideas"})
        self.assertEqual(self.ddd("rank", "--queued")["candidates"], [])


if __name__ == "__main__":
    unittest.main()
