# v0.3.2 Conversations

## Model and migration

The migration runs after the v0.3.0 activity and v0.3.1 interest migrations. It uses SQLite `BEGIN IMMEDIATE`, the existing busy timeout, additive `CREATE TABLE/INDEX IF NOT EXISTS` statements, and checks the resulting structure. Repeated or concurrent `init_db()` calls are safe. Existing rows are not rewritten and no retrospective conversations are created.

There is one durable `conversations` row per confirmed `messenger_users` identity. The unique `messenger_user_id` key is the boundary. Questions, applications, and appeals become `conversation_sources` referencing this conversation. A source is linked only when its own `messenger_user_id` matches; names, usernames, and legacy text IDs are not sufficient. Old confirmed sources are linked when a conversation is opened or a new event occurs. Deleting a legacy record removes its source link while preserving messages and the conversation.

`conversation_messages` stores candidate inbound text and HR outbound text. A completed question or appeal adds one inbound message containing its body. Application form fields stay on the application card. Activity events stay in `user_activity_events`; they are displayed separately. New inbound messages use MAX `message.body.mid` for the partial unique `(messenger, external_message_id)` key. A missing MID is stored as `NULL` and is not deduplicated using content or time.

## Inbound MAX flow and cursor

Only private `message_created` free text enters a human conversation. Bot commands, staff actions, active application/question/appeal forms, and recognized navigation keep precedence. Free text is stored only when a conversation already exists. Group/channel events, callbacks, edits, and bot-authored events do not create candidate messages. A new free text message reopens a closed conversation and sends a short notification to active approved notification recipients, with a Reply button.

The Long Polling `marker` is read from the `max_updates_marker` setting when the bot starts and saved after the whole received batch succeeds. `processed_max_updates` records MAX message MIDs and callback IDs as each update succeeds, so a replay after a crash does not repeat already completed work. The final MID of a structured form is recorded in the same transaction as its legacy record and source link. An update without MID cannot receive this guarantee; no unsafe content based deduplication is attempted. No webhook change is included.

## HR outbound and delivery states

The web panel and staff MAX replies call the same outbound function. It validates the current HR role, candidate identity, text length, and request key, then creates a `pending` message. A separate atomic update claims it as `sending`. One `send_message_once()` request addresses the candidate by `messenger_users.external_user_id`. No database transaction remains open during HTTP. A successful API response is `sent`; 4xx including 429 is `failed`; timeouts, connection errors, 5xx, and other ambiguous errors are `uncertain`. No automatic retry happens for conversation messages. Sending rows older than 90 seconds are marked `uncertain` on bot/web startup and during normal operation. Pending rows older than 90 seconds are marked `failed` because no network send was claimed.

The web form uses a random unique `request_key` and POST redirect GET. A repeat POST returns the existing result without another network send. A staff reply uses the HR message MID as its stable key; a staff message without MID is not forwarded. A failed message can be retried manually from the web panel: a new message row and request key record the new attempt while the failed row remains visible. Uncertain messages have no retry control.

Staff Reply buttons use an opaque conversation token. A verified HR click starts a ten minute in-memory reply state. `/cancel`, a new target selection, expired state, lost bot permission, or a bot restart prevents accidental forwarding. Reply text is sent to the candidate by MAX user ID. The action token is context, not a substitute for checking current HR permissions.

## Reading, status, and interest

`admin_conversation_state` holds a separate read boundary for each HR (`hr:<id>`) and for the virtual web superadmin (`superadmin`). Opening the web detail marks only the last inbound message included in that rendered timeline. New inbound messages arriving after the snapshot remain unread. Outbound messages do not add unread. Closing a conversation preserves its history and does not mark it read. New inbound text, source links, and HR outbound text reopen it.

The web panel has Open and Closed dialog lists, an individual timeline, delivery states, candidate profile, recent activity, and related records. HR staff, HR head, and the existing web superadmin can use it. The interest notification has human labels for History and Write to candidate. A daily digest opens a candidate list; selecting a candidate opens activity history with a Write button. Callback payloads hold the opaque tokens, while message bodies and visible labels do not. Older message-button commands remain accepted. Starting a reply does not create a conversation; sending the message does. When an HR contacts a candidate before a pending interest delivery, that delivery is cancelled. Failed outbound attempts may become eligible again; sent or uncertain contact suppresses another pending notice for the same session.

Opening a native MAX user profile by a deep link remains deferred because the audited API contract does not establish a reliable browser profile link. The HR can contact the candidate through the shared bot without one.
