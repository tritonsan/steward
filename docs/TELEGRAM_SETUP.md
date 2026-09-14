# Connect a Telegram group

Management can connect a group from **Settings → Community Telegram group**.
This installation connects one group to the existing Northgate workspace. It does
not create separate communities for arbitrary visitors. A pasted invite URL cannot
add a bot or prove who manages the group.

## Application operator: configure the bot once

Create a dedicated test bot with BotFather. Supply the following through protected
local environment configuration or the existing channel secret mechanism, identically
for API and worker. Never put the bot token in the frontend or commit it to the repo.

- `STEWARD_TELEGRAM_ENABLED=true`
- `STEWARD_TELEGRAM_BOT_USERNAME`: bot username without `@`
- `STEWARD_TELEGRAM_BOT_TOKEN`: BotFather token
- `STEWARD_TELEGRAM_WEBHOOK_SECRET`: a strong random secret, at least 16 characters

An initial group ID is no longer required when the bot token and username are present.
The persistent connection replaces the legacy static group allowlist once management
connects a group. Disconnecting does not reactivate that old allowlist.

For the local test, enable `STEWARD_TELEGRAM_TEST_POLLING=true` on the worker only.
The dedicated test bot must have no active webhook. Run both processes against the
same database and use real time: omit `STEWARD_SIMULATION_CLOCK_START` for actual
Telegram inputs. The incoming message must not be ahead of a frozen simulation clock.
`STEWARD_EXECUTION_MODE=dry_run` still permits real inbound Telegram reads and real
model inference while simulating mail and Telegram sends. This is sufficient for the
first group-message → case test. Outbound personal Telegram replies need a separately
configured live execution run; do not mistake dry-run receipts for actual sends.

A deployed installation uses the existing HTTPS `/api/channels/telegram` webhook
with its secret header instead of local polling. Group selection does not deploy or
configure the application's global webhook.

## Management: select the group

1. Sign in as a registered community manager.
2. In Settings, connect your personal Telegram account. Open the private link and
   press Start. The account link is single-use and expires in ten minutes.
3. Choose **Choose a Telegram group**, then open the generated Telegram link.
4. Select your test group and add the bot. Make the bot an administrator so it can
   receive ordinary group messages and reliably check member permissions. Your own
   linked Telegram account must also be the group owner or an administrator. If the
   initial add did not include bot permissions, promote it and open the link again.
5. Return to Settings. The page refreshes every five seconds and shows either the
   verified group name or an actionable permission issue.

The group link is also single-use, expires after ten wall-clock minutes and is bound
to the manager's already-linked Telegram account and current connection version.
Telegram permission calls finish before the database transaction. Connection and
token consumption commit together. Adding the bot using someone else's link cannot
authorize that person as a manager.

## One-person live check

From that linked account, send an English message such as:

> The A Block elevator is making a scraping noise near the fourth floor.

The Settings panel should show the received text, then its processing status and
associated case ID after the worker runs. Open Cases to inspect the real result.
Repeat once after restarting the worker to check continuity. A second test participant
is optional. Other residents must link their accounts before their group messages are
accepted; unknown Telegram names are never treated as registered members automatically.

Disconnect stops new inbound group messages and prevents pending group notifications
from dispatching. Existing case evidence remains. A Telegram update removing the bot's
administrator permissions disables the stored connection. Reconnection requires a
fresh manager setup link. Group upgrades to a new chat ID require reconnection in this
version; the application does not guess that two chat IDs are the same group.

## Verification status

Automated tests mock Telegram's permission responses but exercise the actual account
link, API, durable intake, worker, connection persistence, permission removal and
disconnect guards. The same onboarding scenarios run on SQLite and PostgreSQL.
These are not a real Telegram provider test. The actual bot/group run remains pending
until the operator supplies the test bot configuration and sends a group message.

The jury demo can use explicitly labeled simulated channel inputs processed by the
real workflow. A simulation does not establish live Telegram integration evidence.

Reference: [Telegram group deep links](https://core.telegram.org/bots/features#deep-linking)
and [getChatMember](https://core.telegram.org/bots/api#getchatmember).
