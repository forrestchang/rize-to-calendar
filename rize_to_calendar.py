#!/usr/bin/env python3
"""Sync Rize.io time entries to Google Calendar.

Only time entries with a project assigned are synced — in Rize these are the
manually recorded ones (source `user` or `timer`), while auto-tracked entries
(source `ai` / `meeting`) carry no project and are skipped. Task and
overlapping focus/meeting session info is attached to each event.

Events are deduplicated via extendedProperties.private.rizeTimeEntryId, so the
sync is idempotent and safe to run repeatedly over overlapping windows.
Within the sync window the script also deletes previously synced events whose
Rize entry no longer exists or no longer qualifies. Events without the marker
(manually created ones) are never touched.

Usage:
  rize_to_calendar.py                    # sync the last SYNC_LOOKBACK_DAYS days
  rize_to_calendar.py --days 14          # sync the last 14 days
  rize_to_calendar.py --start 2026-03-01 # sync from a date up to now
  rize_to_calendar.py --dry-run          # show what would change
  rize_to_calendar.py --auth             # run the interactive OAuth flow
"""

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/calendar"]
RIZE_ENDPOINT = "https://api.rize.io/api/v1/graphql"
CALENDAR_NAME = os.environ.get("RIZE_CALENDAR_NAME", "Rize")
SESSION_COLOR = {"focus": "9", "meeting": "3"}  # blueberry / grape

log = logging.getLogger("rize-to-calendar")

TIME_ENTRIES_QUERY = """
query TimeEntries($startTime: ISO8601DateTime, $endTime: ISO8601DateTime, $after: String) {
  timeEntries(startTime: $startTime, endTime: $endTime, first: 200, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title description duration startTime endTime status source updatedAt
      project { name }
      task { name }
      client { name }
    }
  }
}
"""

SESSIONS_QUERY = """
query Sessions($startTime: ISO8601DateTime, $endTime: ISO8601DateTime) {
  sessions(startTime: $startTime, endTime: $endTime) {
    id title type source startTime endTime
  }
}
"""


