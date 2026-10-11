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
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys

SCHEMA_VERSION = 1
STATE_DIR = ".ddd"
STATES = ("todo", "in-progress", "awaiting-review", "done", "blocked", "removed")
ACTIVE = ("in-progress", "awaiting-review")
BUILT = ("in-progress", "awaiting-review", "done")
MARKERS = ("next", "blocked", "notes")
TRAILER = "DDD-Item"

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
            continue
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

class State:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, STATE_DIR)
        self.config_path = os.path.join(self.dir, "config.json")
        self.items_path = os.path.join(self.dir, "items.json")
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self.snapshot_path = os.path.join(self.dir, "snapshot.md")
        self.tmp_dir = os.path.join(self.dir, "tmp")
        self.pending_path = os.path.join(self.tmp_dir, "pending.json")
        self.config = merge_defaults(load_json(self.config_path, {}), DEFAULT_CONFIG)
        data = load_json(self.items_path, None) or {"version": SCHEMA_VERSION, "items": {},
                                                    "doc_hash": None, "last_sync": None}
        self.data = data
        self.items = data["items"]

    def save(self):
        save_json(self.items_path, self.data)

    def save_config(self):
        save_json(self.config_path, self.config)

    def log(self, entry):
        entry = dict(entry)
        entry.setdefault("ts", now_iso())
        with open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def resolve_id(self, ident):
        if ident in self.items:
            return ident
        for iid, it in self.items.items():
            if ident in it.get("aliases", []):
                return iid
        raise DddError("unknown item id: %s" % ident)

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


def load_state(args):
    root = args.root or find_root(os.getcwd())
    if not root or not os.path.isdir(os.path.join(root, STATE_DIR)):
        raise DddError("no .ddd/ found; run `ddd.py init --doc <path-or-url>` at the project root")
    return State(os.path.abspath(root))


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


def apply_diff(state, doc, result, text, splits=None, merges=None):
    prev = state.items
    ts = now_iso()
    matches = result["matches"]
    report = result["report"]
    taken = set(prev)
    for it in prev.values():
        taken.update(it.get("aliases", []))
    key_to_id = {}
    new_items = {iid: dict(it) for iid, it in prev.items()}
    changes = {c["key"]: c for c in report["changed"]}

    for it in doc["items"]:
        m = matches.get(it["key"])
        if m:
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
                      "line", "order", "body", "acceptance", "title_norm", "body_norm"):
            rec[field] = it[field]
        rec["parent_key"] = it["parent"]
        rec["seen"] = ts

    for rec in new_items.values():
        if "parent_key" in rec:
            pk = rec.pop("parent_key")
            rec["parent"] = key_to_id.get(pk) if pk else None

    seen_ids = set(key_to_id.values())
    for iid, rec in new_items.items():
        if iid in seen_ids or rec.get("state") == "removed":
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
        state.log({"type": "doc-change", "item": key_to_id[n["key"]], "changes": ["new"],
                   "title": n["title"], "doc_hash": doc["hash"]})
    for r in report["removed"]:
        state.log({"type": "doc-change", "item": r["id"], "changes": ["removed"],
                   "title": r["title"], "doc_hash": doc["hash"]})
    for c in report["conflicts"]:
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


def candidates(state, queued_only=False):
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
    out, blocked = [], []
    for iid, r in live.items():
        eff = effective(r)
        if "notes" in eff or r.get("state") in ("done", "awaiting-review") or r.get("checked"):
            continue
        if open_children.get(iid) or iid in has_children:
            continue
        if r.get("kind") == "heading" and not r.get("body"):
            continue
        if "blocked" in eff or r.get("state") == "blocked":
            blocked.append({"id": iid, "title": r.get("title"),
                            "reason": r.get("blocked_reason") or ("[blocked] in doc" if "blocked" in eff else None),
                            "question": r.get("question")})
            continue
        queued = "next" in eff
        if queued_only and not queued:
            continue
        resume = r.get("state") == "in-progress"
        hot = hot_score(r, now, half)
        is_new = bool(last_sync and r.get("created") == last_sync)
        order_bonus = 1.0 - r.get("order", 0) / total
        score = (200 if resume else 0) + (100 if queued else 0) + 10 * hot + (5 if is_new else 0) + 5 * order_bonus
        parent = items.get(r.get("parent")) if r.get("parent") else None
        out.append({
            "id": iid, "title": r.get("title"), "kind": r.get("kind"), "state": r.get("state"),
            "parent": parent.get("title") if parent else None, "branch": r.get("branch"),
            "score": round(score, 2), "order": r.get("order", 0),
            "signals": {"queued": queued, "resume": resume, "hot": hot, "new": is_new,
                        "flags": r.get("flags", [])},
            "acceptance": r.get("acceptance", []), "body": (r.get("body") or "")[:400],
        })
    if queued_only:
        out.sort(key=lambda c: (not c["signals"]["resume"], c["order"]))
    else:
        out.sort(key=lambda c: (-c["score"], c["order"]))
    return out, blocked


