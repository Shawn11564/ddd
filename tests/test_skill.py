"""Packaging checks: SKILL.md follows the Agent Skills spec, its links resolve,
every `ddd <command>` it mentions exists, the plugin manifests agree, and every
mode has a Claude Code command (commands/<mode>.md, /ddd:<mode>) that the
installers copy."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(REPO, "skills", "ddd")
COMMANDS_DIR = os.path.join(REPO, "commands")
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import ddd  # noqa: E402


def skill_modes(text):
    """Modes from the SKILL.md mode table: `ddd auto` in the first column."""
    rows = [line for line in text.split("\n") if line.startswith("| `ddd")]
    return {m for row in rows for m in re.findall(r"`ddd ([a-z]+)", row.split("|")[1])}


def command_files():
    return sorted(fn[:-3] for fn in os.listdir(COMMANDS_DIR) if fn.endswith(".md"))


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
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
        modes = skill_modes(self.text)  # skill invocations, not script commands
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


class ModeCommands(unittest.TestCase):
    """commands/<mode>.md gives /ddd:<mode> in the Claude Code menu (plugin
    namespace, or ~/.claude/commands/ddd/ after install). Checked against a real
    Claude Code by evals/claude_code_commands.py."""

    def setUp(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
            self.modes = skill_modes(f.read())

    def test_one_command_per_mode(self):
        self.assertEqual(self.modes, {"menu", "auto", "status", "setup", "initialize"})
        self.assertEqual(set(command_files()), self.modes)

    def test_command_files(self):
        for mode in command_files():
            with self.subTest(mode=mode):
                with open(os.path.join(COMMANDS_DIR, mode + ".md"), encoding="utf-8") as f:
                    text = f.read()
                fm = frontmatter(text)
                self.assertTrue(fm.get("description"))
                self.assertEqual(fm.get("disable-model-invocation"), "true")
                self.assertEqual(set(fm) - {"description", "argument-hint", "disable-model-invocation"}, set())
                self.assertIn("`ddd` skill", text)
                self.assertIn("`%s $ARGUMENTS`" % mode, text)

    def prepare(self):
        """A throwaway home or project that already holds a command of the user's own."""
        tmp = tempfile.mkdtemp(prefix="ddd-install-")
        self.addCleanup(shutil.rmtree, tmp, True)
        own = os.path.join(tmp, ".claude", "commands", "ddd")
        os.makedirs(own)
        with open(os.path.join(own, "mine.md"), "w", encoding="utf-8") as f:
            f.write("keep me\n")
        return tmp

    def run_installer(self, argv, cwd, env):
        out = subprocess.run(argv, cwd=cwd, env=env, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stdout.decode("utf-8", "replace")
                         + out.stderr.decode("utf-8", "replace"))

    def assert_installed(self, base):
        self.assertTrue(os.path.exists(os.path.join(base, ".claude", "skills", "ddd", "SKILL.md")))
        cmd_dir = os.path.join(base, ".claude", "commands", "ddd")
        installed = sorted(fn[:-3] for fn in os.listdir(cmd_dir) if fn != "mine.md")
        self.assertEqual(installed, command_files())
        with open(os.path.join(cmd_dir, "mine.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep me\n")

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_install_sh_claude_target(self):
        home = self.prepare()
        self.run_installer(["sh", os.path.join(REPO, "install.sh"), "--target", "claude"],
                           home, dict(os.environ, HOME=home))
        self.assert_installed(home)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "needs PowerShell")
    def test_install_ps1_project_target(self):
        proj = self.prepare()
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.run_installer([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", os.path.join(REPO, "install.ps1"), "-Target", "project"],
                           proj, dict(os.environ))
        self.assert_installed(proj)
        self.assertTrue(os.path.exists(os.path.join(proj, ".agents", "skills", "ddd", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
