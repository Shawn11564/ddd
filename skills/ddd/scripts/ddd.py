#!/usr/bin/env python3
"""ddd: project-state tracker for Document-Driven Development.

Parses a planning doc (Markdown, or Markdown/text exported from Google Docs),
keeps track of every item's identity across rewrites and reformats, and stores
the agent's own project state in .ddd/ at the project root.

Standard library only; Python 3.8+. Every command prints JSON to stdout.
Exit codes: 0 ok, 1 usage or state error, 2 input needed (resolve and retry),
3 the doc changed since the last sync (re-run diff).

See ../references/state-format.md and ../references/matching.md.
"""

import argparse
import base64
import binascii
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

SCHEMA_VERSION = 1
MULTI_CONFIG_VERSION = 2  # config.json with "docs" (references/multi-doc.md)
STATE_DIR = ".ddd"
STATES = ("todo", "in-progress", "awaiting-review", "done", "blocked", "removed")
ACTIVE = ("in-progress", "awaiting-review")
BUILT = ("in-progress", "awaiting-review", "done")
MARKERS = ("next", "blocked", "notes")
TRAILER = "DDD-Item"
# Doc names: short slugs that can't be read as a mode or an item count.
DOC_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RESERVED_DOC_NAMES = {"menu", "auto", "status", "setup", "initialize", "all", "ddd", "doc", "docs"}
# Per-doc settings in a multi-doc config; the rest of config.json is repo-wide.
DOC_SETTINGS = ("test_command", "branch_prefix", "status_section")

# Matching thresholds; see references/matching.md.
CONFIDENT = 0.78
UNCERTAIN = 0.55
POSSIBLE = 0.35

DEFAULT_CONFIG = {
    "version": SCHEMA_VERSION,
    "doc": {"type": "markdown", "path": None, "url": None, "id": None},
    "status_section": "DDD Status",
    "base_branch": None,
    "branch_prefix": "ddd/",
    "test_command": None,
    "hot_half_life_days": 7,
    "auto": {"on_blocker": "stop", "max_items": None},
    "capabilities": {"read": None, "comments": None, "targeted_edit": None},
}