# --------------------------------------------------------------------------
# status section

def render_status(state):
    items = state.items
    title = state.status_title()
    groups = {"in-progress": [], "awaiting-review": [], "blocked": [], "done": []}
    for iid, r in sorted(items.items(), key=lambda kv: kv[1].get("order", 0)):
        st = r.get("state")
        if st in groups:
            groups[st].append((iid, r))
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
        return "- %s%s%s" % (r.get("title"), tag, (" - " + "; ".join(bits)) if bits else "")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = ["## %s" % title, "",
           "_Maintained by the ddd agent; edits in this section are overwritten. Last sync: %s._" % stamp, ""]
    labels = (("in-progress", "In progress"), ("awaiting-review", "Awaiting review"),
              ("blocked", "Blocked / questions"), ("done", "Recently done"))
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
# git

def git(state_or_root, *argv, check=True):
    root = state_or_root.root if isinstance(state_or_root, State) else state_or_root
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
    Guarded by tests/test_ddd.py: test_auto_builds_second_item_after_status_write,
    test_developer_doc_edits_survive_branch_switches, test_state_floats_across_branches."""
    paths = [STATE_DIR]
    doc = state.config.get("doc") or {}
    if doc.get("type") == "markdown" and doc.get("path") and not doc["path"].startswith(".."):
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
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) != state.tmp_dir]
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


# --------------------------------------------------------------------------
# commands

def cmd_init(args):
    root = os.path.abspath(args.root or os.getcwd())
    d = os.path.join(root, STATE_DIR)
    if os.path.exists(os.path.join(d, "config.json")) and not args.force:
        raise DddError(".ddd/ already initialised at %s (use --force to overwrite config)" % root)
    os.makedirs(os.path.join(d, "tmp"), exist_ok=True)
    cfg = merge_defaults({}, DEFAULT_CONFIG)
    doc = args.doc
    m = re.search(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)", doc)
    if m:
        cfg["doc"] = {"type": "gdoc", "url": doc, "id": m.group(1), "path": None}
    else:
        rel = os.path.relpath(os.path.abspath(os.path.join(root, doc)), root).replace("\\", "/")
        cfg["doc"] = {"type": "markdown", "path": rel, "url": None, "id": None}
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
    emit({"ok": True, "root": root, "config": cfg})


def cmd_info(args):
    st = load_state(args)
    counts = {}
    for r in st.items.values():
        counts[r.get("state", "todo")] = counts.get(r.get("state", "todo"), 0) + 1
    info = {"root": st.root, "config": st.config, "doc_hash": st.data.get("doc_hash"),
            "last_sync": st.data.get("last_sync"), "states": counts,
            "pending_diff": os.path.exists(st.pending_path),
            "flagged": {iid: r["flags"] for iid, r in st.items.items() if r.get("flags")}}
    if os.path.isdir(os.path.join(st.root, ".git")) or git(st, "rev-parse", "--git-dir", check=False)[0] == 0:
        info["git"] = {"branch": current_branch(st), "dirty": dirty_files(st)}
    emit(info)


def doc_text_for(state, path):
    if path:
        return read_text(path)
    cfg = state.config["doc"]
    if cfg.get("type") == "markdown" and cfg.get("path"):
        return read_text(os.path.join(state.root, cfg["path"]))
    raise DddError("the doc is a Google Doc: fetch it through the MCP connector, save it to "
                   ".ddd/tmp/doc.md, and pass that path")


def cmd_parse(args):
    st = State(args.root) if args.root else None
    text = read_text(args.file)
    emit(parse_doc(text, st.status_title() if st else "DDD Status"))


def cmd_diff(args):
    st = load_state(args)
    text = doc_text_for(st, args.file)
    doc = parse_doc(text, st.status_title())
    result = compute_diff(st.items, doc)
    report = result["report"]
    report["doc_hash"] = doc["hash"]
    report["previous_doc_hash"] = st.data.get("doc_hash")
    report["doc_unchanged"] = doc["hash"] == st.data.get("doc_hash")
    report["has_status_section"] = doc["has_status_section"]
    os.makedirs(st.tmp_dir, exist_ok=True)
    save_json(st.pending_path, {"text": text, "doc_hash": doc["hash"]})
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
    st = load_state(args)
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
    os.remove(st.pending_path)
    emit({"ok": True, "doc_hash": doc["hash"], "summary": report["summary"],
          "ids": key_to_id, "conflicts": report["conflicts"]})


def cmd_rank(args):
    st = load_state(args)
    cands, blocked = candidates(st, queued_only=args.queued)
    emit({"candidates": cands[: args.limit] if args.limit else cands, "blocked": blocked,
          "total": len(cands)})


def cmd_items(args):
    st = load_state(args)
    rows = []
    for iid, r in sorted(st.items.items(), key=lambda kv: kv[1].get("order", 0)):
        if args.state and r.get("state") not in args.state:
            continue
        rows.append({"id": iid, "title": r.get("title"), "kind": r.get("kind"), "state": r.get("state"),
                     "markers": sorted(effective(r)), "branch": r.get("branch"),
                     "flags": r.get("flags", []), "parent": r.get("parent")})
    emit({"items": rows})


def cmd_show(args):
    st = load_state(args)
    iid = st.resolve_id(args.id)
    emit(st.items[iid])


def cmd_set(args):
    st = load_state(args)
    iid = st.resolve_id(args.id)
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
    st = load_state(args)
    iid = st.resolve_id(args.id)
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
    emit({"ok": True, "id": iid, "branch": branch, "title": rec.get("title"),
          "body": rec.get("body"), "acceptance": rec.get("acceptance", []),
          "test_command": st.config.get("test_command"),
          "commit_trailer": "%s: %s" % (TRAILER, iid)})


def cmd_commit(args):
    st = load_state(args)
    iid = st.resolve_id(args.id)
    branch = st.items[iid].get("branch")
    if branch and current_branch(st) != branch:
        raise DddError("on %s, but %s is built on %s; run `ddd.py start %s`"
                       % (current_branch(st), iid, branch, iid))
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
    argv += ["-m", "%s: %s" % (TRAILER, iid)]
    git(st, *argv)
    _, sha = git(st, "rev-parse", "HEAD")
    rec = st.items[iid]
    rec.setdefault("commits", []).append(sha)
    st.save()
    st.log({"type": "commit", "item": iid, "commit": sha, "branch": current_branch(st)})
    emit({"ok": True, "commit": sha, "files": files})


def cmd_finish(args):
    st = load_state(args)
    iid = st.resolve_id(args.id)
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
                 "--grep", "^%s: %s$" % (TRAILER, re.escape(iid)), "%s..%s" % (base, branch), check=False)
    commits = [c for c in out.splitlines() if c.strip()]
    if not commits:
        raise DddError("branch %s has no commits with the trailer `%s: %s` beyond %s; commit with "
                       "`ddd.py commit %s`" % (branch, TRAILER, iid, base, iid))
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
    emit({"ok": True, "id": iid, "branch": branch, "commits": commits,
          "now_on": current_branch(st), "untracked": untracked})


def cmd_checkout(args):
    st = load_state(args)
    checkout(st, args.branch)
    emit({"ok": True, "now_on": current_branch(st)})


def cmd_sync_git(args):
    st = load_state(args)
    base = base_branch(st)
    merged = []
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
            names = [iid] + rec.get("aliases", [])
            for n in names:
                _, out = git(st, "log", base, "--format=%H", "--extended-regexp",
                             "--grep", "^%s: %s$" % (TRAILER, re.escape(n)), check=False)
                if out.strip():
                    hit = "squash-merged"
                    break
        if hit:
            rec["state"] = "done"
            rec["done_at"] = now_iso()
            rec["done_by"] = "merge"
            merged.append({"id": iid, "title": rec.get("title"), "how": hit})
            st.log({"type": "state", "item": iid, "from": "awaiting-review", "to": "done", "how": hit})
    if merged:
        st.save()
    emit({"ok": True, "merged": merged})


def cmd_status_block(args):
    st = load_state(args)
    block = render_status(st)
    if not args.write:
        emit({"block": block, "title": st.status_title()})
        return
    path = args.write
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
    st = load_state(args)
    base = base_branch(st)
    if current_branch(st) != base:
        raise DddError("ddd state is committed only on the base branch (%s); you are on %s"
                       % (base, current_branch(st)))
    git(st, "add", "--", STATE_DIR)
    code, out = git(st, "diff", "--cached", "--name-only", "--", STATE_DIR)
    if not out.strip():
        emit({"ok": True, "committed": False})
        return
    git(st, "commit", "-m", args.message or "chore(ddd): sync state", "--", STATE_DIR)
    _, sha = git(st, "rev-parse", "HEAD")
    emit({"ok": True, "committed": True, "commit": sha})


def cmd_config(args):
    st = load_state(args)
    if args.action == "get":
        node = st.config
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
    node = st.config
    parts = args.key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    st.save_config()
    st.log({"type": "config", "key": args.key, "value": value})
    emit({"ok": True, "key": args.key, "value": value})


def cmd_log(args):
    st = load_state(args)
    rows = []
    if os.path.exists(st.log_path):
        with open(st.log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if args.item and row.get("item") != args.item and args.item not in (row.get("items") or []):
                    continue
                rows.append(row)
    emit({"entries": rows[-args.limit:] if args.limit else rows})


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
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("parse", help="parse a doc and print its items (no state needed)")
    s.add_argument("file")
    s.set_defaults(func=cmd_parse)

    s = sub.add_parser("diff", help="compare the doc with the last sync")
    s.add_argument("file", nargs="?", help="doc text (required for Google Docs)")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("accept", help="apply the pending diff")
    s.add_argument("--match", action="append", metavar="KEY=ID", help="confirm or force a match")
    s.add_argument("--new", action="append", metavar="KEY", help="treat a doc item as new")
    s.add_argument("--split", action="append", metavar="ID=KEY,KEY", help="an item was split")
    s.add_argument("--merge", action="append", metavar="ID,ID=KEY", help="items were merged")
    s.add_argument("--accept-proposed", action="store_true", help="accept every uncertain match")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("rank", help="ranked work candidates")
    s.add_argument("--queued", action="store_true", help="only [next] items, in doc order (auto mode)")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_rank)

    s = sub.add_parser("items", help="list tracked items")
    s.add_argument("--state", action="append", choices=STATES)
    s.set_defaults(func=cmd_items)

    s = sub.add_parser("show", help="full record for one item")
    s.add_argument("id")
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
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("start", help="check out the item's branch and mark it in-progress")
    s.add_argument("id")
    s.add_argument("--from", dest="from_branch", help="branch to fork from (default: base)")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("commit", help="commit staged changes with the item trailer")
    s.add_argument("id")
    s.add_argument("-m", "--message", action="append", required=True)
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("finish", help="mark built item awaiting-review and return to base")
    s.add_argument("id")
    s.add_argument("--tests-passed", metavar="SUMMARY", help="what ran and passed")
    s.add_argument("--note")
    s.add_argument("--stay", action="store_true", help="stay on the item branch")
    s.set_defaults(func=cmd_finish)

    s = sub.add_parser("checkout", help="switch branch keeping .ddd/ state")
    s.add_argument("branch")
    s.set_defaults(func=cmd_checkout)

    s = sub.add_parser("sync-git", help="mark merged items done")
    s.set_defaults(func=cmd_sync_git)

    s = sub.add_parser("status-block", help="render (or write) the agent-owned status section")
    s.add_argument("--write", metavar="PATH", help="local Markdown doc to update in place")
    s.add_argument("--expect-hash", help="doc hash the write is based on (default: last sync)")
    s.set_defaults(func=cmd_status_block)

    s = sub.add_parser("commit-state", help="commit .ddd/ on the base branch")
    s.add_argument("-m", "--message")
    s.set_defaults(func=cmd_commit_state)

    s = sub.add_parser("config", help="get or set config values")
    s.add_argument("action", choices=("get", "set"))
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("log", help="audit log")
    s.add_argument("--item")
    s.add_argument("--limit", type=int, default=50)
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
