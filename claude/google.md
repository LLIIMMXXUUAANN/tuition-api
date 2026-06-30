## Google services (`app/features/google/`)

All Google API calls go through `app/features/google/`. The OAuth flow uses Next.js as the browser-facing layer: `GET /google/auth-url` returns the consent URL (Next.js redirects the browser), and `POST /google/callback` receives `{code, state}` from Next.js after Google redirects back — both routes are protected by `X-Internal-Secret`.

### `auth.py` — OAuth credentials + CSRF

- `get_oauth2_credentials(supabase)` reads the refresh token from the `settings` table and returns `(Credentials, original_token)`.
- `save_token_if_rotated(creds, original_token, supabase)` detects and persists rotated refresh tokens (Google Auth updates `creds.refresh_token` in-place on rotation). Every endpoint that calls Google APIs must call this after the operation. The save is non-fatal — if the DB upsert fails, the failure is logged at ERROR level (`logger.exception`) and the current request still succeeds; the risk is the next request loading a now-invalid token from DB and failing with `invalid_grant`.
- CSRF protection: `generate_state_token()` / `verify_and_consume_state(token)` use a module-level `_pending_states: dict[str, float]` with a 10-minute TTL. The state token is embedded in the OAuth redirect URL and verified before the code exchange.

### `calendar.py` — recurring class events

- `create_weekly_class_events` creates recurring events; the first slot gets a Google Meet conference (one Meet link per student); subsequent slots reference the same link in their description.
- Datetime strings are formatted as naive `YYYY-MM-DDTHH:MM:SS` (no `Z`) with `timeZone: Asia/Kuala_Lumpur` so Google Calendar interprets them as MYT regardless of server timezone.
- `_slot_date_times(slot)` anchors to the next occurrence of `slot.day` strictly after today (starting from tomorrow avoids "has today already passed?" logic). End time is computed from start + duration via `time_to_mins` so end > start is always guaranteed.
- `update_weekly_class_events` is nuke-and-repave: patches the primary event (the one with `hangoutLink`) or creates a new one with `conferenceData` if the primary was deleted. Always returns `effective_meet_link` (existing `hangoutLink` from primary, or newly generated link) alongside the backward-compat `meet_link` (only set when freshly generated).
- `find_recurring_event_ids` searches Calendar over 90 days (`singleEvents: true`, `maxResults: 200`), collects unique `recurringEventId` values via a set.

### `drive.py` — student Drive folders + Meet docs

- `parse_drive_folder_id(url)` extracts and validates the folder ID from a Drive URL — shared by `update_student_meet_doc` and `delete_student`.
- `create_student_drive_folder` is atomic: deletes the root folder on any subfolder/doc creation failure so retries don't leave duplicates. `My Python Syllabus` → 4 subfolders + Meet doc; `Other Syllabus` → root folder + Meet doc only.
- `update_student_meet_doc` finds the "Google Meet Link" doc by name and rewrites its HTML content.

### `cleanup.py` — student removal

- `delete_student_google` trashes the Drive folder and deletes Calendar events in parallel via `asyncio.gather`; both are non-fatal. Calendar deletes collect per-event failures and raise `RuntimeError` if any event could not be deleted (404/410 are silently swallowed as already-deleted).

### `sync.py` — bulk sync

- `sync_all_students` handles all missing-resource combinations. Only skips students with no `class_schedule`.
- For each student: (1) search Calendar and merge found IDs with DB IDs — catches rogue events not tracked in the DB; (2) if event IDs exist → `update_weekly_class_events` (nuke-and-repave, recovers existing Meet link via `effective_meet_link`); if none → `create_weekly_class_events`; (3) save updated IDs + Meet link to DB only if changed; (4) if Drive folder missing → `create_student_drive_folder`; if folder exists → `update_student_meet_doc`.
- `invalid_grant` errors surface as "Google auth expired — reconnect".
- Error messages use `err_msg` from `app.features.agent.tools.shared` — no private reimplementation.
- All students processed in parallel via `asyncio.gather`.
