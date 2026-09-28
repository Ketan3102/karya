#!/usr/bin/env python3
"""Backfill Kārya with the work you did before it was recording, from git.

    python3 backfill_git.py --github you/app --github you/api --dry-run
    python3 backfill_git.py --github you/app --commits ~/code/dotfiles=Home --save
    python3 backfill_git.py              # re-run with what --save stored

What becomes a task, with its real dates (so it sits on the day it happened):

* Every PR you opened in the --github repos (needs the gh CLI, logged in).
  Merged -> done on the merge date; closed unmerged -> dropped; still open ->
  waiting, in the Inbox. The PR description becomes the notes, so everything
  it says is searchable, and the history says which branch. PRs into a
  release branch (main when the default branch is dev) go under Releases:
  their descriptions list what they carried, which answers "is #6283 live?".
* Branches in the --branches clones with your commits that never became a
  PR (experiments, abandoned approaches): dropped, with the commit list, so
  "where did I try that?" has an answer.
* Commits in --commits repos, which have no PRs (a solo project): one task
  each, under the project given after "=" (default: the folder name).

What is linked instead of recorded: a PR or commit from after Kārya started
that an existing task already mentions (#7009, 5d764ff). That task gains the
reference and a history line; no second task is made for the same work.

Safe to re-run: every item has a stable reference (github app#7009, git
dotfiles@5d764ff) and existing ones are skipped. The first real run stores
its cutoff; later runs never import anything newer, because from then on the
work is tracked live. Settings can live in config.json under "backfill"
(--save writes them): github_repos, branch_repos, commit_repos, author,
skip_branch_prefixes, path_projects / title_projects (a folder or title word
-> project name) and repo_projects.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from store import ROOT, Store, content_tokens, load_config, local_date_of, today

RELEASE_BASES = {"main", "master", "production", "prod", "release", "stable"}
SETTINGS: dict = {}  # the "backfill" section, merged with the command line in main()

PREFIX = re.compile(r"^(feat|fix|chore|refactor|docs|revert|perf|test|build|ci|style|release|tools|checkpoint)"
                    r"(\([^)]*\))?!?:\s*", re.I)
PR_MENTION = re.compile(r"(?:#|\bPRs?\s*#?)(\d{4,5})\b")
HASH_MENTION = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}\b")


def run(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


def utc(stamp: str) -> str:
    """Any ISO timestamp -> the store's UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def day(stamp: str) -> str:
    from store import local_date_of
    return datetime.fromisoformat(local_date_of(stamp)).strftime("%-d %b %Y")  # type: ignore[arg-type]


def clean_title(title: str) -> str:
    title = PREFIX.sub("", " ".join(title.split()))
    return title[:1].upper() + title[1:]


def project_for(paths: list[str], title: str, repo: str) -> str:
    """The project a change belongs to: the folder most of its files sit in
    (path_projects: {"billing": "Billing"}), else a word in the title
    (title_projects), else the repo (repo_projects, default its name)."""
    by_path = {k.lower(): v for k, v in (SETTINGS.get("path_projects") or {}).items()}
    by_title = {k.lower(): v for k, v in (SETTINGS.get("title_projects") or {}).items()}
    votes: Counter[str] = Counter()
    for path in paths:
        for segment in path.lower().split("/"):
            hit = next((project for key, project in by_path.items() if key in segment), None)
            if hit:
                votes[hit] += 1
                break
    if votes:
        return votes.most_common(1)[0][0]
    low = title.lower()
    for word, project in by_title.items():
        if word in low:
            return project
    short = repo.split("/")[-1]
    return (SETTINGS.get("repo_projects") or {}).get(short, short)


