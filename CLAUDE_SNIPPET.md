# Task tracking (Kārya)

{name} tracks their work in Kārya (`{root}`, board at http://127.0.0.1:{port})
through the `karya` MCP server. Keep it current without being asked. The point
is a complete record of what they did.

- When a turn starts real work (build, fix, investigate, run, write, review),
  call `karya_start_task` before the first other tool call. One task per piece
  of work, not per turn: a follow-up on the same work reuses it (`task_id`).
  Pass `project` (the client, product or area) and `source_ref` when the ask
  is a Linear issue or a Slack thread.
- Most work is already tracked (a Linear issue, a Slack ask, an earlier
  session). If `karya_start_task` returns `decision_needed` with candidates,
  nothing was created: continue the one that is the same work by calling it
  again with its `task_id`, and only pass `new_task: true` if none is. Existing
  tasks get status updates, never a second task.
- One piece of work covering several tasks (one PR for three Linear issues):
  start the main one and `karya_log` the others.
- Blocked or waiting on someone: `karya_log` with status `waiting`.
- Asked whether something was done, when, or where ("did we fix X?", "is the
  login fix live?"): call `karya_search` before answering, with the specific
  ticket or PR numbers, Linear IDs, codes or names involved, and a second
  phrasing if the first finds nothing. Answer with the status, the date and
  the recorded outcome.
- When the work is finished, call `karya_complete_task` with a one or two
  sentence outcome: what changed, why it mattered, numbers if there are any.
  The outcome, not "done".
- A future to-do or deadline mentioned in passing ("remind me", "by Friday"):
  `karya_add_task` with `due_date`.
- When a clock time is stated ("order dinner before 7pm", "call Sam at 4:30"),
  also pass `due_time` as HH:MM, 24-hour. Kārya pings them 5 minutes before.
  Only when a time is actually said.
- Anything that isn't {name}'s job (money, health, family, errands, side
  projects, learning for themselves) gets `space: "personal"`. Personal tasks
  stay out of the work view and the weekly work summary. When unsure, it's work.
- Purely conversational turns (a question, an explanation, off-topic chat) are
  not tasks.
- If the karya tools are unavailable, say so once and carry on.
