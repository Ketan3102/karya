"""Search over everything Kārya knows about a task, so "did we do X, and
when?" is answered from the record instead of from memory.

Keyword search (SQLite FTS5, BM25 ranking), not embeddings, on purpose:

* Task text is dense with exact identifiers (ticket and PR numbers, Linear
  IDs, error codes, version numbers, names). Keyword search matches those
  exactly; an embedding model treats v2.3.9 and v2.3.1 as the same thing.
* It needs nothing new. The board is stdlib Python under launchd, and an
  embedding model means a torch install or sending work notes to an API.
* It is instant and explainable: every hit shows the words that matched.

The semantic gap is covered by porter stemming (fixed/fixing/fix), prefix
matching while typing, a short list of synonyms (PR = pull request, plus
your own domain's in config.json),
typo correction against the words actually in the store, and ranking that
lets partial matches through when no task has every word. Beyond that,
Claude is the semantic layer: it calls karya_search with the codes and
names a question implies.

What is searched: the title, the outcome, the notes, every log entry (with
its date), and the references (Linear IDs, Slack threads, project,
requester, who it is waiting on). Mechanical history ("rolled over",
"rescheduled", "Work started") is left out: it says nothing about the work
and would make every task match "today".

Ranking, best first: a task that is the reference searched for (ENG-2430);
then tasks with every word, preferring those with the words together in
one title, outcome or log entry over words scattered across a long history;
then, for several-word queries, tasks with most of the words (weighted by
how rare each word is), flagged partial.

The index lives in memory, per process, and is rebuilt when the store
changes (PRAGMA data_version sees other processes' commits, total_changes
sees this connection's own). Nothing is written to the task database.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from store import Store

HL_OPEN, HL_CLOSE = "\x02", "\x03"  # highlight markers; the board turns them into <mark>

# docs columns after task_id, and their BM25 weights: a word in the title or a
# reference says far more about which task it is than the same word in a log.
FIELDS = ("title", "outcome", "notes", "log", "refs")
WEIGHTS = {"title": 10.0, "outcome": 5.0, "notes": 3.0, "log": 2.0, "refs": 6.0}
TEXT_FIELDS = ("outcome", "notes", "log")  # what a hit line prefers to show

LABEL = {"title": "Title", "outcome": "Outcome", "notes": "Notes", "refs": "Reference", "project": "Project",
         "requester": "Asked by", "waiting_on": "Waiting on",
         "progress": "Progress", "note": "Note", "linked": "Linked", "updated": "Update", "merged": "Merged",
         "dropped": "Dropped", "started": "Started", "created": "Added", "completed": "Completed",
         "reopened": "Reopened"}
SOURCE_NAME = {"slack": "Slack", "gmail": "Gmail", "linear": "Linear", "calendar": "Calendar", "claude": "Claude",
               "codex": "Codex", "manual": "Board"}

# Question scaffolding: "did we fix the login redirect yet?" searches for fix, login, redirect.
STOP = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "by", "for", "with", "from", "into",
    "about", "as", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "done", "doing",
    "have", "has", "had", "it", "its", "this", "that", "these", "those", "we", "i", "me", "my", "our", "us",
    "you", "your", "he", "she", "they", "them", "their", "what", "when", "where", "which", "who", "how",
    "why", "any", "anything", "something", "thing", "things", "task", "tasks", "ever", "yet", "already",
    "still", "if", "there", "then", "than", "so", "can", "could", "would", "should", "will", "please",
    "status", "whether", "one", "some",
}

# Same thing, different spelling. Kept short: an entry must never pull in
# unrelated work. Terms are token sequences as the index sees them ("E/M" is
# the two tokens "e m"). Add your own domain's in config.json:
#   "search_synonyms": [["auth", "authentication"], ["js", "javascript"]]
#   "search_synonyms_one_way": {"ultrasound": ["us"]}   # one way: "us" typed is usually a pronoun
# Mind that the index stems: "smoking" also matches "smoke test".
SYNONYM_GROUPS = [
    ["pr", "pull request"],
    ["mr", "merge request"],
    ["db", "database"],
    ["k8s", "kubernetes"],
]


def synonym_table(groups: list[list[str]] | None = None, one_way: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """term -> alternatives, from the built-in groups plus config.json's."""
    if groups is None or one_way is None:
        from store import load_config
        config = load_config()
        groups = SYNONYM_GROUPS + list(config.get("search_synonyms") or []) if groups is None else groups
        one_way = dict(config.get("search_synonyms_one_way") or {}) if one_way is None else one_way
    table: dict[str, list[str]] = {}
    for group in groups:
        for term in group:
            table.setdefault(term, []).extend(alt for alt in group if alt != term)
    for term, alts in one_way.items():
        table.setdefault(term, []).extend(alts)
    return table

