# rize-to-calendar

Sync your [Rize.io](https://rize.io) time entries to Google Calendar.

Rize automatically tracks everything you do, but only the time entries you
record deliberately reflect what you were actually working on. This tool syncs
**only time entries that have a project assigned** (i.e. the ones you recorded
via the timer or tagged manually) into a dedicated Google Calendar, including
task and focus/meeting session info:

- One calendar event per qualifying time entry, titled `[Project] Task`,
  with the entry's full description in the event body
- Focus sessions are colored blue, meetings purple
- Events are marked free (`transparent`) so they never block your availability
- Idempotent: safe to re-run over any window, no duplicate events
  (deduplicated via `extendedProperties.private.rizeTimeEntryId`)
- Reconciling: entries deleted or un-tagged in Rize are removed from the
  calendar on the next sync; events you created manually are never touched

## Setup

### 1. Rize API key

Generate one in Rize under *Settings > API*, then:

```bash
cp .env.example .env
# put your key in RIZE_API_KEY=...
```

### 2. Google Calendar credentials

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/),
   enable the **Google Calendar API**.
2. Configure the OAuth consent screen (External). Publish it to **production**
   — in Testing status refresh tokens expire after 7 days.
3. Create an OAuth client ID of type **Desktop app** and save the downloaded
   file as `credentials.json` in this directory.

### 3. Install and authorize

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python rize_to_calendar.py --auth   # one-time browser authorization
```

### 4. Sync

```bash
.venv/bin/python rize_to_calendar.py --dry-run          # preview last 3 days
.venv/bin/python rize_to_calendar.py                    # sync last 3 days
.venv/bin/python rize_to_calendar.py --start 2026-01-01 # backfill from a date
```

If `GOOGLE_CALENDAR_ID` is not set in `.env`, the script finds or creates a
calendar named `Rize` and uses it.

## Run automatically (macOS)

```bash
cp com.rize-to-calendar.plist.example ~/Library/LaunchAgents/com.rize-to-calendar.plist
# edit the paths inside to match your checkout, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rize-to-calendar.plist
```

This syncs the last 3 days every 30 minutes and logs to `logs/sync.log`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `RIZE_API_KEY` | required | Rize GraphQL API key |
| `GOOGLE_CALENDAR_ID` | auto | Target calendar ID; found/created by name if unset |
| `RIZE_CALENDAR_NAME` | `Rize` | Calendar name used when auto-creating |
| `SYNC_LOOKBACK_DAYS` | `3` | Default sync window |

## License

MIT