def load_env():
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def rize_query(query, variables):
    api_key = os.environ.get("RIZE_API_KEY")
    if not api_key:
        sys.exit("RIZE_API_KEY is not set (put it in .env next to this script)")
    req = urllib.request.Request(
        RIZE_ENDPOINT,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(f"Rize API error: {payload['errors']}")
    return payload["data"]


def fetch_time_entries(start, end):
    nodes, after = [], None
    while True:
        data = rize_query(TIME_ENTRIES_QUERY, {
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
            "after": after,
        })["timeEntries"]
        nodes.extend(data["nodes"])
        if not data["pageInfo"]["hasNextPage"]:
            return nodes
        after = data["pageInfo"]["endCursor"]


def fetch_sessions(start, end):
    data = rize_query(SESSIONS_QUERY, {
        "startTime": start.isoformat(),
        "endTime": end.isoformat(),
    })
    return [s for s in data["sessions"] if s["type"] in ("focus", "meeting")]


def is_running(entry, now, grace):
    """Whether a time entry is still in progress and must not be synced yet.

    Rize's `status` stays "active" forever, so it can't tell a finished entry
    from a running one. A running timer instead keeps its `endTime` pinned to
    the present (it advances toward, and can even sit just past, "now"), while a
    finished entry has an `endTime` fixed in the past. We treat an entry as
    running until its `endTime` is at least `grace` behind now; it then gets
    picked up on a later run once the timer has actually stopped.
    """
    return datetime.fromisoformat(entry["endTime"]) > now - grace


def match_session(entry, parsed_sessions):
    """Return the focus/meeting session with the largest time overlap."""
    e_start = datetime.fromisoformat(entry["startTime"])
    e_end = datetime.fromisoformat(entry["endTime"])
    best, best_overlap = None, timedelta(0)
    for s_start, s_end, s in parsed_sessions:
        overlap = min(e_end, s_end) - max(e_start, s_start)
        if overlap > best_overlap:
            best, best_overlap = s, overlap
    return best


def get_calendar_service(interactive=False):
    token_path = BASE_DIR / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds or not creds.valid:
        if not interactive:
            sys.exit("No valid Google token. Run once with --auth to authorize.")
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(
            str(BASE_DIR / "credentials.json"), SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_calendar_id(svc):
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if cal_id:
        return cal_id
    page_token = None
    while True:
        resp = svc.calendarList().list(pageToken=page_token).execute()
        for cal in resp.get("items", []):
            if cal.get("summary") == CALENDAR_NAME:
                log.info("Using existing calendar %r (%s)", CALENDAR_NAME, cal["id"])
                return cal["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    created = svc.calendars().insert(body={"summary": CALENDAR_NAME}).execute()
    log.info("Created calendar %r (%s)", CALENDAR_NAME, created["id"])
    return created["id"]


def fmt_duration(seconds):
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def build_event(entry, session):
    project = entry["project"]["name"]
    task = entry["task"]["name"] if entry.get("task") else None
    client = entry["client"]["name"] if entry.get("client") else None
    desc = (entry.get("description") or entry.get("title") or "").strip()

    summary = f"[{project}] {task}" if task else f"[{project}]"

    lines = []
    if desc:
        lines += [desc, ""]
    lines.append(f"Project: {project}")
    if client:
        lines.append(f"Client: {client}")
    if task:
        lines.append(f"Task: {task}")
    if session:
        lines.append(f"Session: {session['type'].capitalize()}"
                     f" ({session.get('title') or 'untitled'})")
    lines.append(f"Duration: {fmt_duration(entry['duration'])}")
    lines.append(f"Source: {entry['source']}")

    private = {
        "rizeTimeEntryId": str(entry["id"]),
        "rizeProject": project,
        "rizeSource": entry["source"],
        "rizeUpdatedAt": entry.get("updatedAt") or "",
        "rizeDurationSeconds": str(entry["duration"]),
    }
    if task:
        private["rizeTask"] = task
    if session:
        private["rizeSessionType"] = session["type"]

    event = {
        "summary": summary,
        "description": "\n".join(lines),
        "start": {"dateTime": entry["startTime"]},
        "end": {"dateTime": entry["endTime"]},
        "transparency": "transparent",
        "reminders": {"useDefault": False},
        "extendedProperties": {"private": private},
    }
    color = SESSION_COLOR.get(session["type"]) if session else None
    if color:
        event["colorId"] = color
    return event


def fetch_existing_events(svc, cal_id, start, end):
    """Map rizeTimeEntryId -> event for synced events in the window."""
    existing, page_token = {}, None
    while True:
        resp = svc.events().list(
            calendarId=cal_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, maxResults=2500, pageToken=page_token,
        ).execute()
        for ev in resp.get("items", []):
            entry_id = (ev.get("extendedProperties", {})
                        .get("private", {}).get("rizeTimeEntryId"))
            if entry_id:
                existing[entry_id] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            return existing


def needs_update(existing, desired):
    if (existing.get("extendedProperties", {}).get("private", {})
            != desired["extendedProperties"]["private"]):
        return True
    for field in ("summary", "description", "transparency"):
        if existing.get(field) != desired.get(field):
            return True
    if existing.get("colorId") != desired.get("colorId"):
        return True
    for boundary in ("start", "end"):
        if (datetime.fromisoformat(existing[boundary]["dateTime"])
                != datetime.fromisoformat(desired[boundary]["dateTime"])):
            return True
    return False


def sync(start, end, dry_run=False, delete_stale=True):
    entries = fetch_time_entries(start, end)
    now = datetime.now(timezone.utc)
    grace = timedelta(seconds=int(os.environ.get("SYNC_RUNNING_GRACE_SECONDS", "60")))
    with_project = [e for e in entries if e.get("project")]
    qualified = [e for e in with_project if not is_running(e, now, grace)]
    running = len(with_project) - len(qualified)
    sessions = [(datetime.fromisoformat(s["startTime"]),
                 datetime.fromisoformat(s["endTime"]), s)
                for s in fetch_sessions(start, end)]
    log.info("Window %s → %s: %d entries, %d with a project "
             "(%d still running, skipped), %d focus/meeting sessions",
             start.date(), end.date(), len(entries), len(with_project),
             running, len(sessions))

    svc = get_calendar_service()
    cal_id = get_calendar_id(svc)
    existing = fetch_existing_events(svc, cal_id, start, end)

    desired_ids = set()
    created = updated = unchanged = deleted = 0
    for entry in qualified:
        session = match_session(entry, sessions)
        desired = build_event(entry, session)
        entry_id = str(entry["id"])
        desired_ids.add(entry_id)
        current = existing.get(entry_id)
        if current is None:
            created += 1
            log.info("create  %s  %s", entry["startTime"], desired["summary"])
            if not dry_run:
                svc.events().insert(calendarId=cal_id, body=desired).execute()
        elif needs_update(current, desired):
            updated += 1
            log.info("update  %s  %s", entry["startTime"], desired["summary"])
            if not dry_run:
                svc.events().update(
                    calendarId=cal_id, eventId=current["id"], body=desired).execute()
        else:
            unchanged += 1

    if delete_stale:
        for entry_id, ev in existing.items():
            if entry_id not in desired_ids:
                deleted += 1
                log.info("delete  %s  %s",
                         ev.get("start", {}).get("dateTime"), ev.get("summary"))
                if not dry_run:
                    try:
                        svc.events().delete(
                            calendarId=cal_id, eventId=ev["id"]).execute()
                    except HttpError as err:
                        if err.resp.status != 410:  # already gone
                            raise

    log.info("%s: %d created, %d updated, %d unchanged, %d deleted",
             "DRY RUN" if dry_run else "Done", created, updated, unchanged, deleted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int,
                        default=int(os.environ.get("SYNC_LOOKBACK_DAYS", "3")))
    parser.add_argument("--start", help="sync window start (YYYY-MM-DD)")
    parser.add_argument("--end", help="sync window end (YYYY-MM-DD), default now")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-delete", action="store_true",
                        help="do not delete stale synced events in the window")
    parser.add_argument("--auth", action="store_true",
                        help="run the interactive OAuth flow and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    load_env()

    if args.auth:
        get_calendar_service(interactive=True)
        log.info("Authorization complete; token.json saved.")
        return

    end = (datetime.fromisoformat(args.end).astimezone()
           if args.end else datetime.now(timezone.utc))
    start = (datetime.fromisoformat(args.start).astimezone()
             if args.start else end - timedelta(days=args.days))
    sync(start, end, dry_run=args.dry_run, delete_stale=not args.no_delete)


if __name__ == "__main__":
    main()
