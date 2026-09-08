"""
Google Calendar OAuth client — thin wrapper around the Calendar API for
creating/deleting loan-expiry reminder events. Used by portfolio_health.py
so reminders can be kept in sync fully independently, with no Claude/MCP
connector involved.

ONE-TIME SETUP (you do this once — nothing here can do it for you):
  1. https://console.cloud.google.com/ → create or select a project.
  2. APIs & Services → Library → enable "Google Calendar API".
  3. APIs & Services → OAuth consent screen → configure it (External +
     Testing mode is fine for personal use; add your own Google account as
     a test user).
  4. APIs & Services → Credentials → Create Credentials → OAuth client ID
     → Application type "Desktop app".
  5. Download the resulting JSON and save it as
     google_calendar_credentials.json in this directory (gitignored — never
     commit it).
  6. The first time anything in this repo calls create_event/delete_event,
     a browser window opens for you to log in and grant calendar access.
     After that, a refresh token is cached in google_calendar_token.json
     (also gitignored) and reused/refreshed silently — no browser needed
     again unless you revoke access.

Override default file locations via GOOGLE_CALENDAR_CREDENTIALS_PATH /
GOOGLE_CALENDAR_TOKEN_PATH.
"""
from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CALENDAR_CREDENTIALS_PATH",
    os.path.join(os.path.dirname(__file__), "google_calendar_credentials.json"),
)
TOKEN_PATH = os.getenv(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(os.path.dirname(__file__), "google_calendar_token.json"),
)

_service = None


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise RuntimeError(
                    f"No Google OAuth client credentials found at {CREDENTIALS_PATH}. "
                    "See this module's docstring for one-time setup steps."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as fh:
            fh.write(creds.to_json())

    return creds


def _calendar_service():
    global _service
    if _service is None:
        _service = build("calendar", "v3", credentials=_get_credentials(), cache_discovery=False)
    return _service


def create_event(summary: str, description: str, start_iso: str, end_iso: str, popup_minutes_before: int = 0) -> str:
    """Creates an event on the primary calendar with a popup reminder override.
    Returns the new event's ID (needed later to delete it)."""
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": popup_minutes_before}]},
    }
    event = _calendar_service().events().insert(calendarId="primary", body=body).execute()
    return event["id"]


def delete_event(event_id: str) -> None:
    """No-ops (doesn't raise) if the event is already gone (404/410) —
    someone may have deleted it manually, which is fine."""
    try:
        _calendar_service().events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as exc:
        if exc.resp.status not in (404, 410):
            raise
