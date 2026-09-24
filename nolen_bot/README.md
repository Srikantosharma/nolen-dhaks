# Nolen Bot — Fixed Build

Key fixes/features:
- `/start`, `/admin`, and `/id` work only in private chats.
- `/profile` and `/claim` work only in the official Nolen group.
- Any other group command is ignored.
- `/profile` shows a compact profile and attempts to include the user's Telegram avatar.
- `/claim` gives a once-per-day 15–33 Nolen bonus and maintains a daily streak.
- Referral deep links are correctly saved; referral earnings apply to chat rewards and claim bonuses.
- Chat earning was reduced to a lower hidden range.
- Nolen → Stars conversion atomically consumes Stars payout stock.
- Official group ID/link are locked in code to prevent a bad Render GROUP_LINK override.
- Bot token is read only from `BOT_TOKEN`; no token is hardcoded.
- Inline button background colours are controlled by Telegram; the UI uses emoji accents instead.

Important:
The bot token that was exposed in the earlier source should be revoked/regenerated in BotFather before using this build.
