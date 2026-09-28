# Kārya

**A day planner that fills itself.** Work you do with Claude Code or Codex is
recorded as it happens: the task, every decision along the way, and the
outcome when it's done. Asks from Slack, Gmail and Linear can be pulled in
three times a day. Everything lives on your Mac, in one SQLite file, behind a
board at <http://127.0.0.1:8788>.

*Kārya* (कार्य) is Sanskrit for work, the thing to be done.

- **It keeps itself current.** Claude Code and Codex get a small MCP server
  and a few lines of instructions: when you ask for real work they start a
  task, log progress and blockers, and close it with the outcome. You don't
  type anything into a tracker.
- **It answers "did we do that?"** Search covers every title, outcome, note
  and log entry, with dates. Click a result and the board slides to the day it
  was done and lights up the card.
- **One task per piece of work.** The same work turning up as a Linear issue,
  a Slack thread and a Claude session stays one task.
- **Days, not lists.** A column per day, a Now slot for what's in progress,
  unfinished work rolling forward, and past days folding into a log strip.
- **Private by design.** No account, no server, no telemetry. Your tasks never
  leave the machine except through connectors you have already signed in.

## Install

On a Mac with [Claude Code](https://claude.com/claude-code) and/or
[Codex](https://github.com/openai/codex), one line:

```sh
curl -fsSL https://raw.githubusercontent.com/Ketan3102/karya/main/install.sh | bash
```

It downloads Kārya into `~/karya` and walks you through the rest, asking
before each step that touches something of yours:

| Step | What it does |
|---|---|
| About you | Your name, email and time zone, saved to `~/karya/config.json` (readable only by you) |
| The board | A launchd job serving <http://127.0.0.1:8788>, started at every login |
| Claude Code | `claude mcp add --scope user karya …`, so every session in every repo can use it |
| Codex | A `[mcp_servers.karya]` entry in `~/.codex/config.toml` |
| Instructions | A "Task tracking (Kārya)" section in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, so both track work without being asked |
| Sync *(optional)* | Pull asks from Slack, Gmail and Linear at 09:00, 15:00 and 21:00 |
| Recap *(optional)* | A Friday email to yourself: the week's work by project, with outcomes |
| Slack bot *(optional)* | Morning plan and reminders on your phone ([SLACK_SETUP.md](SLACK_SETUP.md)) |
| History *(optional)* | Import your past GitHub PRs, each on the day it merged |

Then open a new Claude Code or Codex session and ask for some work. It shows
up on the board.

**Needs:** macOS, Python 3.9 or newer (the one that comes with the Xcode
command line tools is fine: `xcode-select --install`), git. Nothing to `pip
install`: Kārya uses only the standard library. The optional sync needs the
Slack, Gmail and Linear connectors on your claude.ai account; the history
import needs the `gh` CLI, logged in.

Prefer to look first? `git clone https://github.com/Ketan3102/karya.git ~/karya && ~/karya/install.sh`.
Keep it outside `~/Desktop`, `~/Documents` and `~/Downloads`: macOS blocks
background jobs there.

## Everyday use

| You want to | Do |
|---|---|
| Add something | Type in a day's "Add task" row. `!high`, `!!` (urgent), `#Project`, `due:fri`, `@personal` are understood, and so are times: `Call Sam at 4:30 pm`. |
| Plan it for another day | Drag the card to that day, or to the Inbox. |
| Finish it | Tick the circle. Open the card to write an outcome. |
| Find out whether something was done, and when | **Search everything** (or `/`): a ticket or PR number, a name, a few words. Each result says **Done Thu 24 Sep** / In progress / Waiting on … with the outcome and the line that matched. Click it, or `↑` `↓` `Enter`: the board slides to that day and outlines the card; click the card for the details. |
| See what you did | Scroll left for past days, the **Done** tab, or **Review** (work only, as Markdown). |
| See only the urgent / overdue ones | Click a count in the header. `Esc` clears it. |
| Keep something personal | `@personal` when typing it, or flip **Space** on the card. Personal tasks stay out of Review and the recap. |
| Keyboard | `n` new task today · `t` today · `/` search · `Esc` close |

Or just ask Claude: "what's due this week?", "did we ever fix the login
redirect?". It uses the same tools.

## How it fits together

```
Claude Code / Codex ──(karya MCP, stdio)──┐
                                          ├──> data/karya.sqlite3 <── server.py ── the board
launchd 09:00 15:00 21:00 ── sync.py ─────┘         │
   headless `claude -p`, read-only Slack/Gmail/     └── notify.py ── Slack + macOS banner
   Linear connectors, returns typed JSON
```

| File | Role |
|---|---|
| `store.py` | The only code that writes. Three dates per task: `due_date` (someone's deadline), `planned_date` (the day you'll do it; empty means Inbox), `completed_at` (when it was done). Unfinished work rolls over to today; done work stays on its day, so the board is a history. |
| `mcp_server.py` | 10 tools: `karya_start_task`, `karya_add_task`, `karya_log`, `karya_complete_task`, `karya_update_task`, `karya_merge_tasks`, `karya_list`, `karya_search`, `karya_digest`, `karya_ingest`. A session that starts work records the repo and branch it runs in. |
| `search.py` | The search behind the board and `karya_search` (below). |
| `sync.py` | Builds a prompt with the look-back window and your open tasks, runs `claude -p` with only read tools allowed, validates the JSON and applies it. It never reopens a task you closed. |
| `notify.py`, `alarms.py`, `recap.py` | The morning plan, new-ask pings, timed reminders and the Friday recap. |
| `backfill_git.py` | Imports past PRs, unshipped branches and commits, each on the day it happened. |
| `server.py`, `web/` | The board: stdlib HTTP server, plain HTML/CSS/JS, bound to 127.0.0.1 only. |

## Search

Everything recorded about a task is searched: the title, the outcome, the
notes, every log entry with its date, and the references (Linear IDs,
project, requester, who it's waiting on). Bookkeeping like "rolled over" is
left out, so searching "today" doesn't match every task.

It is keyword search (SQLite FTS5, BM25 ranking), not embeddings, because
task text is full of identifiers: ticket and PR numbers, error codes,
versions, names. Keyword search matches those exactly; an embedding model
treats `v2.3.9` and `v2.3.1` as the same. The usual gaps are covered:

| You type | It still finds |
|---|---|
| `did we fix the login redirect?` | login + redirect (question words are ignored) |
| `fixing`, `charts` | fixed, fix, chart (stemming) |
| `tagg` (still typing) | tagging, tagged |
| `recieve` | receive, and says so |
| `ENG 2430` (dictated) | ENG-2430, with the task tracking that issue first |
| `pr 7040` | pull request #7040 |
| words no single task has all of | the tasks with most of them, under "Has some of the words" |
| `"exact phrase"` | only that phrase |

Add your own domain's synonyms and the client names that shouldn't make two
tasks look alike in `config.json` (below). For fuzzier questions, ask Claude:
it searches with the terms your question implies.

## Settings: config.json

Written by the installer; every key is optional. It is gitignored and
readable only by you.

| Key | Default | What |
|---|---|---|
| `name`, `email` | asked at install | Who the tracker is for; the email is used by the Gmail sync and the recap |
| `timezone` | the Mac's | IANA name, e.g. `Europe/Berlin` |
| `port` | `8788` | The board's port |
| `slack_user_id` | | Your Slack member ID, for the Slack part of the sync |
| `linear_user` | `me` | Whose Linear issues the sync reads |
| `sources` | `["slack", "gmail", "linear"]` | What the sync reads |
| `sync_model`, `notify_model` | `sonnet`, `haiku` | Models for the headless runs |
| `morning_time`, `evening_hour` | `08:00`, `18` | When the plan arrives, and from when a sync schedules tomorrow's |
| `recap_to` | your `email` | Where the Friday recap goes |
| `search_synonyms` | | Extra synonym groups, e.g. `[["auth", "authentication"]]` |
| `search_synonyms_one_way` | | e.g. `{"ultrasound": ["us"]}` |
| `similarity_ignore` | | Words most of your tasks share (client names) that shouldn't make two tasks look alike |
| `examples` | | Example title / project / ticket shown to the agents, in your own vocabulary |
| `backfill` | | Saved `backfill_git.py` settings (`--save` writes them) |
| `slack_bot_token` … | | Written by `setup_slack.py` |

## Sync, reminders and the recap

The sync is a headless `claude -p` run on your own Claude login, allowed only
search and read tools for Slack, Gmail and Linear; it returns JSON and
`sync.py` does every write itself, so a confused model can't delete anything.
It uses your Claude plan's usage (about a minute a run on a quiet day; set
`"sync_model": "haiku"` to use less). Every message it reads is treated as
data, never as instructions.

| When | What | Needs |
|---|---|---|
| 21:00 sync | Tomorrow's plan, scheduled in Slack for **08:00** (Slack delivers it with the Mac asleep) | Slack bot |
| 09:00 / 15:00 syncs | A ping only if new asks arrived | Slack bot (else a macOS banner) |
| 5 minutes before a task's time | "⏰ … needs to be done by 7:00 pm" | Slack bot |
| Friday 18:30 | The week's work by project with outcomes, next week's deadlines, emailed to you | Gmail connector |

Without the bot, a sync shows a macOS banner when something is new, urgent,
or it's the first of the day, and timed reminders are off. Laptop asleep at a sync time? launchd runs it on wake;
shut down? `catchup.py` runs at the next login.

## History from git

```sh
python3 backfill_git.py --github you/app --github you/api --dry-run
python3 backfill_git.py --github you/app --github you/api --commits ~/code/side-project --save
```

Every PR you opened becomes a task on the day it merged (closed ones are
dropped, open ones wait in the Inbox), with the PR description as its notes,
so search reaches everything it says. Release PRs go under Releases. Branches
that never became a PR (`--branches ~/code/app`) are kept as dropped, with
their commits. PRs from after Kārya started that a task already mentions are
linked to that task instead of recorded twice. Re-running never duplicates.

## Update and uninstall

```sh
~/karya/install.sh update       # git pull, reload the jobs and the MCP entries
~/karya/install.sh uninstall    # remove jobs, MCP entries and instructions; your data stays in ~/karya/data
python3 -m unittest discover -s tests    # from ~/karya
```

## Design

Ink and paper, plus three signals. The neutrals carry the whole interface.
Colour appears only when a task has earned it: **brick** means it needs you
now (overdue, urgent), **ochre** a deadline today, **teal** that you're on it.
**Sage** marks done and fades back. Every text colour clears WCAG AA in both
themes, and nothing relies on colour alone. Day names use the system serif,
like a paper diary. The ◐ button switches between system, light and dark.

## License

[MIT](LICENSE). Made for one person's working day; issues and pull requests
are welcome.