def summary(body: str, limit: int = 320) -> str:
    """The first prose paragraph of a PR description, as plain text."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para or para.startswith(("#", "|", "```", ">", "- ", "* ", "🤖")) or re.match(r"\d+\. ", para):
            continue
        plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para)
        plain = " ".join(re.sub(r"[*_`]", "", plain).split())
        if len(plain) <= limit:
            return plain
        cut = plain[:limit]
        stop = max(cut.rfind(". "), cut.rfind("; "))
        return (cut[:stop + 1] if stop > limit // 3 else cut.rstrip() + "…")
    return ""


def notes_from(body: str) -> str:
    lines = [line for line in (body or "").splitlines() if not line.startswith("🤖 Generated with")]
    return "\n".join(lines).strip()[:8000]


class Mentions:
    """Which existing (live-tracked) tasks mention a PR number or a commit hash."""

    def __init__(self, store: Store):
        self.prs: dict[str, Counter[str]] = {}
        self.named: dict[str, set[str]] = {}  # PR number -> tasks whose title or outcome names it
        self.hashes: dict[str, set[str]] = {}
        self.tasks: dict[str, dict] = {}
        for task in store.list(statuses=("todo", "doing", "waiting", "done", "dropped"), limit=100000):
            if task["source"] in ("github", "git"):
                continue
            self.tasks[task["id"]] = task
            fields = [(task["title"], 3), (task.get("completion_note") or "", 3), (task.get("notes") or "", 1)]
            fields += [(e["text"], 1) for e in store.events(task["id"], limit=1000)]
            for text, weight in fields:
                for number in PR_MENTION.findall(text or ""):
                    self.prs.setdefault(number, Counter())[task["id"]] += weight
                    if weight == 3:
                        self.named.setdefault(number, set()).add(task["id"])
                for h in HASH_MENTION.findall(text or ""):
                    self.hashes.setdefault(h, set()).add(task["id"])

    def for_pr(self, number: int, project: str, title: str) -> dict | None:
        """The task this PR belongs to. Several tasks often share one PR (#7009
        shipped three ENG-2430 items) and all of them keep mentioning it, so
        search finds each; the reference goes to the best fit: a task whose
        title or outcome names the PR, then the one whose title shares most
        words with the PR's (a PR mentioned in passing by a busy triage task
        still goes to the task that is its work), then the most mentions.
        A task filed under a different project never wins a tie."""
        found = self.prs.get(str(number))
        if not found:
            return None
        words, named = content_tokens(title), self.named.get(str(number), set())
        best = max(found, key=lambda tid: (tid in named, len(words & content_tokens(self.tasks[tid]["title"])),
                                           found[tid], self.tasks[tid]["project"] in (None, project),
                                           self.tasks[tid]["updated_at"]))
        return self.tasks[best]

    def for_commit(self, sha: str) -> dict | None:
        for h, ids in self.hashes.items():
            if sha.startswith(h):
                return self.tasks[sorted(ids)[0]]
        return None


def pr_items(repo: str) -> list[dict]:
    fields = "number,title,body,state,createdAt,mergedAt,closedAt,headRefName,baseRefName,url,files"
    prs = json.loads(run("gh", "pr", "list", "--repo", repo, "--author", "@me", "--state", "all",
                         "--limit", "1000", "--json", fields))
    default = run("gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").strip()
    short = repo.split("/")[1]
    items = []
    for pr in prs:
        n, base, head = pr["number"], pr["baseRefName"], pr["headRefName"]
        title = clean_title(pr["title"])
        project = project_for([f["path"] for f in pr["files"] or []], pr["title"], repo)
        if base != default and base in RELEASE_BASES:  # dev -> main: a release, not a change
            project = "Releases"
            if "release" not in title.lower():
                title = f"Release {title[:1].lower() + title[1:]}"
        created = utc(pr["createdAt"])
        history = [(created, "created", f"Opened PR #{n} in {short} from branch {head}")]
        what = summary(pr["body"])
        item = {"kind": "pr", "repo": short, "number": n, "title": title, "project": project, "head": head,
                "source": "github", "source_ref": f"{short}#{n}", "source_url": pr["url"], "created_at": created,
                "notes": notes_from(pr["body"]), "history": history}
        if pr["state"] == "MERGED":
            done = utc(pr["mergedAt"])
            history.append((done, "completed", f"Merged into {base}"))
            item.update(status="done", completed_at=done, when=done,
                        outcome=f"Merged into {base} on {day(done)} as PR #{n} ({short}, branch {head})."
                                + (f" {what}" if what else ""))
        elif pr["state"] == "CLOSED":
            closed = utc(pr["closedAt"])
            history.append((closed, "dropped", f"PR #{n} closed without merging"))
            item.update(status="dropped", when=closed,
                        outcome=f"PR #{n} ({short}) was closed without merging on {day(closed)}.")
        else:
            item.update(status="waiting", when=created, waiting_on=f"review: PR #{n} open since {day(created)}")
        items.append(item)
    # A release PR still open after later releases shipped is superseded, not waiting.
    released = max((i["completed_at"] for i in items if i["project"] == "Releases" and i["status"] == "done"), default="")
    for item in items:
        if item["project"] == "Releases" and item["status"] == "waiting" and item["created_at"] < released:
            item.update(status="dropped", waiting_on=None,
                        outcome=f"Release PR #{item['number']} is still open on GitHub, but later releases have "
                                f"shipped since (latest merged {day(released)}), so it is superseded.")
            item["history"].append((released, "dropped", "Superseded by later releases"))
    return items


def base_branch(path: Path) -> str:
    """origin's default branch (origin/dev, origin/main...), which a branch's own commits are counted against."""
    try:
        return run("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=path).strip()
    except subprocess.CalledProcessError:
        for guess in ("origin/main", "origin/master"):
            if subprocess.run(["git", "rev-parse", "--verify", "-q", guess], cwd=path, capture_output=True).returncode == 0:
                return guess
        raise


def branch_items(heads: set[str], started: str, author: str) -> list[dict]:
    """Branches with your commits that never became a PR, finished before Kārya started."""
    items, seen = [], set()
    skip = tuple(SETTINGS.get("skip_branch_prefixes") or ["release/"])
    for short, path in (SETTINGS.get("branch_repos") or {}).items():
        path = Path(os.path.expanduser(path))
        if not path.exists():
            continue
        base = base_branch(path)
        current = run("git", "branch", "--show-current", cwd=path).strip()
        refs = run("git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin", cwd=path).split()
        for ref in refs:
            name = ref.removeprefix("origin/")
            if (short, name) in seen or name in ("HEAD", "origin", current, base.removeprefix("origin/")) or \
                    name in RELEASE_BASES or name.startswith(skip) or any(h == name or h.startswith(name) for h in heads):
                continue
            log = run("git", "log", f"{base}..{ref}", f"--author={author}", "-i", "--no-merges",
                      "--format=%H%x1f%aI%x1f%s", cwd=path).strip().splitlines()
            if not log:
                continue
            commits = [line.split("\x1f") for line in log]  # newest first
            if commits[0][1][:10] >= started:
                continue  # recent: tracked live, or a local rebase of a PR
            seen.add((short, name))
            first, last = utc(commits[-1][1]), utc(commits[0][1])
            listing = "\n".join(f"- {c[1][:10]} {c[0][:10]} {c[2]}" for c in reversed(commits))
            items.append({
                "kind": "branch", "repo": short, "title": clean_title(commits[-1][2]),
                "project": project_for([], name + " " + " ".join(c[2] for c in commits), short),
                "source": "git", "source_ref": f"{short}:{name}", "status": "dropped", "created_at": first, "when": last,
                "notes": f"Branch {name} in {short}, {len(commits)} commit{'s' * (len(commits) > 1)}:\n{listing}",
                "outcome": f"{len(commits)} commit{'s' * (len(commits) > 1)} on branch {name} in {short} "
                           f"({day(first)} to {day(last)}); never opened as a PR.",
                "history": [(first, "created", f"First commit on branch {name} ({short})"),
                            (last, "dropped", f"Last commit on {name}; no PR was opened")]})
    return items


def commit_items(author: str) -> list[dict]:
    items = []
    for repo in SETTINGS.get("commit_repos") or []:
        path = Path(os.path.expanduser(repo["path"]))
        if not path.exists():
            continue
        short, project, url = path.name, repo.get("project") or path.name, repo.get("url")
        log = run("git", "log", "--all", f"--author={author}", "-i", "--no-merges",
                  "--format=%H%x1f%aI%x1f%s%x1f%b%x1e", cwd=path)
        for record in filter(None, (r.strip("\n") for r in log.split("\x1e"))):
            sha, stamp, subject, body = (record.split("\x1f") + [""])[:4]
            when = utc(stamp)
            body = "\n".join(line for line in body.splitlines() if not line.startswith("Co-Authored-By")).strip()
            items.append({
                "kind": "commit", "repo": short, "sha": sha, "title": clean_title(subject), "project": project,
                "source": "git", "source_ref": f"{short}@{sha[:10]}", "source_url": f"{url}{sha}" if url else None,
                "status": "done", "created_at": when, "completed_at": when, "when": when, "notes": body,
                "outcome": f"Committed to {short} as {sha[:7]} on {day(when)}.",
                "history": [(when, "completed", f"Commit {sha[:7]} in {short}")]})
    return items


def tracking_started(store: Store) -> str:
    """The day Kārya started recording: its first task that wasn't imported."""
    row = store.db.execute("SELECT min(created_at) FROM tasks WHERE source NOT IN ('github', 'git')").fetchone()
    return local_date_of(row[0]) if row and row[0] else today().isoformat()  # type: ignore[return-value]


