# Nolen Bot

Telegram chat-to-earn bot for @nolen_bot / @nolen_chat.

## Local

```powershell
py -m pip install -r requirements.txt
Copy-Item .env.example .env
# edit .env and add BOT_TOKEN
py nolen_bot.py
```

## Telegram requirements

1. Add the bot to `@nolen_chat`.
2. Make it an admin.
3. In BotFather, make sure group privacy does not prevent the bot from receiving ordinary group messages. An admin bot receives all group messages.

## Render

Use a paid Render Web Service and attach a persistent disk mounted at `/var/data`.

Build command:
`pip install -r requirements.txt`

Start command:
`python nolen_bot.py`

Set:
`BOT_TOKEN` = BotFather token
`DATABASE_PATH` = `/var/data/nolen.db`

Render provides `RENDER_EXTERNAL_URL`, which this app uses automatically for the webhook.

## Important Stars note

The app supports an internal Stars balance, conversion packages and Stars withdrawal requests with stock reservation. The Bot API does not provide a simple generic method for sending an arbitrary raw Stars balance directly to another user's personal Stars balance. The admin therefore confirms a payout only after completing the real supported Telegram payout flow.
