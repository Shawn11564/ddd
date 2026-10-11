"""Packaging checks: SKILL.md follows the Agent Skills spec, its links resolve,
every `ddd <command>` it mentions exists, and the plugin manifests agree."""

import json
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(REPO, "skills", "ddd")
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import ddd  # noqa: E402


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md has no frontmatter"
    fields, key, folded = {}, None, False
    for line in m.group(1).split("\n"):
        top = re.match(r"^([a-z][a-z0-9-]*):\s*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2)
            folded = value in (">", ">-", "|", "|-")
            fields[key] = "" if folded else value.strip().strip('"')
        elif key and folded and line.startswith("  "):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


class SkillPackaging(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
            self.text = f.read()
        self.fm = frontmatter(self.text)

    def test_frontmatter_matches_spec(self):
        name = self.fm.get("name")
        self.assertEqual(name, os.path.basename(SKILL_DIR))
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        desc = self.fm.get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024, "description is %d chars" % len(desc))
        allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
        self.assertEqual(set(self.fm) - allowed, set(), "non-portable frontmatter fields")

    def test_length(self):
        self.assertLess(self.text.count("\n"), 500)

    def test_relative_links_resolve(self):
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", self.text):
            if "://" in target:
                continue
            self.assertTrue(os.path.exists(os.path.join(SKILL_DIR, target)), "broken link: " + target)
        refs = os.path.join(SKILL_DIR, "references")
        for fn in os.listdir(refs):
            with open(os.path.join(refs, fn), encoding="utf-8") as f:
                body = f.read()
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", body):
                if "://" not in target:
                    self.assertTrue(os.path.exists(os.path.join(refs, target)),
                                    "broken link in %s: %s" % (fn, target))

    def test_documented_commands_exist(self):
        parser = ddd.build_parser()
        sub = next(a for a in parser._actions if a.dest == "cmd")
        known = set(sub.choices)
        docs = self.text
        with open(os.path.join(SKILL_DIR, "references", "state-format.md"), encoding="utf-8") as f:
            docs += f.read()
        modes = {"menu", "auto", "status", "setup"}  # skill invocations, not script commands
        used = set(re.findall(r"`ddd ([a-z][a-z-]*)", docs)) - modes
        self.assertEqual(used - known, set(), "SKILL.md mentions unknown commands")

    def test_manifests_agree(self):
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            plugin = json.load(f)
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            market = json.load(f)
        entry = market["plugins"][0]
        self.assertEqual(plugin["name"], entry["name"])
        self.assertTrue(entry["source"].startswith("./"))
        self.assertIn("name", market["owner"])
        version = self.fm.get("metadata", "")
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
            m = re.search(r'version:\s*"([^"]+)"', f.read())
        self.assertEqual(m.group(1), plugin["version"])
        self.assertEqual(entry["version"], plugin["version"])
        self.assertIsNotNone(version)


if __name__ == "__main__":
    unittest.main()
