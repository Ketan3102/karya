# Connect the Kārya bot (about 10 minutes, once)

The bot lives in a Slack workspace of your own. Only the reminders go there;
reading your asks still happens in your work workspace through the Slack
connector.

- **Private.** It's your workspace: nobody at work can see it, the bot, or
  the messages.
- **No approval.** You're the owner, so nobody has to approve the app.
- **Your phone buzzes.** The messages come from the bot, not from you.
- **It arrives with the Mac asleep.** Slack holds the scheduled 08:00 message
  and delivers it itself.

## 1. Create your workspace

Go to <https://slack.com/get-started#/createnew>, sign in with any email (a
personal Gmail keeps it separate from work, but your work email is fine), name
it after yourself, and skip inviting anyone.

## 2. Create the app from the manifest

Copy the manifest to your clipboard from the Claude prompt:

```
! pbcopy < ~/karya/slack-app-manifest.json
```

Then go to <https://api.slack.com/apps> → **Create New App** → **From a
manifest**. Pick your **new** workspace (not your work one) → **Next** → **JSON**
tab → paste → **Next** → **Create**.

The manifest asks for four permissions only: send messages (`chat:write`), open
a DM with you (`im:write`), and look you up by email (`users:read`,
`users:read.email`). It can't read any channel.

## 3. Install it and copy the token

In the app's left menu: **Install App** → **Install to ‹your workspace›** →
**Allow**. Then **OAuth & Permissions** → **Bot User OAuth Token** → **Copy**
(it starts with `xoxb-`).

## 4. Connect it

```
! python3 ~/karya/setup_slack.py --from-clipboard
```

Copy the token first: the script reads it from the clipboard (it is never shown, never goes through
this chat), and confirm the email you used for the new workspace. A test DM
from **Karya** arrives straight away. The token is saved in
`~/karya/config.json`, which only you can read and which is never committed.

## 5. Add the workspace on your phone

In the Slack app, tap the workspace switcher → **Add a workspace** → sign in to
the new one. DMs notify you by default.

To prove the 08:00 delivery doesn't need the Mac, run this, close the lid,
and watch your phone:

```
! python3 ~/karya/notify.py --test-schedule
```

## What happens from then on

| When | What |
|---|---|
| 21:00 sync | Tomorrow's plan is handed to Slack, scheduled for **08:00** |
| 08:00 | The plan arrives: leftovers from yesterday (now overdue), due today, due in 2 days, urgent, the rest of the plan |
| 09:00 and 15:00 syncs | A short ping, only if new asks arrived since the last one |
| Friday 18:30 | The weekly recap email in your Gmail |

If the Mac is asleep at 21:00, the missed run happens when you open it. Before
08:00, it still schedules the 08:00 plan; after 08:00, it sends today's plan
straight away. If you usually close the lid before 21:00, move the evening
slot earlier in `launchd/ai.karya.sync.plist` and run `./install.sh jobs`.
