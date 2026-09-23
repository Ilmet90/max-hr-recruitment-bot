# Interest Notifications v0.3.1

The MAX bot records candidate activity independently of HR notifications. A session
still groups activity with a three-hour inactivity boundary. HR notification delay
(`off`, `1h`, `3h`, `daily`) does not change that boundary. `/start` and the main
menu alone never qualify. A session qualifies only after a meaningful navigation
event and only while no application, question, or appeal conversion is recorded.

Each approved, active HR staff/head member with `can_receive_notifications=1` has
an individual mode, defaulting to `3h`. The global notification flag overrides
that mode. A mode change applies to new meaningful activity from that point on.
The same options are available in the staff MAX menu and the HR's own web profile.
Delegated head permissions do not add notification recipients.

For `1h` and `3h`, the deadline is measured from the session's latest incoming
activity, including messages that do not create an event. The worker rechecks
rights, mode, activity, conversions, and the 24-hour per-HR/per-user cooldown
immediately before sending. A newer meaningful session replaces an older unsent
one. A conversion in a later session also suppresses older unsent interest.
Already sent interest cannot be retracted if a candidate converts later.

`daily` produces at most one digest per HR per Europe/Moscow calendar day, from
09:00 onward. It includes unsent, unconverted interest whose latest activity was
before the current local day and at least three hours ago. After downtime, one
current digest collects the backlog rather than sending a digest for each missed
day. It contains at most 20 distinct users; others wait for a later day. Multiple
sessions of one user are represented once. The same 24-hour cooldown applies.

The schema stores per-HR/session rows in `interest_deliveries` and per-HR/day
outbound digests in `interest_digests`. Unique keys, short SQLite transactions,
claim tokens, and claim expiry prevent concurrent workers from claiming one
message. The bot worker checks the database after each polling batch, around every
30–60 seconds. Web startup only performs migration. The v0.3.0 session columns
`interest_notification_due_at` and `interest_notification_sent_at` remain NULL.

The MAX interest send path performs one POST. A confirmed success becomes `sent`.
HTTP 429 retries at increasing intervals, with at most five attempts. Permanent
4xx errors stop retrying. Timeout, connection failure, HTTP 408/5xx, an expired
in-flight claim, or another ambiguous result becomes `uncertain` and is never
automatically resent: MAX may already have accepted the POST. An uncertain send
also conservatively counts toward the 24-hour cooldown. An expired claim
before the network attempt can safely return to `pending`. This protects HR from
duplicate alerts at the cost of a possible missed alert after an ambiguous result.
The normal application/question/appeal notification path is unchanged.

Messages show only recorded identity and event facts. Vacancy views use saved
`vacancy_title` metadata, so a deleted vacancy remains understandable. The
history button contains a random opaque token; the bot verifies the current HR
recipient before showing up to 25 recent events. A digest button opens a list
of its users first. Times displayed to HR use Europe/Moscow; stored activity
timestamps remain UTC. MAX profile deep links and candidate replies are absent.
Conversations begin only in v0.3.2.

On first migration the database stores an activation timestamp. Activity recorded
before activation is retained but does not generate interest notifications. The
migration is additive, idempotent, and uses `BEGIN IMMEDIATE` to serialize close
bot/web startups.