def save_settings(settings: dict) -> None:
    path = Path(os.environ.get("KARYA_CONFIG", ROOT / "config.json"))
    config = json.loads(path.read_text()) if path.exists() else {}
    config["backfill"] = settings
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    global SETTINGS
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--github", action="append", default=[], metavar="OWNER/REPO", help="import your PRs in this repo (repeatable)")
    parser.add_argument("--branches", action="append", default=[], metavar="PATH", help="a local clone: import your branches that never became a PR")
    parser.add_argument("--commits", action="append", default=[], metavar="PATH[=Project]", help="a repo without PRs: one task per commit")
    parser.add_argument("--author", help="whose commits (default: your git user.email)")
    parser.add_argument("--save", action="store_true", help="remember these settings in config.json for later runs")
    parser.add_argument("--dry-run", action="store_true", help="show what would be recorded; write nothing")
    args = parser.parse_args()
    SETTINGS = dict(load_config().get("backfill") or {})
    if args.github:
        SETTINGS["github_repos"] = args.github
    if args.branches:
        SETTINGS["branch_repos"] = {Path(p).expanduser().name: p for p in args.branches}
    if args.commits:
        SETTINGS["commit_repos"] = [dict(zip(("path", "project"), c.split("=", 1))) for c in args.commits]
    if args.author:
        SETTINGS["author"] = args.author
    if not any(SETTINGS.get(k) for k in ("github_repos", "branch_repos", "commit_repos")):
        parser.error("nothing to import: pass --github, --branches or --commits (see --help)")
    if args.save:
        save_settings(SETTINGS)
        print("settings saved to config.json")
    author = SETTINGS.get("author") or subprocess.run(["git", "config", "--get", "user.email"],
                                                      capture_output=True, text=True).stdout.strip()
    store = Store()
    started = tracking_started(store)
    cutoff = store.kv_get("backfill_cutoff") or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    mentions = Mentions(store)

    prs = [item for repo in SETTINGS.get("github_repos") or [] for item in pr_items(repo)]
    heads = {item["head"] for item in prs}
    items = prs + branch_items(heads, started, author) + commit_items(author)

    tally: Counter[str] = Counter()
    for item in sorted(items, key=lambda i: i["when"]):
        if item["when"] > cutoff:
            tally["after cutoff (tracked live)"] += 1
            continue
        if store.by_source(item["source"], item["source_ref"]):
            tally["already recorded"] += 1
            continue
        recent = item["when"][:10] >= started or item["status"] == "waiting"
        linked = None
        if item["kind"] == "pr" and recent and item["project"] != "Releases":  # a release is its own record
            linked = mentions.for_pr(item["number"], item["project"], item["title"])
        elif item["kind"] == "commit":
            linked = mentions.for_commit(item["sha"])
        date = item["when"][:10]
        if linked:
            tally[f"linked {item['kind']}"] += 1
            print(f"link    {date} {item['source_ref']:<24} -> {linked['id']} {linked['title'][:60]}")
            if not args.dry_run:
                label = f"PR #{item['number']}" if item["kind"] == "pr" else f"commit {item['sha'][:7]}"
                store.add_ref(linked["id"], item["source"], item["source_ref"], actor="import",
                              note=f"{label} ({item['repo']}, {item['status']} {day(item['when'])}): {item['title']}")
            continue
        tally[f"new {item['kind']} ({item['status']})"] += 1
        print(f"new     {date} {item['source_ref']:<24} {item['status']:<8} [{item['project']}] {item['title'][:70]}")
        if not args.dry_run:
            store.import_record(item["title"], source=item["source"], source_ref=item["source_ref"],
                                status=item["status"], created_at=item["created_at"],
                                completed_at=item.get("completed_at"), completion_note=item.get("outcome"),
                                history=item["history"], project=item["project"], notes=item["notes"],
                                source_url=item.get("source_url"), waiting_on=item.get("waiting_on"))
    if not args.dry_run:
        store.kv_set("backfill_cutoff", cutoff)
    print("\n" + ", ".join(f"{n} {k}" for k, n in sorted(tally.items())) + (" (dry run: nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