class DddError(Exception):
    def __init__(self, message, code=1, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


# --------------------------------------------------------------------------
# small helpers

def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    return dt.datetime.fromisoformat(value)


def emit(obj):
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_text(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


STOPWORDS = set("a an and the of to for in on with by is are be as at or it this that".split())


def norm(text):
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = text.replace("_", " ")
    return " ".join(text.split())


def tokens(text):
    return [t for t in norm(text).split() if t not in STOPWORDS]


def slugify(text, limit=32):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > limit:
        cut = slug[:limit]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug or "item"


def short_hash(text, n=16):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


# --------------------------------------------------------------------------
# parsing

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
CHECK_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s+(.*)$")
UNICODE_CHECK_RE = re.compile(r"^(\s*)(?:[-*+]\s+)?([\u2610\u25a1\u2611\u2612\u2705\u2713\u2714])\s*(.*)$")
UNICODE_CHECKED = "\u2611\u2612\u2705\u2713\u2714"
TAG_RE = re.compile(r"\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}")
MARKER_RE = re.compile(r"\[(next|blocked|notes)\]", re.IGNORECASE)
ACCEPT_RE = re.compile(r"^(?:[-*+]\s+)?(?:acceptance|accept|done when)\s*:\s*(.*)$", re.IGNORECASE)
# `Needs: {#tag}` or `Needs: {#doc:tag}`; a Needs line without a reference is prose.
NEEDS_RE = re.compile(r"^(?:[-*+]\s+)?needs\s*:\s*(.*)$", re.IGNORECASE)
REF_RE = re.compile(r"\{#([A-Za-z0-9][A-Za-z0-9_-]*(?::[A-Za-z0-9][A-Za-z0-9_-]*)?)\}")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Google Docs' Markdown export escapes punctuation (\[next\], \{#tag\}).
UNESCAPE_RE = re.compile(r"\\([\[\]{}#_*\-.!()+`>|~])")


def unescape(line):
    lead = ""
    if line.lstrip().startswith("\\#"):
        idx = line.index("\\#")
        lead, line = line[: idx + 2], line[idx + 2:]
    return lead + UNESCAPE_RE.sub(r"\1", line)


def clean_inline(text):
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?<!\w)\*(\S[^*]*?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(\S[^_]*?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    return " ".join(text.split())


def extract_title(raw):
    tags = TAG_RE.findall(raw)
    markers = sorted({m.lower() for m in MARKER_RE.findall(raw)})
    title = TAG_RE.sub(" ", raw)
    title = MARKER_RE.sub(" ", title)
    title = clean_inline(title).rstrip(":").strip()
    return (tags[0] if tags else None), markers, title


def is_meta_line(line):
    """A line holding only tags and markers, e.g. `{#auth} [next]`."""
    text = line.strip()
    if not (TAG_RE.search(text) or MARKER_RE.search(text)):
        return False
    return not MARKER_RE.sub("", TAG_RE.sub("", text)).strip()


def is_status_title(text, status_title):
    _, _, title = extract_title(text)
    return norm(title).startswith(norm(status_title)) and bool(norm(status_title))


def indent_width(ws):
    return len(ws.expandtabs(4))


def status_span(lines, status_title):
    """Return (start, end) line indexes of the agent-owned status section, or None.

    The section starts at a heading (or a bare line) reading `status_title` and
    ends before the next heading of the same or higher level (end of doc for a
    bare line). Mirrors parse_doc().
    """
    in_code = False
    start = level = None
    for i, raw in enumerate(lines):
        line = unescape(raw)
        if FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        hm = HEADING_RE.match(line)
        if start is None:
            if hm and is_status_title(hm.group(2), status_title):
                start, level = i, len(hm.group(1))
            elif not hm and line.strip() and is_status_title(line.strip(), status_title) \
                    and norm(extract_title(line.strip())[2]) == norm(status_title):
                start, level = i, 0
        elif hm and level and len(hm.group(1)) <= level:
            return start, i
    if start is None:
        return None
    return start, len(lines)


def split_lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def doc_hash(text, status_title):
    """Hash of the doc with the status section removed and whitespace normalised,
    so the agent's own status writes never count as developer edits."""
    lines = split_lines(text)
    span = status_span(lines, status_title)
    if span:
        lines = lines[: span[0]] + lines[span[1]:]
    body = "\n".join(l.rstrip() for l in lines).strip()
    return short_hash(body)


def parse_doc(text, status_title="DDD Status"):
    lines = split_lines(text)
    span = status_span(lines, status_title)
    items = []
    heading_stack = []  # (level, item index)
    task_stack = []  # (indent, item index)
    meta_target = None  # heading whose tag/marker line may follow (see doc-format.md)
    in_code = False
    warnings = []

    def add(kind, level, raw_title, lineno, parent, checked=None, indent=0):
        tag, markers, title = extract_title(raw_title)
        item = {
            "key": "n%d" % (len(items) + 1),
            "kind": kind,
            "level": level,
            "title": title,
            "tag": tag,
            "markers": markers,
            "checked": checked,
            "parent": items[parent]["key"] if parent is not None else None,
            "line": lineno,
            "order": len(items),
            "body_lines": [],
            "acceptance": [],
            "needs": [],
            "indent": indent,
        }
        items.append(item)
        return len(items) - 1

    def attach(idx, text):
        if idx is None:
            return
        cleaned = clean_inline(text.strip().lstrip("-*+ ").strip()) if not in_code else text.rstrip()
        if not cleaned:
            return
        am = ACCEPT_RE.match(text.strip())
        if am and am.group(1).strip():
            items[idx]["acceptance"].append(clean_inline(am.group(1)))
        nm = None if in_code else NEEDS_RE.match(text.strip())
        if nm:
            for ref in REF_RE.findall(nm.group(1)):
                if ref not in items[idx]["needs"]:
                    items[idx]["needs"].append(ref)
        items[idx]["body_lines"].append(cleaned)

    for i, raw in enumerate(lines):
        if span and span[0] <= i < span[1]:
            continue
        lineno = i + 1
        line = unescape(raw)
        current = heading_stack[-1][1] if heading_stack else None
        if FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            target = task_stack[-1][1] if task_stack else current
            attach(target, line)
            continue
        hm = HEADING_RE.match(line)
        if hm:
            level = len(hm.group(1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            parent = heading_stack[-1][1] if heading_stack else None
            idx = add("heading", level, hm.group(2), lineno, parent)
            heading_stack.append((level, idx))
            task_stack = []
            meta_target = idx
            continue
        if not line.strip():
            continue
        if meta_target is not None and is_meta_line(line):
            # Google Docs' importer cuts a heading at its first "{", so the tag
            # (and markers) can sit on their own line right under the heading.
            tag, markers, _ = extract_title(line.strip())
            heading = items[meta_target]
            if tag and heading["tag"] and tag != heading["tag"]:
                warnings.append("line %d: heading already has {#%s}; {#%s} is ignored"
                                % (lineno, heading["tag"], tag))
            elif tag:
                heading["tag"] = tag
            heading["markers"] = sorted(set(heading["markers"]) | set(markers))
            meta_target = None
            continue
        meta_target = None
        cm = CHECK_RE.match(line)
        um = None if cm else UNICODE_CHECK_RE.match(line)
        if cm or um:
            ws = (cm or um).group(1)
            mark = (cm or um).group(2)
            rest = (cm or um).group(3)
            checked = mark.lower() == "x" if cm else mark in UNICODE_CHECKED
            ind = indent_width(ws)
            while task_stack and task_stack[-1][0] >= ind:
                task_stack.pop()
            parent = task_stack[-1][1] if task_stack else current
            level = (items[parent]["level"] + 1) if parent is not None else 1
            idx = add("task", level, rest, lineno, parent, checked=checked, indent=ind)
            task_stack.append((ind, idx))
            continue
        if not line.strip():
            continue
        ind = indent_width(re.match(r"^(\s*)", line).group(1))
        if task_stack and ind > task_stack[-1][0]:
            attach(task_stack[-1][1], line)
        else:
            task_stack = [] if not line.lstrip().startswith(("-", "*", "+")) else task_stack
            attach(current, line)

    seen = {}
    by_key = {it["key"]: it for it in items}
    for it in items:
        body = "\n".join(it.pop("body_lines"))
        it["body"] = body
        it["title_norm"] = norm(it["title"])
        it["body_norm"] = norm(body)
        parent = by_key.get(it["parent"]) if it["parent"] else None
        inherited = set(parent["markers"]) | set(parent["inherited"]) if parent else set()
        it["inherited"] = sorted(inherited - set(it["markers"]))
        if it["tag"]:
            if it["tag"] in seen:
                warnings.append("duplicate tag {#%s} at lines %d and %d; the second is ignored"
                                % (it["tag"], seen[it["tag"]], it["line"]))
                it["tag"] = None
            else:
                seen[it["tag"]] = it["line"]
    for it in items:
        it["children"] = [c["key"] for c in items if c["parent"] == it["key"]]
    return {"items": items, "warnings": warnings, "hash": doc_hash(text, status_title),
            "has_status_section": span is not None}


# --------------------------------------------------------------------------
# state

class Project:
    """The repo's .ddd/: config, log, and one doc (the original layout) or
    several named docs under .ddd/docs/<name>/ (references/multi-doc.md)."""

    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, STATE_DIR)
        self.config_path = os.path.join(self.dir, "config.json")
        self.log_path = os.path.join(self.dir, "log.jsonl")
        raw = load_json(self.config_path, {})
        self.multi = isinstance(raw.get("docs"), dict)
        self.config = merge_defaults(raw, DEFAULT_CONFIG)
        if self.multi:
            self.config.pop("doc", None)
            order = [n for n in self.config.get("doc_order", []) if n in self.config["docs"]]
            self.config["doc_order"] = order + sorted(n for n in self.config["docs"] if n not in order)
        self._docs = {}

    @property
    def project(self):
        return self

    def names(self):
        return list(self.config["doc_order"]) if self.multi else [None]

    def doc(self, name=None):
        if not self.multi:
            if name:
                raise DddError("this project has one doc; doc names exist after `ddd.py doc add`")
            name = None
        elif name not in self.config["docs"]:
            raise DddError("no doc named %r; docs: %s" % (name, ", ".join(self.names())))
        if name not in self._docs:
            self._docs[name] = State(self.root, name, self)
        return self._docs[name]

    def docs(self):
        return [self.doc(n) for n in self.names()]

    def save_config(self):
        save_json(self.config_path, self.config)

    def log(self, entry):
        entry = dict(entry)
        entry.setdefault("ts", now_iso())
        with open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def resolve_doc(self, name=None):
        """The doc a command works on: --doc, the only doc, or the doc whose
        scope holds the working directory."""
        if not self.multi or name:
            return self.doc(name)
        names = self.names()
        if len(names) == 1:
            return self.doc(names[0])
        rel = os.path.relpath(os.path.abspath(os.getcwd()), self.root).replace("\\", "/")
        best = None
        for n in names:
            scope = (self.config["docs"][n].get("scope") or "").strip("/")
            if scope and (rel == scope or rel.startswith(scope + "/")):
                if best is None or len(scope) > len(best[1]):
                    best = (n, scope)
        if best:
            return self.doc(best[0])
        raise DddError("this project has several docs; pass --doc NAME (or run inside a doc's scope)",
                       code=2, docs=names)

    def resolve_item(self, ident, name=None):
        """(State, id) for an item id: `doc:id`, or an id unique across docs."""
        if ":" in ident:
            dname, iid = ident.split(":", 1)
            if self.multi and dname in self.config["docs"]:
                st = self.doc(dname)
                found = st.find_id(iid)
                if found:
                    return st, found
            moved = self.find_moved(ident)
            if moved:
                return moved
            raise DddError("unknown item id: %s" % ident)
        if name or not self.multi:
            st = self.resolve_doc(name) if name else self.doc()
            return st, st.resolve_id(ident)
        hits = [(st, st.find_id(ident)) for st in self.docs()]
        hits = [(st, iid) for st, iid in hits if iid]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise DddError("%s is in several docs; use one of: %s"
                           % (ident, ", ".join(st.qid(i) for st, i in hits)), code=2)
        raise DddError("unknown item id: %s" % ident)

    def find_moved(self, qid):
        for st in self.docs():
            for iid, rec in st.items.items():
                if qid in rec.get("moved_from", []):
                    return st, iid
        return None

    def lookup(self, ref, from_state):
        """(State, id) for a Needs reference, or None."""
        if ":" in ref:
            if not self.multi:
                return None
            try:
                return self.resolve_item(ref)
            except DddError:
                return None
        iid = from_state.find_id(ref)
        return (from_state, iid) if iid else None


class State:
    """One doc's state: its items, snapshot and pending diff. `config` is the
    repo-wide config with the doc's own settings on top."""

    def __init__(self, root, name=None, project=None):
        self.root = root
        self.name = name
        self.project = project or Project(root)
        self.dir = os.path.join(root, STATE_DIR)
        self.config_path = self.project.config_path
        self.log_path = self.project.log_path
        self.doc_dir = os.path.join(self.dir, "docs", name) if name else self.dir
        self.items_path = os.path.join(self.doc_dir, "items.json")
        self.snapshot_path = os.path.join(self.doc_dir, "snapshot.md")
        self.tmp_dir = os.path.join(self.doc_dir, "tmp")
        self.pending_path = os.path.join(self.tmp_dir, "pending.json")
        self.fetched_path = os.path.join(self.tmp_dir, "fetched.json")  # `doc add --file`: the first sync's text
        if name:
            pc = self.project.config
            doc = pc["docs"][name]
            cfg = {k: v for k, v in pc.items() if k not in ("docs", "doc_order")}
            for k in DOC_SETTINGS:
                if doc.get(k) is not None:
                    cfg[k] = doc[k]
            if doc.get("branch_prefix") is None:
                cfg["branch_prefix"] = (pc.get("branch_prefix") or "ddd/") + name + "/"
            cfg["doc"] = {k: doc.get(k) for k in ("type", "path", "url", "id", "tab", "scope")}
            self.config = cfg
        else:
            self.config = self.project.config
        data = load_json(self.items_path, None) or {"version": SCHEMA_VERSION, "items": {},
                                                    "doc_hash": None, "last_sync": None}
        self.data = data
        self.items = data["items"]

    def save(self):
        save_json(self.items_path, self.data)

    def save_config(self):
        self.project.save_config()

    def log(self, entry):
        entry = dict(entry)
        if self.name:
            entry.setdefault("doc", self.name)
        self.project.log(entry)

    def qid(self, iid):
        return "%s:%s" % (self.name, iid) if self.name else iid

    def find_id(self, ident):
        if ident in self.items:
            return ident
        for iid, it in self.items.items():
            if ident in it.get("aliases", []):
                return iid
        return None

    def resolve_id(self, ident):
        found = self.find_id(ident)
        if found:
            return found
        if ":" in ident:
            hit = self.project.resolve_item(ident)
            if hit[0] is self:
                return hit[1]
        raise DddError("unknown item id: %s" % ident)

    def trailer_names(self, iid):
        """Every `DDD-Item:` value this item's commits may carry: its qualified
        id, ids it had before (aliases, moves, a renamed doc), and bare ids in
        a single-doc project or for commits made before the project went multi-doc."""
        rec = self.items.get(iid, {})
        bare = [iid] + rec.get("aliases", [])
        if not self.name:
            return bare
        doc = self.project.config["docs"][self.name]
        names = [self.qid(b) for b in bare]
        names += ["%s:%s" % (old, b) for old in doc.get("former_names", []) for b in bare]
        names += rec.get("moved_from", []) + rec.get("old_trailers", [])
        if doc.get("legacy_trailers"):
            names += bare
        return names

    def trailer(self, iid):
        return "%s: %s" % (TRAILER, self.qid(iid))

    def status_title(self):
        return self.config.get("status_section") or "DDD Status"


def merge_defaults(cfg, defaults):
    out = {}
    for k, v in defaults.items():
        if isinstance(v, dict):
            out[k] = merge_defaults(cfg.get(k, {}) or {}, v)
        else:
            out[k] = cfg.get(k, v)
    for k, v in cfg.items():
        if k not in out:
            out[k] = v
    return out


def find_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, STATE_DIR)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def load_project(args):
    root = args.root or find_root(os.getcwd())
    if not root or not os.path.isdir(os.path.join(root, STATE_DIR)):
        raise DddError("no .ddd/ found; run `ddd.py init --doc <path-or-url>` at the project root")
    return Project(os.path.abspath(root))


def load_state(args):
    """The doc a command works on (see Project.resolve_doc)."""
    return load_project(args).resolve_doc(getattr(args, "doc_name", None))


def load_item(args, ident=None):
    """(State, id) for the command's item id, qualified (`doc:id`) or not."""
    return load_project(args).resolve_item(ident or args.id, getattr(args, "doc_name", None))


# --------------------------------------------------------------------------
# matching

def similarity(new, old, parent_ok=None, total_new=1, total_old=1):
    title = difflib.SequenceMatcher(None, new["title_norm"], old["title_norm"]).ratio()
    nb, ob = set(tokens(new["body"])), set(tokens(old.get("body", "")))
    pos = 1.0 - abs(new["order"] / max(total_new, 1) - old.get("order", 0) / max(total_old, 1))
    if nb or ob:
        body = len(nb & ob) / len(nb | ob) if (nb | ob) else 0.0
        if parent_ok is None:
            score = 0.55 * title + 0.35 * body + 0.10 * pos
        else:
            score = 0.50 * title + 0.30 * body + 0.10 * (1.0 if parent_ok else 0.0) + 0.10 * pos
    else:
        if parent_ok is None:
            score = 0.88 * title + 0.12 * pos
        else:
            score = 0.75 * title + 0.15 * (1.0 if parent_ok else 0.0) + 0.10 * pos
    if new["kind"] != old.get("kind"):
        score *= 0.85
    if old.get("state") == "removed":
        score *= 0.9
    return round(score, 3)


def containment(small, big):
    s = set(small)
    return len(s & set(big)) / len(s) if s else 0.0


def compute_diff(prev, doc, forced=None, force_new=None):
    """Match parsed doc items to previously tracked items.

    Returns a dict with `matches` {new_key: {"id", "score", "how", "confident"}}
    and the report sections. `forced` maps new keys to ids the developer
    confirmed; `force_new` lists new keys that must not match anything.
    """
    forced = dict(forced or {})
    force_new = set(force_new or [])
    items = doc["items"]
    by_key = {it["key"]: it for it in items}
    matches = {}
    used = set()
    alias = {}
    for iid, it in prev.items():
        for a in it.get("aliases", []):
            alias[a] = iid

    def take(key, iid, score, how, confident=True):
        matches[key] = {"id": iid, "score": score, "how": how, "confident": confident}
        used.add(iid)

    for key, iid in forced.items():
        if key not in by_key:
            raise DddError("unknown doc key %s (keys come from the last diff report)" % key)
        if iid not in prev:
            iid = alias.get(iid, iid)
            if iid not in prev:
                raise DddError("unknown item id %s" % iid)
        if iid in used:
            raise DddError("item %s is matched twice" % iid)
        take(key, iid, 1.0, "confirmed")

    # 1. explicit tags
    for it in items:
        if it["key"] in matches or it["key"] in force_new or not it["tag"]:
            continue
        iid = it["tag"] if it["tag"] in prev else alias.get(it["tag"])
        if iid and iid not in used:
            take(it["key"], iid, 1.0, "tag")

    # 2. identical text
    exact = {}
    for iid, old in sorted(prev.items(), key=lambda kv: kv[1].get("order", 0)):
        if iid in used:
            continue
        exact.setdefault((old.get("kind"), old.get("title_norm"), old.get("body_norm")), []).append(iid)
    for it in items:
        if it["key"] in matches or it["key"] in force_new:
            continue
        bucket = exact.get((it["kind"], it["title_norm"], it["body_norm"]), [])
        bucket[:] = [i for i in bucket if i not in used]
        if bucket and it["title_norm"]:
            # Identical items (two "Write tests" tasks) follow their parent.
            parent_id = matches.get(it["parent"], {}).get("id") if it["parent"] else None
            pick = next((i for i in bucket if prev[i].get("parent") == parent_id), bucket[0])
            bucket.remove(pick)
            how = "retag" if it["tag"] and it["tag"] != pick else "exact"
            take(it["key"], pick, 1.0, how)

    # 3. similarity, first without parent context, then with it
    total_new, total_old = len(items), max(len(prev), 1)

    def parent_ok(it, old):
        if not it["parent"]:
            return old.get("parent") is None
        m = matches.get(it["parent"])
        return bool(m) and m["id"] == old.get("parent")

    for use_parent, floor in ((False, CONFIDENT), (True, UNCERTAIN)):
        pairs = []
        for it in items:
            if it["key"] in matches or it["key"] in force_new or not it["title_norm"]:
                continue
            for iid, old in prev.items():
                if iid in used:
                    continue
                s = similarity(it, old, parent_ok(it, old) if use_parent else None, total_new, total_old)
                if s >= floor:
                    pairs.append((s, it["key"], iid))
        pairs.sort(key=lambda p: (-p[0], by_key[p[1]]["order"]))
        for s, key, iid in pairs:
            if key in matches or iid in used:
                continue
            how = "retag" if by_key[key]["tag"] and by_key[key]["tag"] != iid else "similar"
            take(key, iid, s, how, confident=s >= CONFIDENT)

    report = build_report(prev, doc, matches, total_new, total_old)
    return {"matches": matches, "report": report}


def build_report(prev, doc, matches, total_new, total_old):
    items = doc["items"]
    by_key = {it["key"]: it for it in items}
    matched_ids = {m["id"] for m in matches.values()}
    id_of = {k: m["id"] for k, m in matches.items()}

    def brief(it):
        parent = by_key.get(it["parent"]) if it["parent"] else None
        return {"key": it["key"], "kind": it["kind"], "title": it["title"], "tag": it["tag"],
                "line": it["line"], "parent": parent["title"] if parent else None,
                "markers": it["markers"], "inherited": it["inherited"]}

    new, changed, proposed, conflicts, possible, splits = [], [], [], [], [], []
    unchanged = 0
    for it in items:
        m = matches.get(it["key"])
        if not m:
            new.append(brief(it))
            continue
        old = prev[m["id"]]
        types = []
        if old.get("state") == "removed":
            types.append("restored")
        if it["title_norm"] != old.get("title_norm"):
            types.append("renamed")
        if it["body_norm"] != old.get("body_norm"):
            types.append("edited")
        parent_id = id_of.get(it["parent"]) if it["parent"] else None
        if parent_id != old.get("parent"):
            types.append("moved")
        om, nm = set(old.get("markers", [])), set(it["markers"])
        for mk in MARKERS:
            if mk in nm and mk not in om:
                types.append({"next": "queued", "blocked": "blocked", "notes": "notes"}[mk])
            if mk in om and mk not in nm:
                types.append({"next": "unqueued", "blocked": "unblocked", "notes": "un-notes"}[mk])
        if it["checked"] and not old.get("checked"):
            types.append("checked")
        if it["checked"] is False and old.get("checked"):
            types.append("unchecked")
        if m["how"] == "retag":
            types.append("retagged")
        state = old.get("state", "todo")
        entry = dict(brief(it), id=m["id"], old_title=old.get("title"), state=state,
                     types=types, how=m["how"], score=m["score"])
        if not m["confident"]:
            proposed.append(entry)
        if types:
            changed.append(entry)
        else:
            unchanged += 1
        if ("renamed" in types or "edited" in types) and state in BUILT:
            conflicts.append({"type": "changed-after-build", "id": m["id"], "key": it["key"],
                              "title": it["title"], "state": state, "branch": old.get("branch")})
        if "checked" in types and state in ACTIVE:
            conflicts.append({"type": "checked-while-active", "id": m["id"], "key": it["key"],
                              "title": it["title"], "state": state, "branch": old.get("branch")})

    removed = []
    unmatched_new = [it for it in items if it["key"] not in matches]
    for iid, old in prev.items():
        if iid in matched_ids or old.get("state") == "removed":
            continue
        removed.append({"id": iid, "title": old.get("title"), "kind": old.get("kind"),
                        "state": old.get("state", "todo"), "branch": old.get("branch")})
        if old.get("state") in ACTIVE:
            conflicts.append({"type": "removed-while-active", "id": iid, "key": None,
                              "title": old.get("title"), "state": old.get("state"),
                              "branch": old.get("branch")})
        best = None
        for it in items:
            if not it["title_norm"]:
                continue
            s = similarity(it, old, None, total_new, total_old)
            if s >= POSSIBLE and (best is None or s > best[0]):
                best = (s, it)
        rewritten = {e["key"] for e in changed if "renamed" in e["types"] or "edited" in e["types"]}
        if best and (best[1]["key"] not in matches or best[1]["key"] in rewritten):
            # A merge target's own text changes; an untouched item is no merge.
            kind = "rename" if best[1]["key"] not in matches else "merge"
            possible.append({"id": iid, "old_title": old.get("title"), "key": best[1]["key"],
                             "title": best[1]["title"], "score": best[0], "kind": kind})
        old_tokens = tokens(old.get("title", "") + " " + old.get("body", ""))
        for it in unmatched_new:
            nt = tokens(it["title"] + " " + it["body"])
            if len(nt) >= 2 and containment(nt, old_tokens) >= 0.7:
                splits.append({"from_id": iid, "from_title": old.get("title"),
                               "key": it["key"], "title": it["title"]})
    for it in unmatched_new:
        nt = tokens(it["title"] + " " + it["body"])
        if len(nt) < 2:
            continue
        for iid in matched_ids:
            old = prev[iid]
            if old.get("state") == "removed":
                continue
            if containment(nt, tokens(old.get("title", "") + " " + old.get("body", ""))) >= 0.7 \
                    and not any(s["key"] == it["key"] for s in splits):
                splits.append({"from_id": iid, "from_title": old.get("title"),
                               "key": it["key"], "title": it["title"]})

    report = {
        "first_sync": not prev,
        "summary": {"items": len(items), "new": len(new), "changed": len(changed),
                    "removed": len(removed), "unchanged": unchanged,
                    "proposed": len(proposed), "conflicts": len(conflicts)},
        "new": new, "changed": changed, "removed": removed,
        "proposed": proposed, "possible": possible, "splits": splits,
        "conflicts": conflicts, "warnings": list(doc.get("warnings", [])),
    }
    report["needs_input"] = bool(proposed or possible or splits or conflicts) and bool(prev)
    return report


# --------------------------------------------------------------------------
# applying a diff

def unique_id(base, taken):
    cand, n = base, 2
    while cand in taken:
        cand = "%s-%d" % (base, n)
        n += 1
    return cand


def apply_diff(state, doc, result, text, splits=None, merges=None, imports=None, moved_out=None):
    """Apply a diff. `imports` {key: (qualified source id, record)} are items
    moved here from another doc: they keep their state, branch and history.
    `moved_out` are ids moving to another doc: never marked removed here."""
    prev = state.items
    ts = now_iso()
    matches = result["matches"]
    report = result["report"]
    imports = imports or {}
    moved_out = set(moved_out or [])
    taken = set(prev)
    for it in prev.values():
        taken.update(it.get("aliases", []))
    key_to_id = {}
    new_items = {iid: dict(it) for iid, it in prev.items()}
    changes = {c["key"]: c for c in report["changed"]}

    for it in doc["items"]:
        m = matches.get(it["key"])
        if it["key"] in imports:
            src, rec = imports[it["key"]]
            rec = json.loads(json.dumps(rec))
            if it["tag"] and it["tag"] not in taken:
                iid = it["tag"]
            elif rec.get("id") and rec["id"] not in taken:
                iid = rec["id"]
            else:
                iid = unique_id(it["tag"] or slugify(it["title"]), taken)
            taken.add(iid)
            src_doc = src.split(":", 1)[0]
            rec["moved_from"] = rec.get("moved_from", []) + [src] +                 ["%s:%s" % (src_doc, a) for a in rec.get("aliases", [])]
            rec["aliases"] = []
            rec["id"] = iid
            if rec.get("state") == "removed":
                rec["state"] = rec.pop("prev_state", None) or "todo"
                rec.pop("removed_at", None)
            rec["flags"] = [f for f in rec.get("flags", []) if f != "removed-from-doc"]
            if it["title_norm"] != rec.get("title_norm") or it["body_norm"] != rec.get("body_norm"):
                rec.setdefault("edits", []).append(ts)
                if rec.get("state") in BUILT and "changed-after-build" not in rec["flags"]:
                    rec["flags"].append("changed-after-build")
            if not rec["flags"]:
                rec.pop("flags")
            new_items[iid] = rec
            key_to_id[it["key"]] = iid
            state.log({"type": "move", "item": iid, "from": src, "title": it["title"]})
        elif m:
            iid = m["id"]
            rec = new_items.pop(iid)
            if it["tag"] and it["tag"] != iid and it["tag"] not in taken:
                rec.setdefault("aliases", []).append(iid)
                taken.add(it["tag"])
                iid = it["tag"]
            rec["id"] = iid
            new_items[iid] = rec
            key_to_id[it["key"]] = iid
            types = changes.get(it["key"], {}).get("types", [])
            if any(t in types for t in ("renamed", "edited", "restored", "queued")):
                rec.setdefault("edits", []).append(ts)
            if "restored" in types:
                rec["state"] = "todo"
                rec.pop("removed_at", None)
            if "checked" in types and rec.get("state") not in ACTIVE:
                rec["state"] = "done"
                rec["done_by"] = "developer"
            if "unchecked" in types and rec.get("state") == "done":
                rec["state"] = "todo"
            if ("renamed" in types or "edited" in types) and rec.get("state") in BUILT:
                rec.setdefault("flags", [])
                if "changed-after-build" not in rec["flags"]:
                    rec["flags"].append("changed-after-build")
        else:
            iid = unique_id(it["tag"] or slugify(it["title"]), taken)
            taken.add(iid)
            key_to_id[it["key"]] = iid
            new_items[iid] = {"id": iid, "state": "done" if it["checked"] else "todo",
                              "created": ts, "edits": [ts], "commits": [], "comments": []}
            if it["checked"]:
                new_items[iid]["done_by"] = "developer"
        rec = new_items[key_to_id[it["key"]]]
        for field in ("kind", "level", "title", "tag", "markers", "inherited", "checked",
                      "line", "order", "body", "acceptance", "needs", "title_norm", "body_norm"):
            rec[field] = it[field]
        rec["parent_key"] = it["parent"]
        rec["seen"] = ts

    for rec in new_items.values():
        if "parent_key" in rec:
            pk = rec.pop("parent_key")
            rec["parent"] = key_to_id.get(pk) if pk else None

    seen_ids = set(key_to_id.values())
    for iid, rec in new_items.items():
        if iid in seen_ids or iid in moved_out or rec.get("state") == "removed":
            continue
        if rec.get("state") in ACTIVE:
            rec.setdefault("flags", [])
            if "removed-from-doc" not in rec["flags"]:
                rec["flags"].append("removed-from-doc")
            continue
        rec["prev_state"] = rec.get("state", "todo")
        rec["state"] = "removed"
        rec["removed_at"] = ts

    for old_id, keys in (splits or {}).items():
        for k in keys:
            new_items[key_to_id[k]]["derived_from"] = old_id
        state.log({"type": "split", "item": old_id, "into": [key_to_id[k] for k in keys]})
    for old_ids, key in (merges or []):
        target = key_to_id[key]
        for oid in old_ids:
            if oid in new_items and oid != target:
                new_items[oid]["merged_into"] = target
        state.log({"type": "merge", "items": old_ids, "into": target})

    state.data["items"] = new_items
    state.items = new_items
    prev_hash = state.data.get("doc_hash")
    state.data["doc_hash"] = doc["hash"]
    state.data["last_sync"] = ts
    state.save()
    write_text(state.snapshot_path, text)

    state.log({"type": "sync", "doc_hash": doc["hash"], "prev_doc_hash": prev_hash,
               "summary": report["summary"]})
    for c in report["changed"]:
        state.log({"type": "doc-change", "item": key_to_id[c["key"]], "changes": c["types"],
                   "title": c["title"], "old_title": c["old_title"], "doc_hash": doc["hash"]})
    for n in report["new"]:
        if n["key"] not in imports:
            state.log({"type": "doc-change", "item": key_to_id[n["key"]], "changes": ["new"],
                       "title": n["title"], "doc_hash": doc["hash"]})
    for r in report["removed"]:
        if r["id"] not in moved_out:
            state.log({"type": "doc-change", "item": r["id"], "changes": ["removed"],
                       "title": r["title"], "doc_hash": doc["hash"]})
    for c in report["conflicts"]:
        if c["id"] not in moved_out:
            state.log({"type": "conflict", "item": c["id"], "conflict": c["type"]})
    return key_to_id


# --------------------------------------------------------------------------
# ranking

def hot_score(rec, now, half_life):
    score = 0.0
    for e in rec.get("edits", []):
        age = (now - parse_iso(e)).total_seconds() / 86400.0
        score += 0.5 ** (max(age, 0.0) / max(half_life, 0.01))
    return round(score, 3)


def effective(rec):
    return set(rec.get("markers", [])) | set(rec.get("inherited", []))


def needs_status(state, rec):
    """What the item's `Needs:` references say about starting it.

    Returns (open, stack_on, unresolved): `open` lists needs not done yet as
    {"ref", "id", "state"}; `stack_on` is the branch to build on when the only
    open need is awaiting review; `unresolved` lists references that match no
    item. The item waits when `open` is non-empty and `stack_on` is None."""
    open_, unresolved = [], []
    for ref in rec.get("needs") or []:
        hit = state.project.lookup(ref, state)
        if not hit:
            unresolved.append(ref)
            continue
        st, iid = hit
        other = st.items[iid]
        if other.get("state") == "done" or other.get("checked") or other.get("state") == "removed":
            continue
        open_.append({"ref": ref, "id": st.qid(iid), "state": other.get("state", "todo"),
                      "branch": other.get("branch")})
    stack_on = None
    if len(open_) == 1 and open_[0]["state"] == "awaiting-review" and open_[0]["branch"]:
        stack_on = open_[0]["branch"]
    return open_, stack_on, unresolved


def candidates(state, queued_only=False):
    """(candidates, blocked, waiting) for one doc. Ids are qualified (`doc:id`)
    in a multi-doc project."""
    items = state.items
    now = dt.datetime.now(dt.timezone.utc)
    half = float(state.config.get("hot_half_life_days") or 7)
    live = {iid: r for iid, r in items.items() if r.get("state") != "removed"}
    open_children, has_children = {}, set()
    for iid, r in live.items():
        p = r.get("parent")
        if not p or "notes" in effective(r):
            continue
        has_children.add(p)
        if r.get("state") != "done" and not r.get("checked"):
            open_children.setdefault(p, []).append(iid)
    total = max(len(live), 1)
    last_sync = state.data.get("last_sync")
    out, blocked, waiting = [], [], []
    for iid, r in live.items():
        eff = effective(r)
        if "notes" in eff or r.get("state") in ("done", "awaiting-review") or r.get("checked"):
            continue
        if open_children.get(iid) or iid in has_children:
            continue
        if r.get("kind") == "heading" and not r.get("body"):
            continue
        if "blocked" in eff or r.get("state") == "blocked":
            blocked.append({"id": state.qid(iid), "title": r.get("title"),
                            "reason": r.get("blocked_reason") or ("[blocked] in doc" if "blocked" in eff else None),
                            "question": r.get("question")})
            continue
        queued = "next" in eff
        if queued_only and not queued:
            continue
        resume = r.get("state") == "in-progress"
        open_needs, stack_on, unresolved = needs_status(state, r)
        if open_needs and not stack_on and not resume:
            waiting.append({"id": state.qid(iid), "title": r.get("title"), "queued": queued,
                            "needs": open_needs})
            continue
        hot = hot_score(r, now, half)
        is_new = bool(last_sync and r.get("created") == last_sync)
        order_bonus = 1.0 - r.get("order", 0) / total
        score = (200 if resume else 0) + (100 if queued else 0) + 10 * hot + (5 if is_new else 0) + 5 * order_bonus
        parent = items.get(r.get("parent")) if r.get("parent") else None
        signals = {"queued": queued, "resume": resume, "hot": hot, "new": is_new,
                   "flags": r.get("flags", [])}
        if stack_on:
            signals["stack_on"] = stack_on
        if unresolved:
            signals["needs_unresolved"] = unresolved
        cand = {
            "id": state.qid(iid), "title": r.get("title"), "kind": r.get("kind"), "state": r.get("state"),
            "parent": parent.get("title") if parent else None, "branch": r.get("branch"),
            "score": round(score, 2), "order": r.get("order", 0),
            "signals": signals,
            "acceptance": r.get("acceptance", []), "body": (r.get("body") or "")[:400],
        }
        if state.name:
            cand["doc"] = state.name
        out.append(cand)
    if queued_only:
        out.sort(key=lambda c: (not c["signals"]["resume"], c["order"]))
    else:
        out.sort(key=lambda c: (-c["score"], c["order"]))
    return out, blocked, waiting


# --------------------------------------------------------------------------
# status section

def render_status(state):
    items = state.items
    title = state.status_title()
    groups = {"in-progress": [], "awaiting-review": [], "blocked": [], "waiting": [], "done": []}
    waits = {}
    for iid, r in sorted(items.items(), key=lambda kv: kv[1].get("order", 0)):
        st = r.get("state")
        if st in groups:
            groups[st].append((iid, r))
        elif st == "todo" and "next" in effective(r) and "notes" not in effective(r)                 and "blocked" not in effective(r):
            open_needs, stack_on, _ = needs_status(state, r)
            if open_needs and not stack_on:
                waits[iid] = open_needs
                groups["waiting"].append((iid, r))
    groups["done"] = sorted((p for p in groups["done"] if p[1].get("done_by") != "developer"),
                            key=lambda p: p[1].get("done_at", ""), reverse=True)[:10]

    def line(iid, r):
        tag = " {#%s}" % iid if r.get("tag") == iid else ""
        bits = []
        if r.get("branch"):
            bits.append("branch `%s`" % r["branch"])
        if r.get("state") == "blocked":
            bits.append("question: " + r["question"] if r.get("question")
                        else (r.get("blocked_reason") or "blocked"))
        if "changed-after-build" in r.get("flags", []):
            bits.append("doc changed after build")
        if "removed-from-doc" in r.get("flags", []):
            bits.append("removed from doc while active")
        if iid in waits:
            bits.append("needs " + ", ".join("%s (%s)" % (n["id"], n["state"]) for n in waits[iid]))
        return "- %s%s%s" % (r.get("title"), tag, (" - " + "; ".join(bits)) if bits else "")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = ["## %s" % title, "",
           "_Maintained by the ddd agent; edits in this section are overwritten. Last sync: %s._" % stamp, ""]
    labels = (("in-progress", "In progress"), ("awaiting-review", "Awaiting review"),
              ("blocked", "Blocked / questions"), ("waiting", "Waiting"), ("done", "Recently done"))
    any_rows = False
    for key, label in labels:
        if groups[key]:
            any_rows = True
            out.append("**%s**" % label)
            out.extend(line(i, r) for i, r in groups[key])
            out.append("")
    if not any_rows:
        out.extend(["Nothing in progress.", ""])
    return "\n".join(out).rstrip() + "\n"


def replace_status(text, block, status_title):
    lines = split_lines(text)
    span = status_span(lines, status_title)
    new = split_lines(block.rstrip("\n"))
    if span:
        tail = lines[span[1]:]
        lines = lines[: span[0]] + new + ([""] if tail and tail[0].strip() else []) + tail
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines = lines + [""] + new
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# status section in a Google Doc: Docs API batchUpdate requests built from a
# saved documents.get (read_doc) result. Indexes are UTF-16 code units, and a
# paragraph's style lives on its trailing newline, so the section's last
# newline is kept and reused: no developer paragraph is ever merged or
# restyled. Guarded by tests/test_ddd.py GdocStatusTests.

GDOC_HEADING_LEVELS = dict([("TITLE", 0), ("SUBTITLE", 0)] +
                           [("HEADING_%d" % n, n) for n in range(1, 7)])
GDOC_TEXT_FIELDS = ("bold,italic,underline,strikethrough,smallCaps,backgroundColor,"
                    "foregroundColor,fontSize,weightedFontFamily,baselineOffset,link")
GDOC_PARA_FIELDS = "namedStyleType,alignment,indentStart,indentEnd,indentFirstLine,spaceAbove,spaceBelow"
GDOC_CODE_FONT = "Roboto Mono"
GDOC_BULLETS = "BULLET_DISC_CIRCLE_SQUARE"


def u16(text):
    return len(text.encode("utf-16-le")) // 2


def load_gdoc_json(path):
    """The documents.get JSON inside a saved read_doc result: the raw JSON, a
    `{"content": {...}}` wrapper (the Google Docs connector's shape), the same
    with the JSON as a string, or a `[{"type": "text", ...}]` list."""
    try:
        data = json.loads(read_text(path))
    except ValueError as e:
        raise DddError("%s is not JSON (%s)" % (path, e))
    for _ in range(5):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                break
        elif isinstance(data, list):
            texts = [b.get("text") for b in data if isinstance(b, dict) and isinstance(b.get("text"), str)]
            if not texts:
                break
            data = "".join(texts)
        elif isinstance(data, dict) and "tabs" not in data and "body" not in data \
                and isinstance(data.get("content"), (str, list, dict)):
            data = data["content"]
        else:
            break
    if not isinstance(data, dict) or not ("tabs" in data or "body" in data):
        raise DddError("%s is not a Google Docs read (documents.get JSON with 'tabs' or 'body')" % path)
    return data


def gdoc_tab(doc, tab_id=None):
    """(tab id, body content, warnings) of the tab to write: `tab_id`, else the first."""
    if "tabs" not in doc:
        if tab_id:
            raise DddError("the read has no tabs, so there is no tab %s" % tab_id)
        return None, doc["body"]["content"], []
    flat = []

    def visit(tabs):
        for t in tabs:
            flat.append((t["tabProperties"]["tabId"], t["documentTab"]["body"]["content"]))
            visit(t.get("childTabs", []))
    visit(doc["tabs"])
    if not flat:
        raise DddError("the read has no tabs")
    if tab_id:
        for tid, content in flat:
            if tid == tab_id:
                return tid, content, []
        raise DddError("no tab %s in the doc; its tabs: %s" % (tab_id, ", ".join(t for t, _ in flat)))
    warnings = []
    if len(flat) > 1:
        warnings.append("the doc has %d tabs; writing to the first (%s). Pass --tab to choose."
                        % (len(flat), flat[0][0]))
    return flat[0][0], flat[0][1], warnings


def gdoc_paragraphs(content):
    out = []
    for el in content:
        p = el.get("paragraph")
        if p is None:
            continue
        runs = [e["textRun"] for e in p.get("elements", []) if "textRun" in e]
        out.append({
            "start": el.get("startIndex", 0), "end": el["endIndex"],
            "text": "".join(r.get("content", "") for r in runs).rstrip("\n"),
            "style": p.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT"),
            "bullet": "bullet" in p,
            "suggested": any(r.get("suggestedInsertionIds") or r.get("suggestedDeletionIds") for r in runs),
            "text_style": runs[0].get("textStyle", {}) if runs else {},
        })
    return out


def gdoc_status_span(paras, status_title):
    """(first, last) paragraph indexes of the status section, or None. Mirrors
    status_span(): a heading reading the title, up to the next heading of the
    same or higher level; a bare line reading the title runs to the end."""
    start = level = None
    for i, p in enumerate(paras):
        text = unescape(p["text"]).strip()
        lvl = GDOC_HEADING_LEVELS.get(p["style"])
        if start is None:
            if lvl and is_status_title(text, status_title):
                start, level = i, lvl
            elif lvl is None and text and is_status_title(text, status_title) \
                    and norm(extract_title(text)[2]) == norm(status_title):
                start, level = i, 0
        elif level and lvl is not None and lvl <= level:
            return start, i - 1
    if start is None:
        return None
    return start, len(paras) - 1


def status_paragraphs(block, level=None):
    """The rendered status block (Markdown) as Docs paragraphs."""
    specs = []
    for line in split_lines(block):
        if not line.strip():
            continue
        hm = HEADING_RE.match(line)
        if hm:
            specs.append({"text": hm.group(2), "style": "HEADING_%d" % (level or len(hm.group(1))),
                          "bullet": False, "whole": None, "code": []})
            continue
        bullet = line.startswith("- ")
        body = line[2:] if bullet else line
        whole = None
        if len(body) > 4 and body.startswith("**") and body.endswith("**"):
            body, whole = body[2:-2], "bold"
        elif len(body) > 2 and body.startswith("_") and body.endswith("_"):
            body, whole = body[1:-1], "italic"
        text, code = "", []
        for k, part in enumerate(body.split("`")):
            if k % 2 and part:
                code.append((u16(text), u16(text) + u16(part)))
            text += part
        specs.append({"text": text, "style": "NORMAL_TEXT", "bullet": bullet, "whole": whole, "code": code})
    return specs


def gdoc_status_requests(doc, block, status_title, tab_id=None):
    """batchUpdate arguments that replace (or append) the status section."""
    if not doc.get("revisionId"):
        raise DddError("the read has no revisionId; read the doc again with the Docs tool")
    tab, content, warnings = gdoc_tab(doc, tab_id)
    paras = gdoc_paragraphs(content)
    body_end = content[-1]["endIndex"]
    span = gdoc_status_span(paras, status_title)

    def loc(i):
        return dict({"index": i}, **({"tabId": tab} if tab else {}))

    def rng(a, b):
        return dict({"startIndex": a, "endIndex": b}, **({"tabId": tab} if tab else {}))

    requests = []
    heading_style = {}
    if span:
        section = paras[span[0]: span[1] + 1]
        if any(p["suggested"] for p in section):
            raise DddError("the %s section in the doc has pending suggestions; ask the developer to accept "
                           "or reject them, then write again" % status_title, code=3)
        first, last = section[0], section[-1]
        level = GDOC_HEADING_LEVELS.get(first["style"]) or None
        heading_style = first["text_style"]
        if last["end"] - 1 > first["start"]:  # keep the section's last newline
            requests.append({"deleteContentRange": {"range": rng(first["start"], last["end"] - 1)}})
        specs = status_paragraphs(block, level)
        at = pos = first["start"]
        text = "\n".join(s["text"] for s in specs)
    else:
        specs = status_paragraphs(block)
        same = [p for p in paras if p["style"] == specs[0]["style"]]
        heading_style = same[0]["text_style"] if same else {}
        at = body_end - 1
        pos = at + 1
        text = "\n" + "\n".join(s["text"] for s in specs)
    requests.append({"insertText": {"location": loc(at), "text": text}})

    for s in specs:
        s["start"] = pos
        s["end"] = pos + u16(s["text"])  # its newline sits at `end`
        pos = s["end"] + 1
    whole = rng(specs[0]["start"], specs[-1]["end"])
    requests.append({"deleteParagraphBullets": {"range": whole}})
    for s in specs:
        r = rng(s["start"], max(s["end"], s["start"] + 1))
        requests.append({"updateParagraphStyle": {"range": r, "paragraphStyle": {"namedStyleType": s["style"]},
                                                  "fields": GDOC_PARA_FIELDS}})
    for s in specs:
        if s["end"] == s["start"]:
            continue
        base = {k: v for k, v in heading_style.items() if k != "link"} if s["style"] != "NORMAL_TEXT" else {}
        requests.append({"updateTextStyle": {"range": rng(s["start"], s["end"]), "textStyle": base,
                                             "fields": GDOC_TEXT_FIELDS}})
        if s["whole"]:
            requests.append({"updateTextStyle": {"range": rng(s["start"], s["end"]),
                                                 "textStyle": {s["whole"]: True}, "fields": s["whole"]}})
        for a, b in s["code"]:
            requests.append({"updateTextStyle": {"range": rng(s["start"] + a, s["start"] + b),
                                                 "textStyle": {"weightedFontFamily": {"fontFamily": GDOC_CODE_FONT}},
                                                 "fields": "weightedFontFamily"}})
    i = 0
    while i < len(specs):
        if not specs[i]["bullet"]:
            i += 1
            continue
        j = i
        while j + 1 < len(specs) and specs[j + 1]["bullet"]:
            j += 1
        requests.append({"createParagraphBullets": {"range": rng(specs[i]["start"], specs[j]["end"]),
                                                    "bulletPreset": GDOC_BULLETS}})
        i = j + 1
    expected = [{"text": s["text"], "style": s["style"], "bullet": s["bullet"]} for s in specs]
    return {"documentId": doc.get("documentId"), "tabId": tab, "requests": requests,
            "writeControl": {"requiredRevisionId": doc["revisionId"]},
            "replaced": bool(span), "expected": expected, "warnings": warnings}


def gdoc_status_check(doc, expected, status_title, tab_id=None):
    """Differences between the status section in a fresh read and what was written."""
    tab, content, _ = gdoc_tab(doc, tab_id)
    paras = gdoc_paragraphs(content)
    span = gdoc_status_span(paras, status_title)
    if not span:
        return ["no %s section found" % status_title]
    got = [{"text": p["text"], "style": p["style"], "bullet": p["bullet"]}
           for p in paras[span[0]: span[1] + 1]]
    diffs = []
    for k, (e, g) in enumerate(zip(expected, got)):
        if e != g:
            diffs.append("paragraph %d: expected %s, found %s" % (k + 1, json.dumps(e), json.dumps(g)))
    if len(got) != len(expected):
        diffs.append("expected %d paragraphs, found %d" % (len(expected), len(got)))
    return diffs


# --------------------------------------------------------------------------
# git

def git(state_or_root, *argv, check=True):
    root = state_or_root if isinstance(state_or_root, str) else state_or_root.root
    proc = subprocess.run(["git"] + list(argv), cwd=root, capture_output=True)
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise DddError("git %s failed: %s" % (" ".join(argv), err or out))
    return proc.returncode, out


def current_branch(state):
    code, out = git(state, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return out if code == 0 else None


def base_branch(state):
    base = state.config.get("base_branch")
    if not base:
        raise DddError("base_branch is not set; run `ddd.py config set base_branch <branch>`")
    return base


def floating_paths(state):
    """Repo-relative paths that float across branch switches instead of
    belonging to a branch: the state dir and a local Markdown doc. Both are
    live working files (the developer edits the doc mid-run, the agent writes
    its status section into it), so they never make the tree dirty.
    With several docs, every doc's Markdown file floats, so one doc's status
    write never blocks work on another.
    Guarded by tests/test_ddd.py: test_auto_builds_second_item_after_status_write,
    test_developer_doc_edits_survive_branch_switches, test_state_floats_across_branches,
    MultiDocTests.test_other_docs_float."""
    project = state.project
    docs = [project.config["docs"][n] for n in project.names()] if project.multi         else [project.config.get("doc") or {}]
    paths = [STATE_DIR]
    for doc in docs:
        if doc.get("type") == "markdown" and doc.get("path") and not doc["path"].startswith("..")                 and doc["path"] not in paths:
            paths.append(doc["path"])
    return paths


def _status(state, untracked):
    argv = ["status", "--porcelain", "--untracked-files=%s" % ("all" if untracked else "no"), "--", "."]
    argv += [":(exclude)%s" % p for p in floating_paths(state)]
    _, out = git(state, *argv)
    return [l for l in out.splitlines() if l.strip()]


def dirty_files(state):
    """Uncommitted changes to tracked files, ignoring the floating paths.
    Untracked files don't count: they carry across branch switches untouched."""
    return _status(state, untracked=False)


def untracked_files(state):
    return [l[3:] for l in _status(state, untracked=True) if l.startswith("??")]


def branch_exists(state, branch):
    code, _ = git(state, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, check=False)
    return code == 0


def checkout(state, branch, create_from=None):
    """Switch branches while the floating paths (.ddd/ and a local Markdown
    doc) keep their working-tree content.

    Item branches never commit them, so their newest version is whatever is
    on disk. Back them up, reset them to HEAD so git allows the switch, then
    restore the backup on top.
    """
    backup = {}
    files = []
    for dirpath, dirnames, filenames in os.walk(state.dir):
        dirnames[:] = [d for d in dirnames if d != "tmp"]  # .ddd/tmp/, .ddd/docs/<name>/tmp/
        files.extend(os.path.join(dirpath, fn) for fn in filenames)
    for rel in floating_paths(state)[1:]:
        if os.path.isfile(os.path.join(state.root, rel)):
            files.append(os.path.join(state.root, rel))
    for p in files:
        with open(p, "rb") as f:
            backup[os.path.relpath(p, state.root)] = f.read()
    # Untracked floating files would block a switch to a branch that tracks
    # them, and modified tracked ones block a switch to another version.
    for path in floating_paths(state):
        _, untracked = git(state, "ls-files", "--others", "--exclude-standard", "--", path, check=False)
        for rel in untracked.splitlines():
            if rel.strip():
                os.remove(os.path.join(state.root, rel.strip()))
        _, tracked = git(state, "ls-files", "--", path, check=False)
        if tracked.strip():
            git(state, "checkout", "--", path, check=False)
    try:
        if create_from:
            git(state, "checkout", "-b", branch, create_from)
        else:
            git(state, "checkout", branch)
    finally:
        for rel, content in backup.items():
            p = os.path.join(state.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(content)


def test_command_warnings(root, cmd):
    """Catch `gradlew test`-style commands: the program sits in the project root
    but is not on PATH, so POSIX shells (Git Bash included) won't find it."""
    if not cmd or not cmd.split():
        return []
    prog = cmd.split()[0]
    if "/" in prog or "\\" in prog:
        return []
    exts = [""] + (os.environ.get("PATHEXT", "").lower().split(";") if os.name == "nt" else [])
    root_abs = os.path.normcase(os.path.abspath(root))
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or d == "." or os.path.normcase(os.path.abspath(d)) == root_abs:
            continue
        if any(os.path.isfile(os.path.join(d, prog + e)) for e in exts):
            return []
    if any(os.path.isfile(os.path.join(root, prog + e)) for e in ("", ".sh", ".bat", ".cmd")):
        return ["`%s` is in the project root but not on PATH; most shells need `./%s`. Record the "
                "command exactly as you ran it." % (prog, prog)]
    return []


# --------------------------------------------------------------------------
# commands

def cmd_init(args):
    root = os.path.abspath(args.root or os.getcwd())
    d = os.path.join(root, STATE_DIR)
    if os.path.exists(os.path.join(d, "config.json")) and not args.force:
        raise DddError(".ddd/ already initialised at %s (use --force to overwrite config)" % root)
    os.makedirs(os.path.join(d, "tmp"), exist_ok=True)
    cfg = merge_defaults({}, DEFAULT_CONFIG)
    cfg["doc"] = doc_source(root, args.doc)
    if args.base:
        cfg["base_branch"] = args.base
    else:
        code, out = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        cfg["base_branch"] = out if code == 0 and out != "HEAD" else None
    if args.test:
        cfg["test_command"] = args.test
    save_json(os.path.join(d, "config.json"), cfg)
    if not os.path.exists(os.path.join(d, "items.json")):
        save_json(os.path.join(d, "items.json"),
                  {"version": SCHEMA_VERSION, "items": {}, "doc_hash": None, "last_sync": None})
    write_text(os.path.join(d, ".gitignore"), "tmp/\n")
    readme = os.path.join(d, "README.md")
    if not os.path.exists(readme):
        write_text(readme, "# .ddd\n\nState for the `ddd` (Document-Driven Development) agent skill.\n"
                           "Commit this folder on the base branch; never edit `items.json` by hand.\n"
                           "`snapshot.md` is the doc as of the last sync, `log.jsonl` the audit trail.\n")
    st = State(root)
    st.log({"type": "init", "doc": cfg["doc"]})
    emit({"ok": True, "root": root, "config": cfg, "warnings": test_command_warnings(root, args.test)})


def doc_summary(st):
    counts = {}
    for r in st.items.values():
        counts[r.get("state", "todo")] = counts.get(r.get("state", "todo"), 0) + 1
    return {"doc_hash": st.data.get("doc_hash"), "doc_version": st.data.get("doc_version"),
            "last_sync": st.data.get("last_sync"), "states": counts,
            "pending_diff": os.path.exists(st.pending_path),
            "saved_fetch": (load_json(st.fetched_path) or {}).get("doc_version"),
            "flagged": {st.qid(iid): r["flags"] for iid, r in st.items.items() if r.get("flags")}}


def cmd_info(args):
    project = load_project(args)
    if project.multi:
        docs = []
        for st in selected_docs(args):
            d = {"name": st.name, "doc": st.config["doc"], "branch_prefix": st.config.get("branch_prefix"),
                 "test_command": st.config.get("test_command"), "status_section": st.status_title(),
                 "wait_for": project.config["docs"][st.name].get("wait_for") or []}
            d.update(doc_summary(st))
            docs.append(d)
        info = {"root": project.root, "config": project.config, "docs": docs}
    else:
        st = project.doc()
        info = dict({"root": st.root, "config": st.config}, **doc_summary(st))
        info.pop("doc_version")
        info.pop("saved_fetch")
    if os.path.isdir(os.path.join(project.root, ".git")) or \
            git(project, "rev-parse", "--git-dir", check=False)[0] == 0:
        info["git"] = {"branch": current_branch(project), "dirty": dirty_files(project)}
    emit(info)


def doc_source(root, source):
    """Config fields for a doc given as a Google Docs URL or a Markdown path."""
    m = re.search(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)", source)
    if m:
        doc = {"type": "gdoc", "url": source, "id": m.group(1), "path": None}
        tab = re.search(r"[?&#]tab=(t\.[A-Za-z0-9_-]+)", source)
        if tab:
            doc["tab"] = tab.group(1)
        return doc
    rel = os.path.relpath(os.path.abspath(os.path.join(root, source)), root).replace("\\", "/")
    return {"type": "markdown", "path": rel, "url": None, "id": None}


def doc_name_problem(root, name, prefix, taken=()):
    """Why `name` can't name a doc, or None."""
    if not DOC_NAME_RE.match(name or "") or name in RESERVED_DOC_NAMES:
        return ("doc name %r: use a short slug starting with a letter (a-z, 0-9, -), not one of %s"
                % (name, ", ".join(sorted(RESERVED_DOC_NAMES))))
    if name in taken:
        return "there is already a doc named %s" % name
    # git can't hold a branch ddd/<name> next to branches under ddd/<name>/
    if git(root, "rev-parse", "--git-dir", check=False)[0] == 0 and \
            git(root, "rev-parse", "--verify", "--quiet", "refs/heads/" + prefix + name, check=False)[0] == 0:
        return ("branch %s%s exists, so items of a doc named %s can't use %s%s/<id>; pick another name"
                % (prefix, name, name, prefix, name))
    return None


def check_doc_name(project, name, prefix):
    problem = doc_name_problem(project.root, name, prefix, project.config["docs"] if project.multi else ())
    if problem:
        raise DddError(problem)


def migrate_to_multi(project, first):
    """Turn the single-doc layout into a multi-doc one, naming the doc `first`.
    Its items keep their ids and recorded branches; its old bare trailers stay
    recognised (`legacy_trailers`)."""
    cfg = project.config
    prefix = cfg.get("branch_prefix") or "ddd/"
    check_doc_name(project, first, prefix)
    dest = os.path.join(project.dir, "docs", first)
    os.makedirs(os.path.join(dest, "tmp"), exist_ok=True)
    for fn in ("items.json", "snapshot.md"):
        if os.path.exists(os.path.join(project.dir, fn)):
            os.replace(os.path.join(project.dir, fn), os.path.join(dest, fn))
    old_tmp = os.path.join(project.dir, "tmp")
    if os.path.isdir(old_tmp):
        for fn in os.listdir(old_tmp):
            os.replace(os.path.join(old_tmp, fn), os.path.join(dest, "tmp", fn))
        os.rmdir(old_tmp)
    doc = dict(cfg.pop("doc"))
    doc["legacy_trailers"] = True
    cfg["docs"] = {first: doc}
    cfg["doc_order"] = [first]
    cfg["version"] = MULTI_CONFIG_VERSION
    project.multi = True
    project._docs = {}
    project.save_config()
    project.log({"type": "doc-migrate", "doc": first})


def cmd_doc(args):
    if args.action == "suggest":
        cmd_doc_suggest(args)
        return
    project = load_project(args)
    cfg = project.config
    prefix = cfg.get("branch_prefix") or "ddd/"
    if args.action == "list":
        if not project.multi:
            emit({"multi": False, "docs": [{"name": None, "doc": cfg.get("doc")}]})
            return
        emit({"multi": True, "docs": [dict({"name": st.name, "doc": st.config["doc"],
                                            "branch_prefix": st.config.get("branch_prefix")},
                                           **doc_summary(st)) for st in project.docs()]})
        return
    if args.action == "add":
        if not args.name or not args.source:
            raise DddError("usage: doc add NAME PATH|URL [--scope DIR] [--test CMD] [--first-name NAME]")
        migrated, fetched = None, None
        for q in args.wait_for or []:
            project.resolve_item(q)  # refuse an unknown id before changing anything
        if args.file or args.doc_version:
            # The text already fetched (suggest) or read back (doc new), kept
            # for the first sync, which then skips the fetch if the doc's
            # version is still this one.
            if not (args.file and args.doc_version):
                raise DddError("--file and --doc-version go together: the text you fetched and the doc's "
                               "modifiedTime from before that fetch")
            if doc_source(project.root, args.source)["type"] != "gdoc":
                raise DddError("--file and --doc-version are for a Google Doc; a Markdown doc is read from its path")
            fetched = read_doc_input(args.file)
        if not project.multi:
            if not args.first_name:
                raise DddError("this project has one doc (%s); name it with --first-name NAME so both docs "
                               "have names" % (cfg["doc"].get("path") or cfg["doc"].get("url")), code=2)
            if args.first_name == args.name:
                raise DddError("the two docs need different names")
            check_doc_name(project, args.name, prefix)
            migrate_to_multi(project, args.first_name)
            migrated = args.first_name
        check_doc_name(project, args.name, prefix)
        doc = doc_source(project.root, args.source)
        for other, od in project.config["docs"].items():
            if (od.get("id") and od.get("id") == doc.get("id") and od.get("tab") == doc.get("tab")) or \
                    (od.get("path") and od.get("path") == doc.get("path")):
                raise DddError("doc %s already tracks %s" % (other, args.source))
        if args.scope:
            scope = os.path.relpath(os.path.abspath(os.path.join(project.root, args.scope)),
                                    project.root).replace("\\", "/")
            if scope.startswith(".."):
                raise DddError("--scope must be inside the project")
            doc["scope"] = scope
        if args.test:
            doc["test_command"] = args.test
        if args.wait_for:
            doc["wait_for"] = [other.qid(iid) for other, iid in (project.resolve_item(q) for q in args.wait_for)]
        project.config["docs"][args.name] = doc
        project.config["doc_order"].append(args.name)
        os.makedirs(os.path.join(project.dir, "docs", args.name, "tmp"), exist_ok=True)
        save_json(os.path.join(project.dir, "docs", args.name, "items.json"),
                  {"version": SCHEMA_VERSION, "items": {}, "doc_hash": None, "last_sync": None})
        project.save_config()
        project.log({"type": "doc-add", "doc": args.name, "source": doc})
        out = {"ok": True, "added": args.name, "migrated": migrated, "docs": project.names(),
               "branch_prefix": prefix + args.name + "/",
               "warnings": test_command_warnings(project.root, args.test)}
        if fetched is not None:
            st = project.doc(args.name)
            parsed = parse_doc(fetched, st.status_title())
            save_json(st.fetched_path, {"text": fetched, "doc_version": args.doc_version})
            out["saved_fetch"] = {"doc_version": args.doc_version, "items": len(parsed["items"])}
            out["warnings"] += parsed["warnings"]
        emit(out)
        return
    if not project.multi:
        raise DddError("this project has one doc; `doc add` names it and adds another")
    if args.action == "rename":
        old, new = args.name, args.source
        if old not in project.config["docs"]:
            raise DddError("no doc named %s" % old)
        check_doc_name(project, new, prefix)
        doc = project.config["docs"].pop(old)
        doc["former_names"] = doc.get("former_names", []) + [old]
        if doc.get("branch_prefix") is None:
            doc["branch_prefix"] = prefix + old + "/"  # existing and future branches keep one prefix
        project.config["docs"][new] = doc
        project.config["doc_order"] = [new if n == old else n for n in project.config["doc_order"]]
        src, dst = os.path.join(project.dir, "docs", old), os.path.join(project.dir, "docs", new)
        if os.path.isdir(src):
            os.replace(src, dst)
        project.save_config()
        project.log({"type": "doc-rename", "doc": new, "from": old})
        emit({"ok": True, "renamed": old, "to": new, "docs": project.names()})
        return
    if args.action == "remove":
        st = project.doc(args.name)
        active = [st.qid(i) for i, r in st.items.items() if r.get("state") in ACTIVE]
        if active and not args.force:
            raise DddError("doc %s has items in progress or awaiting review: %s; finish or drop them, or pass "
                           "--force" % (args.name, ", ".join(active)), code=2, active=active)
        if len(project.config["docs"]) == 1:
            raise DddError("can't remove the last doc")
        del project.config["docs"][args.name]
        project.config["doc_order"].remove(args.name)
        shutil.rmtree(st.doc_dir, ignore_errors=True)
        project.save_config()
        project.log({"type": "doc-remove", "doc": args.name, "items": len(st.items)})
        emit({"ok": True, "removed": args.name, "docs": project.names()})
        return
    raise DddError("unknown doc action %s" % args.action)


# --------------------------------------------------------------------------
# doc suggest: a best guess at the `doc add` settings, for the developer to
# confirm (references/multi-doc.md, "Adding a doc interactively")

GENERIC_WORDS = {"plan", "plans", "planning", "roadmap", "doc", "docs", "document", "spec", "specs",
                 "todo", "todos", "backlog", "notes", "readme", "design", "milestone", "milestones",
                 "draft", "project", "the", "a", "an", "and", "of", "for", "v1", "v2", "md", "main", "src"}
DOC_FOLDERS = {"doc", "docs", "documentation", "plan", "plans", "planning", "notes", "design", "wiki"}
SCOPE_DEPTH = 3


def name_words(text):
    """Lowercase words of a title or path, generic words dropped:
    'Clash plan' and 'ClashPlan.md' both give ['clash']."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", os.path.splitext(text)[0] if text.endswith(".md") else text)
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w and w not in GENERIC_WORDS]


def name_options(*texts):
    """Doc-name slugs from the given texts, best first: one word, then two."""
    out = []
    for text in texts:
        words = name_words(text or "")
        for n in (1, 2):
            if len(words) >= n:
                slug = "-".join(words[:n])[:32].strip("-")
                if slug and slug[0].isalpha() and slug not in out:
                    out.append(slug)
    return out


def doc_title(doc):
    heads = [it for it in doc["items"] if it["kind"] == "heading"]
    top = [it for it in heads if it["level"] == 1]
    return (top or heads or [{"title": None}])[0]["title"]


def scope_candidates(root, files, text, title, source):
    """Folders the doc probably plans, best first, each with its reasons."""
    dirs = set()
    for f in files:
        parts = f.split("/")[:-1]
        for n in range(1, min(len(parts), SCOPE_DEPTH) + 1):
            dirs.add("/".join(parts[:n]))
    title_words = set(name_words(title or ""))
    if source.get("path"):
        title_words |= set(name_words(os.path.basename(source["path"])))
    doc_dir = os.path.dirname(source["path"]).replace("\\", "/") if source.get("path") else ""
    manifests = {f.rsplit("/", 1)[0] for f in files if "/" in f and f.rsplit("/", 1)[1] in MANIFESTS}
    low = text.lower()
    out = []
    for d in dirs:
        base = d.rsplit("/", 1)[-1]
        if base.lower() in DOC_FOLDERS or base.startswith("."):
            continue
        why, score = [], 0
        hits = len(re.findall(r"(?<![\w./-])%s(?=/|[^\w-]|$)" % re.escape(d.lower()), low)) if "/" in d else \
            len(re.findall(r"(?<![\w./-])%s/" % re.escape(d.lower()), low))
        if hits:
            score += min(hits, 5)
            why.append("named %d time%s in the doc" % (hits, "" if hits == 1 else "s"))
        if title_words & set(name_words(base)):
            score += 3
            why.append("matches the doc's title")
        if doc_dir and (doc_dir == d or doc_dir.startswith(d + "/")) and doc_dir.count("/") - d.count("/") <= 1:
            score += 4 if doc_dir == d else 2
            why.append("holds the doc")
        if score and d in manifests:
            score += 2
            why.append("has its own build file")
        if score:
            out.append({"path": d, "score": score, "why": why})
    out.sort(key=lambda c: (-c["score"], c["path"].count("/"), c["path"]))
    return out[:5]


def gradle_project(root, files, scope):
    """`:games:clash` for a Gradle subproject in games/clash, as settings.gradle names it."""
    settings = next((f for f in ("settings.gradle.kts", "settings.gradle") if f in files), None)
    text = (read_small_text(os.path.join(root, settings)) or "") if settings else ""
    nested = scope.replace("/", ":")
    base = scope.rsplit("/", 1)[-1]
    if re.search(r"""["']:?%s["']""" % re.escape(nested), text):
        return ":" + nested
    if "/" in scope and re.search(r"""["']:?%s["']""" % re.escape(base), text):
        return ":" + base
    return ":" + nested


def scope_test_command(root, files, scope):
    """(command, reason) for the tests of the code in `scope`, or (None, None)."""
    fs = set(files)
    at = lambda name: "%s/%s" % (scope, name) in fs  # noqa: E731
    if at("build.gradle") or at("build.gradle.kts"):
        if "gradlew" not in fs and at("gradlew"):
            return "./%s/gradlew -p %s test" % (scope, scope), scope + " has its own Gradle wrapper"
        wrapper = "./gradlew" if "gradlew" in fs else "gradle"
        return wrapper + " " + gradle_project(root, files, scope) + ":test", "Gradle build file in " + scope
    if at("pom.xml"):
        mvn = "./mvnw" if "mvnw" in fs else "mvn"
        how = "-pl" if "pom.xml" in fs else "-f"  # a module of the root build, or a build of its own
        return "%s %s %s test" % (mvn, how, scope), "Maven pom in " + scope
    if at("package.json"):
        try:
            scripts = json.loads(read_text(os.path.join(root, scope, "package.json"))).get("scripts") or {}
        except (ValueError, AttributeError, OSError):
            scripts = {}
        if "test" in scripts:
            return "npm --prefix " + scope + " test", "package.json in %s has a test script" % scope
    if at("Cargo.toml"):
        return "cargo test --manifest-path " + scope + "/Cargo.toml", "Cargo.toml in " + scope
    if at("go.mod"):
        return "go -C " + scope + " test ./...", "go.mod in " + scope
    if "go.mod" in fs and any(f.startswith(scope + "/") and f.endswith("_test.go") for f in files):
        return "go test ./" + scope + "/...", "Go tests in " + scope
    py_tests = any(f.startswith(scope + "/") and re.search(r"(^|/)test_[^/]+\.py$", f) for f in files)
    if py_tests or any(at(n) for n in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")):
        pytest = any(at(n) or n in fs for n in ("pytest.ini", "conftest.py")) or any(
            "pytest" in (read_small_text(os.path.join(root, f)) or "")
            for f in ("pyproject.toml", "setup.cfg", "tox.ini", scope + "/pyproject.toml") if f in fs)
        if pytest:
            return "python -m pytest " + scope, "Python tests in %s (pytest configured)" % scope
        return "python -m unittest discover -s " + scope, "Python tests in %s (no pytest config)" % scope
    if at("Makefile") and re.search(r"(?m)^test\s*:", read_small_text(os.path.join(root, scope, "Makefile")) or ""):
        return "make -C " + scope + " test", "Makefile in %s has a test target" % scope
    return None, None


def live_test(st):
    """(test, checked): `test(id, record)` says whether an item is still in its
    doc. Checked against the Markdown file as it is now, or for a Google Doc
    against the last sync and any pending diff of this run (`checked`)."""
    p = pending_doc(st)
    leaving = {r["id"] for r in p[2]["report"]["removed"]} if p else set()
    path = st.config["doc"].get("path")
    now = None
    if st.config["doc"].get("type") == "markdown" and path and os.path.isfile(os.path.join(st.root, path)):
        now = parse_doc(read_text(os.path.join(st.root, path)), st.status_title())
    tags = {it["tag"] for it in now["items"] if it["tag"]} if now else set()
    titles = {it["title_norm"] for it in now["items"]} if now else set()

    def test(sid, rec):
        if rec.get("state") == "removed" or "removed-from-doc" in rec.get("flags", []) or sid in leaving:
            return False
        return now is None or bool(sid in tags or rec.get("tag") in tags or rec.get("title_norm") in titles)
    return test, "file" if now else "last sync"


def find_overlap(project, doc):
    """Items of the new doc that are already tracked in another doc, paired
    the way `find_moves` will pair them at the sync: by tag, or by identical
    text or similarity with an item that has left its doc. Items `live` in
    their own doc pair only by tag or identical text: they stay where they
    are unless the developer cuts them. The doc's title heading is left out."""
    states = project.docs() if project.multi else [project.doc()]
    now = dt.datetime.now(dt.timezone.utc)
    title = next((it for it in doc["items"] if it["kind"] == "heading" and it["level"] == 1), None)
    tracked = []  # (state, id, record, live, checked)
    for st in states:
        live, checked = live_test(st)
        for sid, rec in st.items.items():
            if rec.get("state") == "removed" and not rec.get("tag"):
                age = (now - parse_iso(rec["removed_at"])).days if rec.get("removed_at") else 0
                if age > RECENT_REMOVAL_DAYS:
                    continue
            tracked.append((st, sid, rec, live(sid, rec), checked))
    pairs = []
    for it in doc["items"]:
        if not it["title_norm"] or it is title:
            continue
        for n, (st, sid, rec, live, _) in enumerate(tracked):
            if it["tag"] and (it["tag"] == sid or it["tag"] == rec.get("tag")
                              or it["tag"] in rec.get("aliases", [])):
                pairs.append((0, 1.0, it, n, "tag"))
            elif (it["kind"], it["title_norm"], it["body_norm"]) == \
                    (rec.get("kind"), rec.get("title_norm"), rec.get("body_norm")):
                pairs.append((1, 1.0, it, n, "exact"))
            elif not live and difflib.SequenceMatcher(
                    None, it["title_norm"], rec.get("title_norm") or "").quick_ratio() >= UNCERTAIN:
                score = similarity(it, dict(rec, state="todo"), None, len(doc["items"]), max(len(st.items), 1))
                if score >= UNCERTAIN:
                    pairs.append((2, score, it, n, "similar"))
    pairs.sort(key=lambda p: (p[0], -p[1]))
    out, used_new, used_old = [], set(), set()
    for rank, score, it, n, how in pairs:
        if it["key"] in used_new or n in used_old:
            continue
        used_new.add(it["key"])
        used_old.add(n)
        st, sid, rec, live, checked = tracked[n]
        out.append({"key": it["key"], "order": it["order"], "title": it["title"], "doc": st.name,
                    "id": st.qid(sid), "tracked_title": rec.get("title"), "how": how, "score": round(score, 3),
                    "state": rec.get("prev_state") if rec.get("state") == "removed" else rec.get("state"),
                    "live": live, "checked": checked})
    out.sort(key=lambda o: o["order"])
    for o in out:
        del o["order"]
    return out


def cmd_doc_suggest(args):
    """Read a doc that isn't tracked yet and print a best guess at its
    `doc add` settings (or `init`'s, without a .ddd/), plus what the developer
    should confirm. Changes nothing."""
    if not args.name:
        raise DddError("usage: doc suggest PATH|URL [--file SAVED-TEXT]")
    found = args.root or find_root(os.getcwd())
    root = os.path.abspath(found or os.getcwd())
    project = Project(root) if found and os.path.exists(os.path.join(root, STATE_DIR, "config.json")) else None
    source = doc_source(root, args.name)
    if args.file:
        text = read_doc_input(args.file)
    elif source["type"] == "markdown":
        if not os.path.isfile(os.path.join(root, source["path"])):
            raise DddError("no file at %s" % source["path"])
        text = read_text(os.path.join(root, source["path"]))
    else:
        raise DddError("a Google Doc: fetch it through the connector, save it outside the repo, and pass "
                       "--file <saved>")
    status_title = (project.config.get("status_section") if project else None) or "DDD Status"
    doc = parse_doc(text, status_title)
    title = doc_title(doc)
    files = project_files(root)
    git_ok = git(root, "rev-parse", "--git-dir", check=False)[0] == 0
    prefix = (project.config.get("branch_prefix") if project else None) or "ddd/"

    tracked = {}
    if project:
        tracked = project.config["docs"] if project.multi else {None: project.config["doc"]}
    already = next((n for n, od in tracked.items()
                    if (od.get("id") and od.get("id") == source.get("id") and od.get("tab") == source.get("tab"))
                    or (od.get("path") and od.get("path") == source.get("path"))), False)

    scopes = scope_candidates(root, files, text, title, source)
    scope = scopes[0]["path"] if scopes and scopes[0]["score"] >= 3 else None
    test, test_why = scope_test_command(root, files, scope) if scope else (None, None)

    taken = [n for n in tracked if n]
    first = None
    if project and not project.multi:
        snap = os.path.join(project.dir, "snapshot.md")
        old_title = doc_title(parse_doc(read_text(snap), status_title)) if os.path.exists(snap) else None
        old = project.config["doc"]
        opts = name_options(old_title, os.path.basename(old.get("path") or ""),
                            os.path.dirname(old.get("path") or ""), os.path.basename(root)) + ["main"]
        first = next((n for n in opts if not doc_name_problem(root, n, prefix)), None)
        taken = [first] if first else []
    base = os.path.basename(source["path"]) if source.get("path") else ""
    opts = name_options(scope.rsplit("/", 1)[-1] if scope else "", title, base,
                        os.path.dirname(source.get("path") or "").rsplit("/", 1)[-1])
    names = [n for n in opts if not doc_name_problem(root, n, prefix, taken)]

    overlap = find_overlap(project, doc) if project else []
    live = [o for o in overlap if o["live"]]
    markers = {m: sum(1 for it in doc["items"] if m in it["markers"]) for m in MARKERS}
    lines = outline(doc)
    questions = []
    if already is not False:
        questions.append("already tracked%s: nothing to add" % (" as " + already if already else ""))
    else:
        if project:
            questions.append("doc name (suggested: %s)" % (names[0] if names else "none fits"))
            if not project.multi:
                questions.append("name for the existing doc (suggested: %s)" % (first or "none fits"))
        questions.append("scope folder (suggested: %s)" % (scope or "none: the doc plans the whole repo"))
        questions.append("test command (suggested: %s)" % (
            test or (project.config.get("test_command") if project else None) or "none found"))
        if live:
            questions.append("%d item%s also still in another doc: moving or copying?"
                             % (len(live), "" if len(live) == 1 else "s"))
    emit({
        "source": source,
        "project": "multi" if project and project.multi else ("single" if project else "none"),
        "already_tracked": already,
        "title": title,
        "items": len(doc["items"]),
        "markers": markers,
        "tags": sum(1 for it in doc["items"] if it["tag"]),
        "has_status_section": doc["has_status_section"],
        "warnings": doc["warnings"],
        "outline": lines[:80],
        "outline_truncated": max(len(lines) - 80, 0),
        "name": {"suggested": names[0] if names else None, "alternatives": names[1:4]},
        "first_name": first,
        "scope": {"suggested": scope, "candidates": scopes},
        "test_command": {"suggested": test, "why": test_why, "verified": False,
                         "repo": project.config.get("test_command") if project else None},
        "overlap": overlap,
        "overlap_live": len(live),
        "needs": sorted({ref for it in doc["items"] for ref in it.get("needs", [])}),
        "git": {"repo": git_ok, "branch": current_branch(root) if git_ok else None},
        "confirm": questions,
    })


def read_doc_input(path):
    """Doc text from a file: plain Markdown, a `.b64` file, or a connector's
    result saved as JSON (`{"content": "<base64 or text>"}`, also wrapped in a
    `[{"type": "text", "text": "..."}]` list), so a large result the host saved
    to disk never has to be retyped."""
    text = read_text(path)
    if path.lower().endswith(".b64"):
        return decode_b64(text, path)
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[", '"')):
        return text
    try:
        data = json.loads(stripped)
    except ValueError:
        return text
    if isinstance(data, str):  # the result saved as a JSON string
        try:
            data = json.loads(data)
        except ValueError:
            return data
    if isinstance(data, list) and data and all(isinstance(x, dict) and "text" in x for x in data):
        joined = "".join(x["text"] for x in data)
        try:
            data = json.loads(joined)
        except ValueError:
            return joined
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        content = data["content"]
        try:
            return decode_b64(content, path)
        except DddError:
            return content
    return text


def decode_b64(value, path):
    try:
        return base64.b64decode("".join(value.split()), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        raise DddError("%s is not valid base64 text" % path)


def doc_text_for(state, path):
    if path:
        return read_doc_input(path)
    cfg = state.config["doc"]
    if cfg.get("type") == "markdown" and cfg.get("path"):
        return read_text(os.path.join(state.root, cfg["path"]))
    raise DddError("the doc is a Google Doc: fetch it through the MCP connector, save it to "
                   "%s/doc.md, and pass that path"
                   % os.path.relpath(state.tmp_dir, state.root).replace("\\", "/"))


def outline(doc):
    """One line per item: indent by depth, then title, tag, markers, checkbox."""
    by_key = {it["key"]: it for it in doc["items"]}
    lines = []
    for it in doc["items"]:
        depth, p = 0, it["parent"]
        while p:
            depth, p = depth + 1, by_key[p]["parent"]
        box = "# " if it["checked"] is None else ("[x] " if it["checked"] else "[ ] ")
        extra = (" {#%s}" % it["tag"] if it["tag"] else "") + "".join(" [%s]" % m for m in it["markers"])
        if it["acceptance"]:
            extra += " (done when: %d)" % len(it["acceptance"])
        lines.append("%s%s%s%s" % ("  " * depth, box, it["title"], extra))
    return lines


def compare_docs(a, b):
    """Align two parses by title and report what differs: what a conversion lost."""
    fields = ("kind", "level", "tag", "markers", "checked", "acceptance")
    ta = [it["title_norm"] for it in a["items"]]
    tb = [it["title_norm"] for it in b["items"]]
    diffs, only_a, only_b = [], [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            for x, y in zip(a["items"][i1:i2], b["items"][j1:j2]):
                changed = {f: [x[f], y[f]] for f in fields if x[f] != y[f]}
                if changed:
                    diffs.append(dict({"title": x["title"]}, **changed))
        else:
            only_a += [it["title"] for it in a["items"][i1:i2]]
            only_b += [it["title"] for it in b["items"][j1:j2]]
    return {"same": not (diffs or only_a or only_b), "items": [len(ta), len(tb)],
            "differences": diffs, "only_in_first": only_a, "only_in_second": only_b}


# --------------------------------------------------------------------------
# several docs: moves between docs and Needs references (references/multi-doc.md)

RECENT_REMOVAL_DAYS = 30


def pending_doc(state):
    """(pending, parsed doc, diff result) for a doc's pending diff, or None."""
    pending = load_json(state.pending_path)
    if not pending:
        return None
    doc = parse_doc(pending["text"], state.status_title())
    return pending, doc, compute_diff(state.items, doc)


def find_moves(project, forced=None, rejected=None):
    """Items that moved from one doc to another, across every pending diff.

    A new item in doc B is matched to an item leaving doc A (removed in A's
    pending diff, or removed in an earlier sync): by tag or identical text
    with confidence, by similarity as a proposal. A tag that is still live in
    another doc is proposed too (that doc's own edit may not be synced yet).
    `forced` {"B:key": "A:id"} are moves the developer confirmed; `rejected`
    lists "B:key" that are new items. Returns a list of moves."""
    forced, rejected = dict(forced or {}), set(rejected or [])
    pend = {}
    for st in project.docs():
        p = pending_doc(st)
        if p:
            pend[st.name] = (st,) + p
    now = dt.datetime.now(dt.timezone.utc)
    leaving = {}  # (doc, id) -> "leaving" | "removed" | "live"
    for st in project.docs():
        going = {r["id"] for r in pend[st.name][3]["report"]["removed"]} if st.name in pend else set()
        for iid, rec in st.items.items():
            if iid in going or "removed-from-doc" in rec.get("flags", []):
                leaving[(st.name, iid)] = "leaving"
            elif rec.get("state") == "removed":
                age = (now - parse_iso(rec["removed_at"])).days if rec.get("removed_at") else 0
                leaving[(st.name, iid)] = "removed" if age <= RECENT_REMOVAL_DAYS else "old"
            else:
                leaving[(st.name, iid)] = "live"

    pairs = []  # (rank, score, to, key, from_doc, from_id, how, confident)
    for name, (st, pending, doc, result) in pend.items():
        new_keys = {n["key"] for n in result["report"]["new"]}
        for it in doc["items"]:
            qkey = "%s:%s" % (name, it["key"])
            if it["key"] not in new_keys or qkey in rejected:
                continue
            if qkey in forced:
                sdoc, sid = forced[qkey].split(":", 1)
                pairs.append((0, 1.0, name, it["key"], sdoc, project.doc(sdoc).resolve_id(sid), "confirmed", True))
                continue
            for src in project.docs():
                if src.name == name:
                    continue
                for sid, rec in src.items.items():
                    kind = leaving[(src.name, sid)]
                    tagged = it["tag"] and (it["tag"] == sid or it["tag"] == rec.get("tag")
                                            or it["tag"] in rec.get("aliases", []))
                    if tagged:
                        pairs.append((1, 1.0, name, it["key"], src.name, sid, "tag", kind != "live"))
                    elif kind == "live":
                        continue
                    elif (it["kind"], it["title_norm"], it["body_norm"]) == \
                            (rec.get("kind"), rec.get("title_norm"), rec.get("body_norm")) and it["title_norm"]:
                        pairs.append((2, 1.0, name, it["key"], src.name, sid, "exact", True))
                    elif kind in ("leaving", "removed") and it["title_norm"]:
                        score = similarity(it, dict(rec, state="todo"), None,
                                           len(doc["items"]), max(len(src.items), 1))
                        if score >= UNCERTAIN:
                            pairs.append((3, score, name, it["key"], src.name, sid, "similar", False))
    pairs.sort(key=lambda p: (p[0], -p[1]))
    moves, used_keys, used_src = [], set(), set()
    for rank, score, to, key, sdoc, sid, how, confident in pairs:
        if (to, key) in used_keys or (sdoc, sid) in used_src:
            continue
        used_keys.add((to, key))
        used_src.add((sdoc, sid))
        it = next(i for i in pend[to][2]["items"] if i["key"] == key)
        rec = project.doc(sdoc).items[sid]
        moves.append({"to": to, "key": key, "qkey": "%s:%s" % (to, key), "title": it["title"],
                      "from": "%s:%s" % (sdoc, sid), "from_title": rec.get("title"),
                      "state": rec.get("prev_state") if rec.get("state") == "removed" else rec.get("state"),
                      "how": how, "score": round(score, 3), "confident": confident})
    return moves


def apply_moves_to_report(report, name, moves):
    """Take confident moves out of a doc's new/removed lists and the questions
    they would raise, and recompute needs_input."""
    ins = {m["key"] for m in moves if m["to"] == name and m["confident"]}
    outs = {m["from"].split(":", 1)[1] for m in moves if m["from"].split(":", 1)[0] == name and m["confident"]}
    report["moved_in"] = [m for m in moves if m["to"] == name and m["confident"]]
    report["moved_out"] = [m for m in moves if m["from"].split(":", 1)[0] == name and m["confident"]]
    report["new"] = [n for n in report["new"] if n["key"] not in ins]
    report["removed"] = [r for r in report["removed"] if r["id"] not in outs]
    report["conflicts"] = [c for c in report["conflicts"] if c["id"] not in outs]
    report["possible"] = [p for p in report["possible"] if p["id"] not in outs and p["key"] not in ins]
    report["splits"] = [s for s in report["splits"] if s["from_id"] not in outs and s["key"] not in ins]
    report["summary"].update(new=len(report["new"]), removed=len(report["removed"]),
                             conflicts=len(report["conflicts"]), moved_in=len(report["moved_in"]),
                             moved_out=len(report["moved_out"]))
    report["needs_input"] = bool(report["proposed"] or report["possible"] or report["splits"]
                                 or report["conflicts"]) and not report["first_sync"]
    return report


def needs_warnings(state, doc):
    """Warnings for Needs references that match no item."""
    tags = {it["tag"] for it in doc["items"] if it["tag"]}
    out = []
    for it in doc["items"]:
        for ref in it.get("needs", []):
            if ":" in ref:
                dname, iid = ref.split(":", 1)
                ok = state.project.multi and dname in state.project.config["docs"] and (
                    (dname == state.name and (iid in tags or state.find_id(iid)))
                    or (dname != state.name and state.project.doc(dname).find_id(iid)))
            else:
                ok = ref in tags or bool(state.find_id(ref))
            if not ok:
                out.append("line %d: Needs {#%s} matches no item%s" % (
                    it["line"], ref, "" if state.project.multi or ":" not in ref
                    else " (this project has one doc; `doc:tag` references need several)"))
    return out


# --------------------------------------------------------------------------
# reformat: the same doc in another style (references/reformat.md)

def is_notes(it):
    return "notes" in it.get("markers", []) or "notes" in it.get("inherited", [])


def reformat_report(st, text):
    """What a rewrite of the doc would change besides its look. A reformat
    keeps every item, its words, section, order, markers and checkbox; it may
    pin ids with tags (or drop tags the text still matches without) and add
    `[notes]` sections. Words are compared normalised, so punctuation, bold,
    code spans, list markers and line breaks are free."""
    doc = parse_doc(text, st.status_title())
    result = compute_diff(st.items, doc)
    r, matches = result["report"], result["matches"]
    by_key = {it["key"]: it for it in doc["items"]}
    problems, added_notes = [], []
    for e in r["new"]:
        if is_notes(by_key[e["key"]]):
            added_notes.append({"line": e["line"], "title": e["title"]})
        else:
            problems.append({"problem": "new", "line": e["line"], "title": e["title"],
                             "fix": "a reformat adds no work: make it description or [notes] text, or take it out"})
    for e in r["removed"]:
        problems.append({"problem": "lost", "id": st.qid(e["id"]), "title": e["title"],
                         "fix": "put it back in its own words, or tag it {#%s} where it went" % e["id"]})
    uncertain = {p["key"] for p in r["proposed"]}
    for p in r["proposed"]:
        problems.append({"problem": "uncertain", "id": st.qid(p["id"]), "line": p["line"], "title": p["title"],
                         "fix": "tag it {#%s}" % p["id"]})
    for e in r["changed"]:
        bad = [t for t in e["types"] if t != "retagged"]
        if bad and e["key"] not in uncertain:
            problems.append({"problem": "changed", "id": st.qid(e["id"]), "line": e["line"], "title": e["title"],
                             "changes": bad, "fix": "keep its words, section, markers and checkbox as they were"})
    # doc order is build order (auto mode): work items keep theirs
    work = [it["key"] for it in doc["items"] if it["key"] in matches and not is_notes(it)]
    drafted = [matches[k]["id"] for k in work]
    kept = set(drafted)
    tracked = [iid for iid, _ in sorted(st.items.items(), key=lambda kv: kv[1].get("order", 0)) if iid in kept]
    if drafted != tracked:
        n = next(i for i, (a, b) in enumerate(zip(drafted, tracked)) if a != b)
        problems.append({"problem": "reordered", "id": st.qid(drafted[n]), "title": st.items[drafted[n]].get("title"),
                         "fix": "keep the items in their order (it is the build order); %s comes first"
                                % st.qid(tracked[n])})
    pinned = [st.qid(m["id"]) for k, m in matches.items() if by_key[k]["tag"] == m["id"]]
    return {"ok": not problems and not r["warnings"], "items": len(doc["items"]),
            "tracked": sum(1 for rec in st.items.values() if rec.get("state") != "removed"),
            "kept": len(matches), "tagged": len(pinned), "added_notes": added_notes, "problems": problems,
            "warnings": r["warnings"], "has_status_section": doc["has_status_section"]}


def reformat_fresh(st):
    """A reformat starts from the doc as last synced: no open diff, and a
    Markdown file untouched since."""
    if not st.data.get("doc_hash"):
        raise DddError("sync the doc first: there is nothing tracked to keep yet")
    pending = load_json(st.pending_path)
    if pending and pending.get("doc_hash") != st.data.get("doc_hash"):
        raise DddError("finish the sync first (`ddd accept`): the doc has changes not accepted yet", code=3)
    path = st.config["doc"].get("path")
    if st.config["doc"].get("type") == "markdown" and path:
        if doc_hash(read_text(os.path.join(st.root, path)), st.status_title()) != st.data["doc_hash"]:
            raise DddError("%s changed since the last sync; sync it, then redo the reformat" % path, code=3)


def cmd_reformat(args):
    st = load_state(args)
    cfg = st.config["doc"]
    if args.action == "relink":
        # A Google Doc is reformatted into a new doc (references/reformat.md):
        # point ddd at it, keeping every item and its state.
        if cfg.get("type") != "gdoc":
            raise DddError("relink is for a Google Doc; a Markdown doc is rewritten with `reformat write`")
        reformat_fresh(st)
        if args.old_version and args.old_version != st.data.get("doc_version"):
            raise DddError("the old doc changed since the last sync (version %s, synced %s): sync it, then "
                           "redo the reformat from its new text" % (args.old_version, st.data.get("doc_version")),
                           code=3)
        new = doc_source(st.root, args.target)
        if new["type"] != "gdoc":
            raise DddError("relink takes the new Google Doc's URL")
        node = st.project.config["docs"][st.name] if st.name else st.project.config["doc"]
        old = {k: node.get(k) for k in ("url", "id", "tab")}
        node["former_urls"] = node.get("former_urls", []) + [old["url"]]
        node.update({"url": new["url"], "id": new["id"]})
        node.pop("tab", None)
        if new.get("tab"):
            node["tab"] = new["tab"]
        st.project.save_config()
        st.data["doc_version"] = None  # a new doc: its first fetch has no version to skip on
        st.save()
        st.log({"type": "doc-relink", "from": old["url"], "to": new["url"]})
        emit({"ok": True, "from": old["url"], "to": new["url"]})
        return
    reformat_fresh(st)
    text = read_doc_input(args.target)
    report = reformat_report(st, text)
    if args.action == "check":
        emit(report)
        return
    # write: replace a Markdown doc with the reformatted text
    if cfg.get("type") != "markdown" or not cfg.get("path"):
        raise DddError("write replaces a Markdown doc; a Google Doc is reformatted into a new doc and "
                       "relinked (references/reformat.md)")
    if not report["ok"]:
        raise DddError("the reformat would change more than the doc's look; fix the problems first",
                       problems=report["problems"], warnings=report["warnings"])
    path = os.path.join(st.root, cfg["path"])
    current = read_text(path)
    lines = split_lines(current)
    span = status_span(lines, st.status_title())
    out = text.replace("\r\n", "\n")
    if span and not report["has_status_section"]:
        out = out.rstrip("\n") + "\n\n" + "\n".join(lines[span[0]:span[1]]).strip("\n") + "\n"
    if "\r\n" in current:
        out = out.replace("\n", "\r\n")
    os.makedirs(st.tmp_dir, exist_ok=True)
    backup = os.path.join(st.tmp_dir, "before-reformat.md")
    write_text(backup, current)
    write_text(path, out)
    st.log({"type": "reformat", "path": cfg["path"], "items": report["kept"]})
    emit(dict(report, written=cfg["path"], backup=os.path.relpath(backup, st.root).replace("\\", "/")))


def cmd_parse(args):
    st = State(args.root) if args.root else None
    title = st.status_title() if st else "DDD Status"
    doc = parse_doc(read_doc_input(args.file), title)
    if args.compare:
        other = parse_doc(read_doc_input(args.compare), title)
        emit(dict(compare_docs(doc, other), warnings=[doc["warnings"], other["warnings"]]))
    elif args.outline:
        emit({"items": len(doc["items"]), "warnings": doc["warnings"], "outline": outline(doc)})
    else:
        emit(doc)


def waiting_for(st):
    """Items this doc's first sync waits for to leave their own docs (`doc add
    --wait-for`): a section copied into the new doc moves, with its state,
    only if the sync sees it leave the old doc. Clears the wait once they all
    have."""
    doc = st.project.config["docs"][st.name] if st.name else {}
    wait = doc.get("wait_for") or []
    tests, still = {}, []
    for q in wait:
        try:
            other, sid = st.project.resolve_item(q)
        except DddError:
            continue
        if other is st:
            continue
        if other.name not in tests:
            tests[other.name] = live_test(other)[0]
        if tests[other.name](sid, other.items[sid]):
            still.append({"id": other.qid(sid), "title": other.items[sid].get("title")})
    if wait and not still:
        doc.pop("wait_for")
        st.project.save_config()
        st.log({"type": "doc-wait-done", "items": wait})
    return still


def cmd_diff(args):
    st = load_state(args)
    version = args.doc_version
    held = waiting_for(st)
    if held:
        emit({"doc": st.name, "held": True, "waiting_for": held, "fetch_skipped": True, "fetch_needed": False,
              "doc_unchanged": True, "needs_input": False})
        return
    text, saved = None, load_json(st.fetched_path)
    if saved and (version or args.file):
        # `doc add --file`: the text fetched then stands in for the first
        # fetch while the doc is still at that version; a newer one is stale.
        if not args.file and saved.get("doc_version") == version:
            text = saved["text"]
        os.remove(st.fetched_path)
    if text is None and version and not args.file and st.config["doc"].get("type") == "gdoc":
        # Cheap check before fetching a Google Doc: same version, same text.
        pending = load_json(st.pending_path)
        open_diff = bool(pending) and pending.get("doc_hash") != st.data.get("doc_hash")
        unchanged = version == st.data.get("doc_version") and not open_diff
        emit({"fetch_skipped": unchanged, "fetch_needed": not unchanged, "doc_unchanged": unchanged,
              "needs_input": False, "doc_version": version,
              "previous_doc_version": st.data.get("doc_version"), "doc_hash": st.data.get("doc_hash")})
        return
    from_saved = text is not None
    text = text if from_saved else doc_text_for(st, args.file)
    doc = parse_doc(text, st.status_title())
    result = compute_diff(st.items, doc)
    report = result["report"]
    if from_saved:
        report["saved_fetch_used"] = True
    report["warnings"] += needs_warnings(st, doc)
    report["doc_hash"] = doc["hash"]
    report["previous_doc_hash"] = st.data.get("doc_hash")
    report["doc_unchanged"] = doc["hash"] == st.data.get("doc_hash")
    report["has_status_section"] = doc["has_status_section"]
    os.makedirs(st.tmp_dir, exist_ok=True)
    save_json(st.pending_path, {"text": text, "doc_hash": doc["hash"], "doc_version": version})
    if version and report["doc_unchanged"]:
        st.data["doc_version"] = version  # nothing to accept; remember the version now
        st.save()
    if st.name:
        # Moves between docs are judged across every pending diff, so the
        # last doc diffed in a run reports the full set.
        moves = find_moves(st.project)
        apply_moves_to_report(report, st.name, moves)
        proposed = [m for m in moves if not m["confident"]]
        report["doc"] = st.name
        report["joint"] = {"pending": [d.name for d in st.project.docs() if os.path.exists(d.pending_path)],
                           "moves": [m for m in moves if m["confident"]], "proposed_moves": proposed}
        report["needs_input"] = report["needs_input"] or bool(proposed)
    emit(report)


def parse_pairs(values, sep="="):
    out = []
    for v in values or []:
        if sep not in v:
            raise DddError("expected A%sB, got %r" % (sep, v))
        a, b = v.split(sep, 1)
        out.append((a.strip(), b.strip()))
    return out


def cmd_accept(args):
    project = load_project(args)
    if project.multi:
        return accept_docs(args, project)
    st = project.doc()
    if args.move or args.no_move:
        raise DddError("--move and --no-move need several docs")
    pending = load_json(st.pending_path)
    if not pending:
        raise DddError("no pending diff; run `ddd.py diff` first")
    text = pending["text"]
    doc = parse_doc(text, st.status_title())
    forced = {k: v for k, v in parse_pairs(args.match)}
    force_new = set(args.new or [])
    splits = {}
    for old, keys in parse_pairs(args.split):
        splits[st.resolve_id(old)] = [k.strip() for k in keys.split(",") if k.strip()]
        force_new.update(splits[st.resolve_id(old)])
    merges = []
    for olds, key in parse_pairs(args.merge):
        ids = [st.resolve_id(o.strip()) for o in olds.split(",") if o.strip()]
        merges.append((ids, key))
        forced.setdefault(key, ids[0])
    result = compute_diff(st.items, doc, forced=forced, force_new=force_new)
    report = result["report"]
    unresolved = [p for p in report["proposed"] if p["key"] not in forced]
    if unresolved and not args.accept_proposed:
        raise DddError("uncertain matches need a decision: pass --match KEY=ID to confirm, --new KEY to "
                       "reject, or --accept-proposed", code=2, proposed=unresolved)
    key_to_id = apply_diff(st, doc, result, text, splits=splits, merges=merges)
    if pending.get("doc_version"):
        st.data["doc_version"] = pending["doc_version"]
        st.save()
    os.remove(st.pending_path)
    emit({"ok": True, "doc_hash": doc["hash"], "summary": report["summary"],
          "ids": key_to_id, "conflicts": report["conflicts"]})


def accept_docs(args, project):
    """Apply every pending diff of a multi-doc project in one step, so items
    that moved between docs keep their state, branch and history."""
    pend = {}
    for st in project.docs():
        p = pending_doc(st)
        if p:
            pend[st.name] = (st,) + p
    if not pend:
        raise DddError("no pending diff; run `ddd.py diff --doc NAME` first")

    def split_key(value, what):
        if ":" in value:
            d, k = value.split(":", 1)
            if d not in pend:
                raise DddError("%s %s: doc %s has no pending diff" % (what, value, d))
            return d, k
        if len(pend) == 1:
            return next(iter(pend)), value
        raise DddError("%s %s: qualify keys as DOC:KEY when several docs have pending diffs" % (what, value))

    forced_moves = {}
    for qkey, src in parse_pairs(args.move):
        d, k = split_key(qkey, "--move")
        if ":" not in src:
            raise DddError("--move %s=%s: the source is DOC:ID" % (qkey, src))
        forced_moves["%s:%s" % (d, k)] = src
    rejected = {"%s:%s" % split_key(k, "--no-move") for k in args.no_move or []}
    moves = find_moves(project, forced_moves, rejected)
    unresolved_moves = [m for m in moves if not m["confident"] and not args.accept_proposed]
    if unresolved_moves:
        raise DddError("possible moves between docs need a decision: --move DOC:KEY=DOC:ID to confirm, "
                       "--no-move DOC:KEY to keep it new, or --accept-proposed", code=2,
                       proposed_moves=unresolved_moves)
    if args.doc_name:
        other = [m for m in moves if args.doc_name in (m["to"], m["from"].split(":", 1)[0])]
        if other or len(pend) > 1:
            raise DddError("accept every doc together (omit --doc): pending diffs in %s%s"
                           % (", ".join(pend), "; items move between docs" if other else ""))

    plans = {}
    for name, (st, pending, doc, _) in pend.items():
        forced, force_new, splits, merges = {}, set(), {}, []
        for qkey, iid in parse_pairs(args.match):
            d, k = split_key(qkey, "--match")
            if d == name:
                forced[k] = st.resolve_id(iid.split(":", 1)[1] if iid.startswith(name + ":") else iid)
        for v in args.new or []:
            d, k = split_key(v, "--new")
            if d == name:
                force_new.add(k)
        for old, keys in parse_pairs(args.split):
            d, _ = split_key(keys.split(",")[0].strip(), "--split")
            if d == name:
                oid = st.resolve_id(old.split(":", 1)[1] if old.startswith(name + ":") else old)
                splits[oid] = [split_key(k.strip(), "--split")[1] for k in keys.split(",") if k.strip()]
                force_new.update(splits[oid])
        for olds, key in parse_pairs(args.merge):
            d, k = split_key(key, "--merge")
            if d == name:
                ids = [st.resolve_id(o.strip().split(":", 1)[1] if o.strip().startswith(name + ":") else o.strip())
                       for o in olds.split(",") if o.strip()]
                merges.append((ids, k))
                forced.setdefault(k, ids[0])
        ins = {m["key"]: m["from"] for m in moves if m["to"] == name}
        force_new.update(ins)
        result = compute_diff(st.items, doc, forced=forced, force_new=force_new)
        unresolved = [p for p in result["report"]["proposed"] if p["key"] not in forced]
        if unresolved and not args.accept_proposed:
            raise DddError("uncertain matches in doc %s need a decision: pass --match %s:KEY=ID to confirm, "
                           "--new %s:KEY to reject, or --accept-proposed" % (name, name, name), code=2,
                           doc=name, proposed=unresolved)
        plans[name] = (st, pending, doc, result, splits, merges, ins)

    # Copy moved records before any doc changes, apply every diff, then drop
    # the records from the docs they left.
    outs = {}
    for m in moves:
        sdoc, sid = m["from"].split(":", 1)
        outs.setdefault(sdoc, set()).add(sid)
    out = {"ok": True, "docs": {}, "moves": moves}
    for name, (st, pending, doc, result, splits, merges, ins) in plans.items():
        imports = {}
        for key, src in ins.items():
            sdoc, sid = src.split(":", 1)
            rec = json.loads(json.dumps(project.doc(sdoc).items[sid]))
            if project.config["docs"][sdoc].get("legacy_trailers"):
                # commits made before the project had several docs carry bare ids
                rec["old_trailers"] = rec.get("old_trailers", []) + [sid] + rec.get("aliases", [])
            imports[key] = (src, rec)
        plans[name] = plans[name] + (imports,)
    for name, (st, pending, doc, result, splits, merges, ins, imports) in plans.items():
        report = apply_moves_to_report(result["report"], name, moves)
        key_to_id = apply_diff(st, doc, result, pending["text"], splits=splits, merges=merges,
                               imports=imports, moved_out=outs.get(name))
        if pending.get("doc_version"):
            st.data["doc_version"] = pending["doc_version"]
            st.save()
        os.remove(st.pending_path)
        out["docs"][name] = {"doc_hash": doc["hash"], "summary": report["summary"],
                             "ids": key_to_id, "conflicts": report["conflicts"]}
    for sdoc, sids in outs.items():
        st = project.doc(sdoc)
        for sid in sids:
            st.items.pop(sid, None)
        st.data["items"] = st.items
        st.save()
    emit(out)


def selected_docs(args):
    """The docs a reporting command covers: --doc, else all of them."""
    project = load_project(args)
    if getattr(args, "doc_name", None):
        return [project.resolve_doc(args.doc_name)]
    return project.docs()


def cmd_rank(args):
    docs = selected_docs(args)
    cands, blocked, waiting = [], [], []
    for i, st in enumerate(docs):
        c, b, w = candidates(st, queued_only=args.queued)
        for x in c:
            x["_doc"] = i
        cands += c
        blocked += b
        waiting += w
    if args.queued:  # docs in config order, then doc order
        cands.sort(key=lambda c: (not c["signals"]["resume"], c["_doc"], c["order"]))
    else:
        cands.sort(key=lambda c: (-c["score"], c["_doc"], c["order"]))
    for c in cands:
        c.pop("_doc")
    emit({"candidates": cands[: args.limit] if args.limit else cands, "blocked": blocked,
          "waiting": waiting, "total": len(cands)})


def cmd_items(args):
    rows = []
    for st in selected_docs(args):
        for iid, r in sorted(st.items.items(), key=lambda kv: kv[1].get("order", 0)):
            if args.state and r.get("state") not in args.state:
                continue
            row = {"id": st.qid(iid), "title": r.get("title"), "kind": r.get("kind"), "state": r.get("state"),
                   "markers": sorted(effective(r)), "branch": r.get("branch"),
                   "flags": r.get("flags", []), "parent": r.get("parent")}
            if r.get("needs"):
                row["needs"] = r["needs"]
            rows.append(row)
    emit({"items": rows})


def cmd_show(args):
    st, iid = load_item(args)
    rec = dict(st.items[iid])
    if st.name:
        rec["doc"] = st.name
        rec["qid"] = st.qid(iid)
    if rec.get("needs"):
        open_needs, stack_on, unresolved = needs_status(st, rec)
        rec["needs_status"] = {"open": open_needs, "stack_on": stack_on, "unresolved": unresolved}
    emit(rec)


def cmd_set(args):
    st, iid = load_item(args)
    rec = st.items[iid]
    old = rec.get("state")
    if args.state:
        if args.state not in STATES:
            raise DddError("state must be one of %s" % ", ".join(STATES))
        if args.state == "blocked" and not (args.question or args.note or rec.get("question")):
            raise DddError("blocked needs --question or --note explaining why")
        rec["state"] = args.state
        if args.state == "done":
            rec["done_at"] = now_iso()
            rec.setdefault("done_by", "agent")
        if args.state != "blocked":
            rec.pop("question", None)
            rec.pop("blocked_reason", None)
    if args.question:
        rec["question"] = args.question
    if args.note:
        rec.setdefault("notes", []).append({"ts": now_iso(), "text": args.note})
        if args.state == "blocked":
            rec["blocked_reason"] = args.note
    if args.branch:
        rec["branch"] = args.branch
    for c in args.commit or []:
        rec.setdefault("commits", [])
        if c not in rec["commits"]:
            rec["commits"].append(c)
    if args.comment_id:
        rec.setdefault("comments", []).append({"id": args.comment_id, "kind": args.comment_kind,
                                               "ts": now_iso(), "state": rec.get("state")})
    if args.clear_flags:
        rec.pop("flags", None)
    st.save()
    st.log({"type": "state", "item": iid, "from": old, "to": rec.get("state"),
            "branch": args.branch, "commits": args.commit, "note": args.note,
            "question": args.question, "comment_id": args.comment_id})
    emit({"ok": True, "item": rec})


def cmd_start(args):
    st, iid = load_item(args)
    rec = st.items[iid]
    if rec.get("state") in ("done", "removed"):
        raise DddError("item %s is %s" % (iid, rec.get("state")))
    dirty = dirty_files(st)
    if dirty:
        raise DddError("working tree has uncommitted changes outside .ddd/; commit or stash first",
                       code=2, dirty=dirty)
    branch = rec.get("branch") or (st.config.get("branch_prefix") or "ddd/") + iid
    if current_branch(st) != branch:
        if branch_exists(st, branch):
            checkout(st, branch)
        else:
            checkout(st, branch, create_from=args.from_branch or base_branch(st))
    rec["state"] = "in-progress"
    rec["branch"] = branch
    rec.setdefault("started", now_iso())
    st.save()
    st.log({"type": "state", "item": iid, "to": "in-progress", "branch": branch})
    out = {"ok": True, "id": st.qid(iid), "branch": branch, "title": rec.get("title"),
           "body": rec.get("body"), "acceptance": rec.get("acceptance", []),
           "test_command": st.config.get("test_command"), "commit_trailer": st.trailer(iid)}
    if st.name:
        out["doc"] = st.name
    open_needs, stack_on, _ = needs_status(st, rec)
    if open_needs:
        out["needs_open"] = open_needs
        hint = ""
        if stack_on and not args.from_branch:
            hint = "; it can build on %s with `start --from %s`" % (stack_on, stack_on)
        out["warnings"] = ["%s needs %s, which is not done%s"
                           % (st.qid(iid), ", ".join(n["id"] for n in open_needs), hint)]
    emit(out)


def cmd_commit(args):
    st, iid = load_item(args)
    branch = st.items[iid].get("branch")
    if branch and current_branch(st) != branch:
        raise DddError("on %s, but %s is built on %s; run `ddd.py start %s`"
                       % (current_branch(st), st.qid(iid), branch, st.qid(iid)))
    code, staged = git(st, "diff", "--cached", "--name-only")
    files = [f for f in staged.splitlines() if f.strip()]
    if any(f == STATE_DIR or f.startswith(STATE_DIR + "/") for f in files):
        raise DddError(".ddd/ is staged; item branches must not commit ddd state. "
                       "Unstage it with `git restore --staged .ddd`")
    if not files:
        raise DddError("nothing staged; stage the item's files explicitly first")
    argv = ["commit"]
    for m in args.message:
        argv += ["-m", m]
    argv += ["-m", st.trailer(iid)]
    git(st, *argv)
    _, sha = git(st, "rev-parse", "HEAD")
    rec = st.items[iid]
    rec.setdefault("commits", []).append(sha)
    st.save()
    st.log({"type": "commit", "item": iid, "commit": sha, "branch": current_branch(st)})
    emit({"ok": True, "commit": sha, "files": files})


def trailer_grep(names):
    return "^%s: (%s)$" % (TRAILER, "|".join(re.escape(n) for n in names))


def cmd_finish(args):
    st, iid = load_item(args)
    rec = st.items[iid]
    branch = rec.get("branch")
    if not branch:
        raise DddError("item %s has no branch; use `start` first" % iid)
    if not args.tests_passed:
        raise DddError("refusing to mark %s awaiting-review without --tests-passed; run the tests, "
                       "or mark it blocked with a note" % iid)
    if current_branch(st) == branch and dirty_files(st):
        raise DddError("uncommitted changes on %s; commit them with `ddd.py commit`" % branch,
                       code=2, dirty=dirty_files(st))
    base = base_branch(st)
    # Only this item's commits: a branch stacked on another item's branch
    # (`start --from`) also contains the parent's commits, and recording those
    # would mark this item merged as soon as the parent is.
    _, out = git(st, "log", "--format=%H", "--extended-regexp",
                 "--grep", trailer_grep(st.trailer_names(iid)), "%s..%s" % (base, branch), check=False)
    commits = [c for c in out.splitlines() if c.strip()]
    if not commits:
        raise DddError("branch %s has no commits with the trailer `%s` beyond %s; commit with "
                       "`ddd.py commit %s`" % (branch, st.trailer(iid), base, st.qid(iid)))
    for c in commits:
        if c not in rec.setdefault("commits", []):
            rec["commits"].append(c)
    rec["state"] = "awaiting-review"
    rec["built_at"] = now_iso()
    rec.pop("flags", None)
    if args.note:
        rec.setdefault("notes", []).append({"ts": now_iso(), "text": args.note})
    st.save()
    st.log({"type": "state", "item": iid, "to": "awaiting-review", "branch": branch,
            "commits": commits, "tests": args.tests_passed, "note": args.note})
    # Untracked files don't block, but a source file the agent forgot to add
    # would leave the branch broken, so surface them.
    untracked = untracked_files(st) if current_branch(st) == branch else []
    if not args.stay:
        checkout(st, base)
    emit({"ok": True, "id": st.qid(iid), "branch": branch, "commits": commits,
          "now_on": current_branch(st), "untracked": untracked})


def cmd_checkout(args):
    st = load_project(args)
    checkout(st, args.branch)
    emit({"ok": True, "now_on": current_branch(st)})


def cmd_sync_git(args):
    project = load_project(args)
    base = base_branch(project)
    merged = []
    for st in project.docs():
        changed = False
        for iid, rec in st.items.items():
            if rec.get("state") != "awaiting-review":
                continue
            hit = None
            for c in rec.get("commits", []):
                code, _ = git(st, "merge-base", "--is-ancestor", c, base, check=False)
                if code == 0:
                    hit = "merged"
                    break
            if not hit:
                _, out = git(st, "log", base, "--format=%H", "--extended-regexp",
                             "--grep", trailer_grep(st.trailer_names(iid)), check=False)
                if out.strip():
                    hit = "squash-merged"
            if hit:
                rec["state"] = "done"
                rec["done_at"] = now_iso()
                rec["done_by"] = "merge"
                changed = True
                merged.append({"id": st.qid(iid), "title": rec.get("title"), "how": hit})
                st.log({"type": "state", "item": iid, "from": "awaiting-review", "to": "done", "how": hit})
        if changed:
            st.save()
    emit({"ok": True, "merged": merged})


def cmd_status_block(args):
    st = load_state(args)
    saved = os.path.join(st.tmp_dir, "gdoc-status.json")
    tab = args.tab or st.config["doc"].get("tab")
    if args.gdoc_check:
        written = load_json(saved)
        if not written:
            raise DddError("nothing to check: run `status-block --gdoc-read` and apply its requests first")
        diffs = gdoc_status_check(load_gdoc_json(args.gdoc_check), written["expected"],
                                  st.status_title(), written.get("tabId") or tab)
        if diffs:
            raise DddError("the status section doesn't match what was written", differences=diffs)
        st.log({"type": "status-write", "doc": "gdoc", "tab": written.get("tabId")})
        emit({"ok": True, "paragraphs": len(written["expected"])})
        return
    block = render_status(st)
    if args.gdoc_read:
        out = gdoc_status_requests(load_gdoc_json(args.gdoc_read), block, st.status_title(), tab)
        os.makedirs(st.tmp_dir, exist_ok=True)
        save_json(saved, {"tabId": out["tabId"], "expected": out["expected"]})
        out["block"] = block
        emit(out)
        return
    if not args.write:
        emit({"block": block, "title": st.status_title()})
        return
    path = args.write
    doc_path = st.config["doc"].get("path")
    if st.name and doc_path and os.path.abspath(path) != os.path.abspath(os.path.join(st.root, doc_path)):
        raise DddError("doc %s is %s, not %s; pass --doc to pick the doc" % (st.name, doc_path, path))
    text = read_text(path)
    current = doc_hash(text, st.status_title())
    expected = args.expect_hash or st.data.get("doc_hash")
    if current != expected:
        raise DddError("the doc changed since the last sync (hash %s, expected %s); re-run diff before "
                       "writing" % (current, expected), code=3)
    out = replace_status(text, block, st.status_title())
    if "\r\n" in text:  # keep the doc's line endings (autocrlf checkouts)
        out = out.replace("\n", "\r\n")
    write_text(path, out)
    st.log({"type": "status-write", "path": os.path.relpath(path, st.root).replace("\\", "/")})
    emit({"ok": True, "written": path, "block": block})


def cmd_commit_state(args):
    st = load_project(args)
    base = base_branch(st)
    if current_branch(st) != base:
        raise DddError("ddd state is committed only on the base branch (%s); you are on %s. "
                       "Leave .ddd/ uncommitted here (never `git add` it on this branch) and run "
                       "commit-state after switching to %s" % (base, current_branch(st), base))
    git(st, "add", "--", STATE_DIR)
    code, out = git(st, "diff", "--cached", "--name-only", "--", STATE_DIR)
    if not out.strip():
        emit({"ok": True, "committed": False})
        return
    git(st, "commit", "-m", args.message or "chore(ddd): sync state", "--", STATE_DIR)
    _, sha = git(st, "rev-parse", "HEAD")
    emit({"ok": True, "committed": True, "commit": sha})


def cmd_config(args):
    """Get or set config. With several docs, --doc reads the doc's merged
    config and sets its own settings (test_command, branch_prefix,
    status_section, doc.*); without --doc, the repo-wide config."""
    project = load_project(args)
    name = getattr(args, "doc_name", None)
    st = project.resolve_doc(name) if name else None
    if args.action == "get":
        node = st.config if st else project.config
        for part in (args.key.split(".") if args.key else []):
            node = node.get(part) if isinstance(node, dict) else None
        emit({"key": args.key, "value": node})
        return
    if not args.key or args.value is None:
        raise DddError("usage: config set KEY VALUE")
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    parts = args.key.split(".")
    if project.multi and st:
        if parts[0] == "doc":
            parts = parts[1:]
        elif parts[0] not in DOC_SETTINGS:
            raise DddError("%s is repo-wide; set it without --doc (per-doc settings: %s, doc.*)"
                           % (args.key, ", ".join(DOC_SETTINGS)))
        if not parts:
            raise DddError("usage: config set doc.FIELD VALUE --doc NAME")
        node = project.config["docs"][st.name]
    else:
        if project.multi and parts[0] in ("doc", "docs", "doc_order"):
            raise DddError("%s belongs to one doc; pass --doc NAME (or use `ddd.py doc`)" % args.key)
        node = project.config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    project.save_config()
    (st or project).log({"type": "config", "key": args.key, "value": value})
    warnings = []
    if args.key == "test_command" and isinstance(value, str):
        warnings = test_command_warnings(project.root, value)
    emit({"ok": True, "key": args.key, "value": value, "warnings": warnings})


def cmd_log(args):
    project = load_project(args)
    rows = []
    want_doc, want_item = getattr(args, "doc_name", None), args.item
    if want_item and ":" in want_item:
        want_doc, want_item = want_item.split(":", 1)
    if os.path.exists(project.log_path):
        with open(project.log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if want_doc and row.get("doc", want_doc) != want_doc:
                    continue
                if want_item and row.get("item") != want_item and want_item not in (row.get("items") or []):
                    continue
                rows.append(row)
    emit({"entries": rows[-args.limit:] if args.limit else rows})


# --------------------------------------------------------------------------
# survey: facts about the project for Initialize mode (no .ddd/ needed)

SKIP_DIRS = {".git", ".ddd", "node_modules", ".venv", "venv", "env", "__pycache__", "dist",
             "build", "target", "out", "bin", "obj", ".idea", ".vscode", ".gradle", "vendor",
             ".next", ".nuxt", "coverage", ".tox", ".mypy_cache", ".pytest_cache"}
MANIFESTS = ("package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
             "Pipfile", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
             "settings.gradle", "settings.gradle.kts", "Gemfile", "composer.json", "CMakeLists.txt",
             "Makefile", "Dockerfile", "docker-compose.yml", "compose.yaml", "mix.exs",
             "pubspec.yaml", "Package.swift", "deno.json", "tsconfig.json")
CI_PATTERNS = (r"^\.github/workflows/[^/]+\.ya?ml$", r"^\.gitlab-ci\.yml$", r"^\.circleci/",
               r"^Jenkinsfile$", r"^azure-pipelines\.yml$", r"^\.travis\.yml$")
PLAN_NAME = re.compile(r"(plan|roadmap|todo|backlog|spec|milestone)", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_[^/]+$|_test\.[^/]+$|"
                       r"\.(test|spec)\.[^/]+$|Tests?\.[^/]+$")
CODE_MARKER = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b(?:\([^)]*\))?(?::|\s|$)(.*)")


def project_files(root):
    if git(root, "rev-parse", "--git-dir", check=False)[0] == 0:
        _, out = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", check=False)
        files = [f for f in out.split("\0") if f]
        return sorted(f for f in files
                      if not any(part in SKIP_DIRS for part in f.split("/")[:-1])
                      and os.path.isfile(os.path.join(root, f)))
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel = os.path.relpath(dirpath, root).replace("\\", "/")
        files.extend(fn if rel == "." else rel + "/" + fn for fn in filenames)
    return sorted(files)


def read_small_text(path, limit=1_000_000):
    """Text of a file, or None for large or binary files."""
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def survey_git(root):
    if git(root, "rev-parse", "--git-dir", check=False)[0] != 0:
        return {"repo": False}
    info = {"repo": True}
    code, branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    info["branch"] = branch if code == 0 else None
    code, count = git(root, "rev-list", "--count", "HEAD", check=False)
    info["commits"] = int(count) if code == 0 and count.isdigit() else 0
    if info["commits"]:
        _, first = git(root, "log", "--reverse", "--format=%cs", check=False)
        _, recent = git(root, "log", "-15", "--format=%cs %s", check=False)
        info["first_commit"] = first.splitlines()[0] if first else None
        info["recent"] = recent.splitlines()
        _, unmerged = git(root, "branch", "--no-merged", "HEAD", "--format=%(refname:short)", check=False)
        info["unmerged_branches"] = [b for b in unmerged.splitlines() if b.strip()]
    code, head = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    guess = head.split("/", 1)[1] if code == 0 and "/" in head else None
    if not guess:
        _, local = git(root, "branch", "--format=%(refname:short)", check=False)
        guess = next((b for b in ("main", "master", "trunk", "develop") if b in local.split()), None)
    info["base_guess"] = guess
    info["on_base"] = guess is not None and info["branch"] == guess
    _, status = git(root, "status", "--porcelain", check=False)
    info["dirty_files"] = len([l for l in status.splitlines() if l.strip()])
    return info


def related_items(root, scope):
    """Tracked items that are about `scope`: they name its path, or its folder
    name in their title, tag or body. A doc planned for that folder should
    carry them over (references/multi-doc.md, "A new doc for part of the
    project"). The rest of a section comes along only when its heading is
    about the folder (title or tag): a passing mention in a section's text,
    or in the doc's intro, lists that item alone. The doc's title heading is
    left out."""
    if not os.path.exists(os.path.join(root, STATE_DIR, "config.json")):
        return []
    project = Project(root)
    words = set(name_words(scope.rsplit("/", 1)[-1]))
    out = []
    for st in (project.docs() if project.multi else [project.doc()]):
        if (st.config["doc"].get("scope") or "").strip("/") == scope:
            continue  # the folder's own doc
        ordered = sorted(st.items.items(), key=lambda kv: kv[1].get("order", 0))
        first = ordered[0][1] if ordered else {}
        title_id = ordered[0][0] if first.get("kind") == "heading" and first.get("level") == 1 else None
        sections = set()  # headings about the scope, and what is under them
        for sid, rec in ordered:
            if rec.get("state") == "removed" or sid == title_id:
                continue
            title, body = rec.get("title") or "", rec.get("body") or ""
            text = (title + "\n" + body).lower()
            why = [w for w, hit in (("path", scope.lower() + "/" in text or ("/" in scope and scope.lower() in text)),
                                    ("title", bool(words & set(name_words(title + " " + (rec.get("tag") or ""))))),
                                    ("body", bool(words & set(name_words(body))))) if hit]
            if rec.get("parent") in sections:
                why.append("in section")
            if "title" in why or "in section" in why:
                sections.add(sid)
            if why:
                out.append({"id": st.qid(sid), "doc": st.name, "title": rec.get("title"), "kind": rec.get("kind"),
                            "parent": st.qid(rec["parent"]) if rec.get("parent") else None,
                            "line": rec.get("line"), "tag": rec.get("tag"), "state": rec.get("state"), "why": why})
    return out


def survey_scope_git(root, scope):
    if git(root, "rev-parse", "--git-dir", check=False)[0] != 0:
        return {"repo": False}
    _, count = git(root, "rev-list", "--count", "HEAD", "--", scope, check=False)
    _, recent = git(root, "log", "-15", "--format=%cs %s", "--", scope, check=False)
    _, unmerged = git(root, "branch", "--no-merged", "HEAD", "--format=%(refname:short)", check=False)
    touching = [b for b in unmerged.splitlines() if b.strip()
                and git(root, "log", "-1", "--format=%h", "HEAD.." + b.strip(), "--", scope, check=False)[1]]
    return {"commits": int(count) if count.isdigit() else 0, "recent": recent.splitlines(),
            "unmerged_branches": touching}


def cmd_survey(args):
    root = os.path.abspath(args.root or os.getcwd())
    files = all_files = project_files(root)
    scope = None
    if args.scope:
        scope = os.path.relpath(os.path.abspath(os.path.join(root, args.scope)), root).replace("\\", "/")
        if scope.startswith("..") or scope == "." or not os.path.isdir(os.path.join(root, scope)):
            raise DddError("--scope must be a folder inside the project: %s" % args.scope)
        files = [f for f in all_files if f.startswith(scope + "/")]
    inner = (lambda f: f[len(scope) + 1:]) if scope else (lambda f: f)  # noqa: E731
    exts, top = {}, {}
    for f in files:
        ext = os.path.splitext(f)[1].lower() or os.path.basename(f)
        exts[ext] = exts.get(ext, 0) + 1
        rel = inner(f)
        head = (scope + "/" if scope else "") + rel.split("/")[0] + ("/" if "/" in rel else "")
        top[head] = top.get(head, 0) + 1
    base = lambda f: f.rsplit("/", 1)[-1]  # noqa: E731
    docs = sorted((f for f in files if f.lower().endswith((".md", ".rst", ".txt", ".adoc"))),
                  key=lambda f: (f.count("/"), f))
    markers, total = [], 0
    for f in files:
        text = read_small_text(os.path.join(root, f))
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = CODE_MARKER.search(line)
            if m:
                total += 1
                if len(markers) < args.limit:
                    markers.append({"path": f, "line": n, "tag": m.group(1),
                                    "text": m.group(2).strip()[:160]})
    scripts = None
    package = (scope + "/" if scope else "") + "package.json"
    if package in files:
        try:
            scripts = sorted((json.loads(read_text(os.path.join(root, package))).get("scripts") or {}))
        except (ValueError, AttributeError):
            scripts = None
    tests = [f for f in files if TEST_PATH.search(f)]
    extra = {}
    if scope:
        test, why = scope_test_command(root, all_files, scope)
        extra["scope"] = {"path": scope, "test_command": {"suggested": test, "why": why},
                          "git": survey_scope_git(root, scope), "related_items": related_items(root, scope)}
    emit(dict({
        "root": root,
        "name": scope.rsplit("/", 1)[-1] if scope else os.path.basename(root),
        "ddd_initialised": os.path.exists(os.path.join(root, STATE_DIR, "config.json")),
        "git": survey_git(root),
        "files": len(files),
        "extensions": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:15]),
        "top_level": dict(sorted(top.items())),
        "manifests": [f for f in files if base(f) in MANIFESTS and inner(f).count("/") <= 2]
                     + [f for f in files if f.endswith((".csproj", ".sln")) and inner(f).count("/") <= 2],
        "package_scripts": scripts,
        "tests": {"files": len(tests), "sample": tests[:20]},
        "ci": [f for f in all_files if any(re.search(p, f) for p in CI_PATTERNS)],
        "readme": [f for f in files if base(f).lower().startswith("readme") and inner(f).count("/") <= 1],
        "docs": docs[:30],
        "plan_candidates": [f for f in docs if PLAN_NAME.search(base(f))],
        "code_markers": {"total": total, "items": markers},
    }, **extra))


DOC_HELP = "with several docs: the doc to use (default: the only doc, or the doc whose scope holds the cwd)"


def add_doc_option(s, help_text=DOC_HELP):
    s.add_argument("--doc", dest="doc_name", metavar="NAME", help=help_text)


def build_parser():
    p = argparse.ArgumentParser(prog="ddd.py", description="Document-Driven Development state tracker")
    p.add_argument("--root", help="project root (default: nearest parent with .ddd/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create .ddd/ for a planning doc")
    s.add_argument("--doc", required=True, help="Markdown path (relative to root) or Google Docs URL")
    s.add_argument("--base", help="base branch (default: current branch)")
    s.add_argument("--test", help="test command")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("info", help="config, counts, git status")
    add_doc_option(s, "with several docs: only this doc (default: all)")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("doc", help="several docs: suggest (best-guess settings for a doc, no changes), add "
                                   "(the first add names the existing doc), list, rename, remove")
    s.add_argument("action", choices=("suggest", "add", "list", "rename", "remove"))
    s.add_argument("name", nargs="?", help="doc name (rename: the old name; suggest: the Markdown path or "
                                           "Google Docs URL)")
    s.add_argument("source", nargs="?", help="add: Markdown path or Google Docs URL; rename: the new name")
    s.add_argument("--file", help="suggest: the doc's text, fetched and saved (needed for a Google Doc); add: "
                                  "that text, kept for the first sync (with --doc-version)")
    s.add_argument("--doc-version", help="add, with --file: the Google Doc's modifiedTime from before that fetch; "
                                         "the first sync reuses the text while the doc is unchanged")
    s.add_argument("--scope", help="add: folder whose work this doc plans (picks the doc by cwd)")
    s.add_argument("--test", help="add: the doc's own test command (default: the repo's)")
    s.add_argument("--first-name", help="add, single-doc project: the name for the existing doc")
    s.add_argument("--wait-for", action="append", metavar="ID",
                   help="add: hold the new doc's first sync until this item (copied into it) has left its "
                        "own doc, so it moves with its state; repeatable")
    s.add_argument("--force", action="store_true", help="remove: even with active items")
    s.set_defaults(func=cmd_doc)

    s = sub.add_parser("reformat", help="the doc in another style (references/reformat.md): check a draft keeps "
                                        "every item; write it over a Markdown doc; relink to a new Google Doc")
    s.add_argument("action", choices=("check", "write", "relink"))
    s.add_argument("target", help="check, write: the draft (Markdown, .b64 or a saved read-back); relink: the "
                                  "new Google Doc's URL")
    s.add_argument("--old-version", metavar="V", help="relink: the old doc's modifiedTime now; refused if it "
                                                     "changed since the last sync")
    add_doc_option(s)
    s.set_defaults(func=cmd_reformat)

    s = sub.add_parser("parse", help="parse a doc and print its items (no state needed)")
    s.add_argument("file", help="Markdown, a .b64 file, or a saved connector result (JSON)")
    s.add_argument("--outline", action="store_true", help="one line per item instead of full JSON")
    s.add_argument("--compare", metavar="OTHER", help="report what differs in OTHER (e.g. a read-back)")
    s.set_defaults(func=cmd_parse)

    s = sub.add_parser("survey", help="facts about the project for drafting a plan (no state needed)")
    s.add_argument("--limit", type=int, default=200, help="max code markers (TODO, FIXME, ...) listed")
    s.add_argument("--scope", metavar="DIR", help="only this folder (a new doc for part of the project); adds "
                   "its test command, git history and the tracked items about it")
    s.set_defaults(func=cmd_survey)

    s = sub.add_parser("diff", help="compare the doc with the last sync")
    s.add_argument("file", nargs="?", help="doc text (required for Google Docs): Markdown, .b64, or a "
                   "saved connector result")
    s.add_argument("--doc-version", help="the doc's modified time or revision id; without FILE, "
                   "reports whether a fetch is needed")
    add_doc_option(s)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("accept", help="apply the pending diff")
    s.add_argument("--match", action="append", metavar="KEY=ID", help="confirm or force a match")
    s.add_argument("--new", action="append", metavar="KEY", help="treat a doc item as new")
    s.add_argument("--split", action="append", metavar="ID=KEY,KEY", help="an item was split")
    s.add_argument("--merge", action="append", metavar="ID,ID=KEY", help="items were merged")
    s.add_argument("--accept-proposed", action="store_true", help="accept every uncertain match (and move)")
    s.add_argument("--move", action="append", metavar="DOC:KEY=DOC:ID",
                   help="several docs: the item at KEY moved here from another doc")
    s.add_argument("--no-move", action="append", metavar="DOC:KEY", help="several docs: KEY is a new item")
    add_doc_option(s, "several docs: accept only this doc (refused when items move between docs)")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("rank", help="ranked work candidates")
    s.add_argument("--queued", action="store_true", help="only [next] items, in doc order (auto mode)")
    s.add_argument("--limit", type=int, default=10)
    add_doc_option(s, "with several docs: only this doc (default: all)")
    s.set_defaults(func=cmd_rank)

    s = sub.add_parser("items", help="list tracked items")
    s.add_argument("--state", action="append", choices=STATES)
    add_doc_option(s, "with several docs: only this doc (default: all)")
    s.set_defaults(func=cmd_items)

    s = sub.add_parser("show", help="full record for one item")
    s.add_argument("id")
    add_doc_option(s)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("set", help="update an item's state or metadata")
    s.add_argument("id")
    s.add_argument("state", nargs="?")
    s.add_argument("--branch")
    s.add_argument("--commit", action="append")
    s.add_argument("--note")
    s.add_argument("--question")
    s.add_argument("--comment-id")
    s.add_argument("--comment-kind", default="status", choices=("status", "question"))
    s.add_argument("--clear-flags", action="store_true")
    add_doc_option(s)
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("start", help="check out the item's branch and mark it in-progress")
    s.add_argument("id")
    s.add_argument("--from", dest="from_branch", help="branch to fork from (default: base)")
    add_doc_option(s)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("commit", help="commit staged changes with the item trailer")
    s.add_argument("id")
    s.add_argument("-m", "--message", action="append", required=True)
    add_doc_option(s)
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("finish", help="mark built item awaiting-review and return to base")
    s.add_argument("id")
    s.add_argument("--tests-passed", metavar="SUMMARY", help="what ran and passed")
    s.add_argument("--note")
    s.add_argument("--stay", action="store_true", help="stay on the item branch")
    add_doc_option(s)
    s.set_defaults(func=cmd_finish)

    s = sub.add_parser("checkout", help="switch branch keeping .ddd/ state")
    s.add_argument("branch")
    s.set_defaults(func=cmd_checkout)

    s = sub.add_parser("sync-git", help="mark merged items done")
    s.set_defaults(func=cmd_sync_git)

    s = sub.add_parser("status-block", help="render (or write) the agent-owned status section")
    s.add_argument("--write", metavar="PATH", help="local Markdown doc to update in place")
    s.add_argument("--expect-hash", help="doc hash the write is based on (default: last sync)")
    s.add_argument("--gdoc-read", metavar="READ",
                   help="Google Doc: a saved read_doc (documents.get) result; prints the guarded "
                        "batchUpdate requests that replace or append the status section")
    s.add_argument("--gdoc-check", metavar="READ",
                   help="Google Doc: a read taken after applying those requests; checks the section")
    s.add_argument("--tab", help="Google Doc tab id (default: the doc URL's ?tab=, else the first tab)")
    add_doc_option(s)
    s.set_defaults(func=cmd_status_block)

    s = sub.add_parser("commit-state", help="commit .ddd/ on the base branch")
    s.add_argument("-m", "--message")
    s.set_defaults(func=cmd_commit_state)

    s = sub.add_parser("config", help="get or set config values")
    s.add_argument("action", choices=("get", "set"))
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    add_doc_option(s, "with several docs: the doc's own settings (default: repo-wide)")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("log", help="audit log")
    s.add_argument("--item", help="item id (DOC:ID with several docs)")
    s.add_argument("--limit", type=int, default=50)
    add_doc_option(s, "with several docs: only this doc's entries")
    s.set_defaults(func=cmd_log)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except DddError as e:
        emit(dict({"ok": False, "error": str(e)}, **e.extra))
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
