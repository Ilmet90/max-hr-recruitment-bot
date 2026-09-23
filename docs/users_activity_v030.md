# Users & Activity v0.3.0

The MAX bot stores candidate identity under `(messenger, external_user_id)`. For MAX,
the external ID comes from the existing update user ID extraction. Chat ID and
username never identify a person. Missing user ID leaves the legacy candidate
flow intact and creates no identity, session, or activity event; submitted records
then have a NULL `messenger_user_id`.

New identity, session, and activity timestamps use UTC milliseconds with a `Z`
suffix. Existing local-time timestamps and `now_iso()` are unchanged.

Each incoming candidate message with a confirmed user ID updates `last_seen_at`
and the open session's `last_activity_at`. A gap of at least three hours closes
the previous session at its last activity plus three hours and opens a new one.
Confirmed active HR staff are excluded, including when using candidate screens.
The existing in-memory form state is unchanged and still resets on bot restart.

The ten event types are `bot_started`, `main_menu_opened`, `vacancies_opened`,
`vacancy_viewed`, `vacancy_apply_started`, `vacancy_application_submitted`,
`conditions_opened`, `question_section_opened`, `question_sent`, and `appeal_sent`.
The first five meaningful navigation events are vacancies opened/viewed, apply
started, conditions opened, and question section opened. The three submission
events are conversions and also mark a session meaningful. The first conversion
sets the session's conversion type; later conversions remain in the event log.

Candidate submissions and their conversion events share one SQLite transaction.
No event is written when the sender ID is unavailable. When a known MAX user
returns, old questions and appeals with the exact same `max_user_id` gain a link;
old applications are not linked automatically. This does not create historical
sessions or events and does not change old timestamps.

The migration is additive and runs at startup. Both legacy initialization and
the new structural migration use SQLite write transactions. The new tables
contain nullable fields reserved for future interest notifications, but v0.3.0
does not schedule or send any. The current update parser does not establish a
stable delivery ID, so duplicate delivery retains the existing behavior; no
content-based deduplication is attempted.