# Log entries that are pure bookkeeping.
SKIP_KINDS = {"rolled_over", "rescheduled"}
SCHEDULING = {"planned_date", "due_date", "due_time", "priority", "status", "space", "sort_order"}
BOILERPLATE = re.compile(
    r"(added (from|on) [\w ]+|picked up from \w+|work started|started in \w+( \(.*\))?|picked up again in \w+( \(.*\))?"
    r"|marked done|reopened|moved to (today|inbox)|rescheduled)", re.I)

PARTIAL_FLOOR = 0.45  # a partial match must carry this share of the query's weight
ISSUE_REF = re.compile(r"[A-Z][A-Z0-9]{1,5}-\d{1,6}")  # ENG-2430, OPS-436: shown on the hit


def fold(text: str) -> str:
    """Lower-case and strip diacritics, as the index does (Kārya -> karya)."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def edit_distance(a: str, b: str, cap: int) -> int:
    """Optimal string alignment distance (a swap counts as one edit), giving up past cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def _quote(toks: list[str]) -> str:
    return '"' + " ".join(toks) + '"'


class SearchIndex:
    def __init__(self, store: "Store", *, synonyms: dict[str, list[str]] | None = None):
        self.store = store
        self.synonyms = synonyms if synonyms is not None else synonym_table()
        self.mem = sqlite3.connect(":memory:", check_same_thread=False)
        self.mem.row_factory = sqlite3.Row
        self.version: tuple[int, int] | None = None
        self.tasks: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, list[str]] = {}          # task -> issue IDs shown on the hit
        self.ref_keys: dict[str, set[str]] = {}       # task -> every reference, as tokens, for "is this that task"
        self.context: dict[str, dict[str, Any]] = {}  # task -> the line that says where it stands
        self.unit_of: dict[int, tuple[str, str]] = {}  # unit rowid -> (task, field)
        self.hidden: set[str] = set()
        self.vocab: dict[str, int] = {}

    def close(self) -> None:
        self.mem.close()

    # ------------------------------------------------------------ indexing
    def _signature(self) -> tuple[int, int]:
        db = self.store.db
        return db.execute("PRAGMA data_version").fetchone()[0], db.total_changes

    def refresh(self) -> None:
        signature = self._signature()
        if signature != self.version:
            self._build()
            self.version = signature

    def _units(self, task: dict[str, Any], events: list[sqlite3.Row],
               refs: list[tuple[str, str]]) -> list[tuple[str, str, str | None, str]]:
        """(field, kind, at, text) for every piece of text worth searching in a task."""
        from store import Store  # store imports this module lazily; keep the cycle one-way

        units: list[tuple[str, str, str | None, str]] = [("title", "title", None, task["title"])]
        if task.get("completion_note"):
            units.append(("outcome", "outcome", task["completed_at"], task["completion_note"].strip()))
        if (task.get("notes") or "").strip():
            units.append(("notes", "notes", None, task["notes"].strip()))
        for event in events:
            text = (event["text"] or "").strip()
            if not text or event["kind"] in SKIP_KINDS or BOILERPLATE.fullmatch(text):
                continue
            if event["kind"] == "completed" and text == (task.get("completion_note") or "").strip():
                continue  # already there as the outcome
            if event["kind"] == "updated":
                changes = (json.loads(event["data"] or "{}") or {}).get("changes") or {}
                if set(changes) <= SCHEDULING and text == Store._describe(changes):
                    continue  # "planned date → 2026-09-28"
            units.append(("log", event["kind"], event["at"], text))
        for key in ("project", "requester", "waiting_on"):
            if task.get(key):
                units.append(("refs", key, None, task[key]))
        named = [f"{SOURCE_NAME.get(src, src)} {key}" for src, key in refs]
        units.append(("refs", "refs", None, " · ".join([*named, f"from {SOURCE_NAME.get(task['source'], task['source'])}",
                                                          f"id {task['id']}"])))
        return units

    def _build(self) -> None:
        db = self.store.db
        tasks = {r["id"]: self.store._row(r) for r in db.execute("SELECT * FROM tasks")}
        refs: dict[str, list[tuple[str, str]]] = {}
        for r in db.execute("SELECT source, ref_key, task_id FROM task_refs"):
            refs.setdefault(r["task_id"], []).append((r["source"], r["ref_key"]))
        events: dict[str, list[sqlite3.Row]] = {}
        for r in db.execute("SELECT * FROM events WHERE task_id IS NOT NULL ORDER BY id"):
            events.setdefault(r["task_id"], []).append(r)

        # A duplicate merged into another task: its history moved to the kept
        # task, so showing it would only repeat that task as "dropped".
        self.hidden = {tid for tid, evs in events.items() if tasks.get(tid, {}).get("status") == "dropped"
                       and any(e["kind"] == "dropped" and e["text"].startswith("Duplicate of") for e in evs)}

        mem = self.mem
        mem.executescript(
            """
            DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS units;
            CREATE VIRTUAL TABLE docs USING fts5(task_id UNINDEXED, title, outcome, notes, log, refs,
                tokenize = 'porter unicode61 remove_diacritics 2');
            CREATE VIRTUAL TABLE units USING fts5(task_id UNINDEXED, field UNINDEXED, kind UNINDEXED, at UNINDEXED, text,
                tokenize = 'porter unicode61 remove_diacritics 2');
            """)
        vocab: dict[str, int] = {}
        self.refs, self.ref_keys, self.context = {}, {}, {}
        for tid, task in tasks.items():
            units = self._units(task, events.get(tid, []), refs.get(tid, []))
            by_field: dict[str, list[str]] = {f: [] for f in FIELDS}
            for field, _kind, _at, text in units:
                by_field[field].append(text)
            mem.execute("INSERT INTO docs(task_id, title, outcome, notes, log, refs) VALUES (?,?,?,?,?,?)",
                        (tid, *("\n".join(by_field[f]) for f in FIELDS)))
            mem.executemany("INSERT INTO units(task_id, field, kind, at, text) VALUES (?,?,?,?,?)",
                            [(tid, field, kind, at, text) for field, kind, at, text in units])
            for _field, _kind, _at, text in units:
                for word in tokens(text):
                    if len(word) >= 3 and not word.isdigit():
                        vocab[word] = vocab.get(word, 0) + 1
            keys = [key for _src, key in refs.get(tid, [])] + ([task["source_ref"]] if task.get("source_ref") else [])
            self.refs[tid] = sorted({k.upper() for k in keys if ISSUE_REF.fullmatch(k.upper())})
            self.ref_keys[tid] = {" ".join(tokens(k)) for k in [*keys, tid]} - {""}
            self.context[tid] = self._context(task, units)
        mem.commit()
        self.unit_of = {r[0]: (r[1], r[2]) for r in mem.execute("SELECT rowid, task_id, field FROM units")}
        self.tasks, self.vocab = tasks, vocab

    @staticmethod
    def _context(task: dict[str, Any], units: list[tuple[str, str, str | None, str]]) -> dict[str, Any] | None:
        """Where the task stands, in one line: the outcome if it is finished,
        why it was dropped, or the latest log entry (else the notes) if open."""
        from store import local_date_of

        def line(kind: str, at: str | None, text: str) -> dict[str, Any]:
            return {"label": LABEL.get(kind, kind.title()), "date": local_date_of(at), "text": text}

        logs = [u for u in units if u[0] == "log"]
        if task["status"] == "done" and task.get("completion_note"):
            return line("outcome", task["completed_at"], task["completion_note"])
        if task["status"] == "dropped":
            dropped = [u for u in logs if u[1] == "dropped"]
            if dropped:
                return line(*dropped[-1][1:])
        if logs:
            return line(*logs[-1][1:])
        if (task.get("notes") or "").strip():
            return line("notes", None, task["notes"].strip())
        return None

    # --------------------------------------------------------------- query
    def _terms(self, query: str) -> list[tuple[str, list[str], bool]]:
        """(as typed, tokens, is_phrase) per query word; quoted text is one phrase."""
        terms = []
        for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query):
            toks = tokens(phrase or word)
            if toks:
                terms.append((phrase or word, toks, bool(phrase)))
        content = [t for t in terms if t[2] or " ".join(t[1]) not in STOP]
        return content or terms  # a query of only stop words still searches for them

    def _expr(self, toks: list[str], *, prefix: bool) -> str:
        exact = _quote(toks)
        # Exact OR prefix: an exact hit counts twice, so it outranks a mere prefix.
        parts = [exact, f"{exact}*"] if prefix and len(toks[-1]) >= 2 else [exact]
        if prefix and len(toks) == 1 and len(toks[0]) >= 3 and not toks[0].isdigit():
            # The index holds stems, so a half-typed "tagg" never prefixes "tag" (tagging).
            # Complete it from the words in the store instead; each is stemmed as it is matched.
            done = sorted((w for w in self.vocab if w.startswith(toks[0]) and w != toks[0]), key=lambda w: -self.vocab[w])
            parts += [_quote([w]) for w in done[:12]]
        parts += [_quote(alt.split()) for alt in self.synonyms.get(" ".join(toks), [])]
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def _match(self, expr: str) -> set[str]:
        return {r[0] for r in self.mem.execute("SELECT task_id FROM docs WHERE docs MATCH ?", (expr,))}

    def _correct(self, word: str) -> str | None:
        """The closest word in the store for a word that matches nothing, if it is close."""
        if len(word) < 4 or any(ch.isdigit() for ch in word):
            return None
        cap = 1 if len(word) <= 5 else 2
        best: tuple[int, int, str] | None = None
        for candidate, freq in self.vocab.items():
            if abs(len(candidate) - len(word)) > cap:
                continue
            d = edit_distance(word, candidate, cap)
            if d <= cap and (best is None or (d, -freq) < best[:2]):
                best = (d, -freq, candidate)
        return best[2] if best else None

    def search(self, query: str, *, status: str = "all", limit: int = 30) -> dict[str, Any]:
        """Tasks matching the query, best first (see the module docstring).
        status is all, open or done; done includes dropped."""
        if status not in ("all", "open", "done"):
            raise ValueError("status must be all, open or done")
        self.refresh()
        result = {"query": query, "tasks": [], "total": 0, "counts": {"all": 0, "open": 0, "done": 0},
                  "corrected": {}, "missing": []}
        terms = self._terms(query or "")
        if not terms:
            return result
        typing = not query.endswith((" ", '"'))
        groups: list[tuple[str, set[str]]] = []
        for index, (typed, toks, is_phrase) in enumerate(terms):
            expr = self._expr(toks, prefix=typing and index == len(terms) - 1 and not is_phrase)
            hits = self._match(expr)
            if not hits and len(toks) == 1:
                fix = self._correct(toks[0])
                if fix:
                    result["corrected"][typed] = fix
                    expr = self._expr([fix], prefix=False)
                    hits = self._match(expr)
            if hits:
                groups.append((expr, hits))
            else:
                result["missing"].append(typed)  # in no task at all: say so, and rank on the other words
        union = set().union(*(h for _, h in groups)) - self.hidden
        if not union:
            return result

        n = max(1, len(self.tasks))
        idf = [math.log(1 + (n - len(h) + 0.5) / (len(h) + 0.5)) for _, h in groups]
        any_expr = " OR ".join(expr for expr, _ in groups)
        bm25 = {r[0]: r[1] for r in self.mem.execute(
            f"SELECT task_id, bm25(docs, 0, {', '.join(str(WEIGHTS[f]) for f in FIELDS)}) FROM docs WHERE docs MATCH ?",
            (any_expr,))}
        # How many of the words sit together in one piece of text, per task, and in which field.
        cover: dict[int, int] = {}
        for expr, _ in groups:
            for (rowid,) in self.mem.execute("SELECT rowid FROM units WHERE units MATCH ?", (expr,)):
                cover[rowid] = cover.get(rowid, 0) + 1
        best_unit: dict[str, tuple[int, float]] = {}
        for rowid, count in cover.items():
            tid, field = self.unit_of[rowid]
            best_unit[tid] = max(best_unit.get(tid, (0, 0.0)), (count, WEIGHTS[field]))
        # Is the query a reference this task carries? "ENG 2430" dictated is ENG-2430 typed.
        flat = [tok for _typed, toks, _p in terms for tok in toks]
        wanted = {" ".join(toks) for _typed, toks, _p in terms} | {" ".join(flat)} | \
                 {" ".join(flat[i:i + 2]) for i in range(len(flat) - 1)}

        scored = []
        for tid in union:
            share = sum(w for w, (_, hits) in zip(idf, groups) if tid in hits) / (sum(idf) or 1.0)
            full = all(tid in hits for _, hits in groups)
            if not full and (len(groups) < 2 or share < PARTIAL_FLOOR):
                continue
            together, field_weight = best_unit.get(tid, (0, 0.0))
            is_ref = bool(wanted & self.ref_keys.get(tid, set()))
            key = (0, -is_ref, -together, -field_weight, bm25.get(tid, 0.0)) if full else \
                  (1, -round(share, 3), -together, -field_weight, bm25.get(tid, 0.0))
            scored.append((key, tid, full and not result["missing"]))
        scored.sort()

        counts = result["counts"]
        counts["all"] = len(scored)
        for _key, tid, _full in scored:
            counts["open" if self.tasks[tid]["open"] else "done"] += 1
        picked = [(tid, full) for _key, tid, full in scored
                  if status == "all" or ("open" if self.tasks[tid]["open"] else "done") == status][:limit]
        pieces = self._pieces([tid for tid, _ in picked], any_expr, cover)
        result["tasks"] = [self._result(tid, full, pieces.get(tid, []), len(groups)) for tid, full in picked]
        result["total"] = counts[status]
        return result

    def _pieces(self, ids: list[str], expr: str, cover: dict[int, int]) -> dict[str, list[dict[str, Any]]]:
        """Every matching piece of text in these tasks, highlighted, with how many words it holds."""
        if not ids:
            return {}
        found: dict[str, list[dict[str, Any]]] = {}
        rows = self.mem.execute(
            f"SELECT rowid, task_id, field, kind, at, highlight(units, 4, ?, ?) AS hl, "
            f"snippet(units, 4, ?, ?, '…', 28) AS snip, bm25(units) AS score FROM units "
            f"WHERE units MATCH ? AND task_id IN ({','.join('?' * len(ids))}) ORDER BY score",
            (HL_OPEN, HL_CLOSE, HL_OPEN, HL_CLOSE, expr, *ids))
        for r in rows:
            found.setdefault(r["task_id"], []).append({**dict(r), "cover": cover.get(r["rowid"], 0)})
        return found

    def _result(self, tid: str, full: bool, pieces: list[dict[str, Any]], n_groups: int) -> dict[str, Any]:
        from store import local_date_of

        task = self.tasks[tid]
        title = next((p for p in pieces if p["field"] == "title"), None)
        others = sorted((p for p in pieces if p["field"] != "title"),
                        key=lambda p: (-p["cover"], p["field"] not in TEXT_FIELDS, p["score"]))
        best = others[0] if others else None
        # When the title already holds every word, the useful line is where the task stands, not another mention.
        if best and title and title["cover"] >= n_groups and best["cover"] < title["cover"]:
            best = None
        hit = None
        if best:
            hit = {"field": best["field"], "label": LABEL.get(best["kind"], best["kind"].title()),
                   "date": local_date_of(best["at"]), "text": best["snip"]}
        keep = ("id", "title", "status", "open", "space", "priority", "project", "source", "source_ref", "source_url",
                "requester", "waiting_on", "planned_date", "due_date", "due_time", "done_date", "completion_note",
                "created_at", "started_at", "completed_at", "updated_at")
        return {**{k: task.get(k) for k in keep}, "title_hl": title["hl"] if title else None,
                "refs": self.refs.get(tid, []), "partial": not full, "hit": hit, "context": self.context.get(tid)}
