# Telegram identity hardening: deploy runbook

For the owner. Run the commands on the production server, from the project
directory. Every command here is read-only unless it says otherwise.

First define a shortcut (once per terminal session). Commands below use `$DC`:

    DC="docker compose -f docker-compose.yml -f docker-compose.production.yml"

## What changed

- Eight dangerous identity endpoints were deleted: `link-telegram`,
  `link-web-account`, `sync-profile`, `suggest-auto-link`,
  `generate-telegram-auth`, `generate-web-auth`, `verify-telegram-auth/<code>`
  and `verify-web-auth/<token>`.
- Five endpoints are now bot-only and need the bot signature header:
  `telegram-login`, `telegram-register`, `check-phone-availability`,
  `link-phone-account/send-otp`, `link-phone-account/verify`.
- nginx now answers 404 for all thirteen paths on the three public hosts
  (`aqua-element.uz`, `cabinet.aqua-element.uz`, `admin.aqua-element.uz`).
  This is a second lock: even a backend mistake cannot expose them.
- A phone number is saved only after its SMS code is verified. `/send-otp`
  and `/change-phone` no longer write the number early, and a code works only
  for the number it was sent to. In the customer bot, Profile → Phone →
  "Change number" with a TYPED number now sends a code first; before, it wrote
  the number straight into the database with no code. A shared Telegram
  contact works as before.

## 1. Precondition: the bots must reach the backend internally

The nginx block would break a bot that calls the backend through the public
address. Check that both bots use the internal address:

    $DC exec -T telegram_bot printenv BUSINESS_APP_URL
    $DC exec -T staff_bot printenv BUSINESS_APP_URL

Both must print `http://business_app:80`, or nothing at all. Empty output
means the variable is not set and the bot uses its built-in default,
`http://business_app:80`, so that is fine and not a reason to stop. If either
prints an `https://aqua-element.uz...` address, stop. Fix the `.env` value
first and recreate that bot, or the nginx block will cut it off.

(The staff bot calls none of the thirteen paths, so the nginx block cannot
break it either way. It is checked here only to be safe.)

## 2. Optional same-day step: nginx only

This closes the public exposure before the code deploy. It changes no
application code. The nginx config folder is mounted into the container, so
after `git pull` the new rule is already on disk.

**Restart the customer bot immediately after `git pull`, before the nginx
commands.** `git pull` also puts the new backend code on disk, and
`business_app` runs it from the mounted folder. From that moment, any restart
of `business_app` (a crash, an out-of-memory kill on the Pi, or a manual
restart) starts requiring the signature while the old, unsigned bot is still
running, and new-customer registration and phone-link break. The new bot works
with the old backend, so restart it straight away (this is step 3.1):

    git pull
    $DC build telegram_bot
    $DC up -d --no-deps telegram_bot

Never leave the server pulled with the old bot still running. Then:

    $DC exec nginx nginx -t
    $DC exec nginx nginx -s reload     # changes live traffic: only if -t said "successful"

Check it (expect `404` for each of these, from outside the server):

    curl -s -o /dev/null -w '%{http_code}\n' -X POST https://aqua-element.uz/api/v1/auth/sync-profile
    curl -s -o /dev/null -w '%{http_code}\n' -X POST https://cabinet.aqua-element.uz/api/v1/auth/telegram-login
    curl -s -o /dev/null -w '%{http_code}\n' -X POST https://admin.aqua-element.uz/api/v1/auth/link-telegram

Normal login must still work (`/api/v1/auth/login` must not return the nginx 404).

## 3. Deploy order

Do not run `scripts/deploy.sh` for this. It runs `up -d --build` on every
service at once, so there is no way to control which one restarts first.
The order matters:

1. Rebuild and restart only the customer bot. It now signs its requests; the
   current backend simply ignores the extra header, so nothing breaks. If you
   did step 2, this is already done. If you skipped step 2, run `git pull` and
   then these two commands straight away, for the reason given in step 2:

       $DC build telegram_bot
       $DC up -d --no-deps telegram_bot

2. Confirm it works before going further: register a test customer through the
   bot and link a phone number. Watch for errors:

       $DC logs -n 100 telegram_bot

3. Only then restart the backend so it starts requiring the signature. The
   backend code is mounted from disk and gunicorn loads it once at start
   (`preload_app`), so the new code only takes effect on restart:

       $DC restart business_app
       $DC ps business_app        # wait for "healthy"
       $DC restart nginx          # nginx caches the backend address; deploy.sh does this too

   SMS codes sent before this restart stop working: a phone code is refused
   for up to 30 minutes after it was sent, and an account-link code for up to
   10 minutes. A customer who hits this just asks for a new code.

4. Repeat the registration and phone-link check through the bot, and change a
   phone number by typing it in Profile → Phone (a code must arrive before the
   number shows). Then check that the backend itself refuses an unsigned
   request. `business_app` is published on the server at `127.0.0.1:5000`, so
   this request does not go through nginx:

       curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:5000/api/v1/auth/telegram-register -H 'Content-Type: application/json' -d '{}'

   Expect `401`. A `400` means the backend is still running the old code.
   (Going through nginx on port 81 would only show nginx's own `404`, which
   proves nothing about the backend.)

If step 2 fails, do not restart the backend. The old backend is still serving
and nothing is lost.

## 4. Incident review: were these endpoints already abused?

Use Grafana, Explore, Loki. Labels are `service_name` and `env="prod"`.
Loki keeps only 30 days of logs (`retention_period: 720h` in
`monitoring/loki-config.yml`), and one query is capped at a 30-day window. Set
the time range to the last 30 days; to look further back, split it into
30-day windows. Anything older than that is only in the nginx access logs
below.

Requests that reached the backend from outside Docker (the bot uses the
internal network, so its addresses start with `172.`; production logs confirm
the bot calls from `172.18.0.15`).
Routes with `log_request` write a `Request: POST /api/v1/auth/... from <ip>`
line:

    {service_name="business_app", env="prod"} |~ "Request: (POST|GET) /api/v1/auth/(link-telegram|link-web-account|sync-profile|suggest-auto-link|generate-telegram-auth|generate-web-auth|verify-telegram-auth|verify-web-auth|telegram-login|telegram-register|check-phone-availability|link-phone-account)" !~ "from 172\\."

Account merges (from `cross_platform_sync_service.py`, a few lines near 224,
534 and 595). Every line is a merge. Compare against merges you expect:

    {service_name="business_app", env="prod"} |~ "Successfully merged and deleted|Starting full merge|Starting same-platform merge"

Typed numbers the old bot wrote straight into the database (Profile → Phone
→ "Change number"). Only the old code logs this line, once after every such
write. The same customer's `Phone text received for user <id>: <number>` line,
shortly before it, names the number:

    {service_name="telegram_bot", env="prod"} |~ "Phone updated via API for user [0-9]+ from text input"

Some of these routes have no `log_request`. For those, use the nginx access
log, which records the address and status of every request:

    grep -E '/api/v1/auth/(link-telegram|link-web-account|sync-profile|suggest-auto-link|generate-telegram-auth|generate-web-auth|verify-telegram-auth|verify-web-auth)' logs/nginx/access.log*

Anything with status `200` from a public address, before the nginx block, is
suspect. Also look for customers whose placeholder email was replaced without
them asking. Bot-registered customers get a placeholder ending in
`@bluestream.local` or `@bot.internal`, and `sync-profile` replaced only the
`@bluestream.local` ones.

Database check for bot-registered customers whose email is no longer a
placeholder, read-only. Each row is a customer to compare against what they
told you; some will have changed their email themselves:

    $DC exec postgres psql -U postgres -d bluestream_db -c "SELECT id, email, telegram_id, updated_at FROM users WHERE registration_source = 'telegram' AND email NOT LIKE '%@bluestream.local' AND email NOT LIKE '%@bot.internal' ORDER BY updated_at DESC LIMIT 100;"

Two read-only counts for the phone fix:

    # Numbers saved but never verified. The old early write could park these.
    $DC exec postgres psql -U postgres -d bluestream_db -c "SELECT count(*) FROM users WHERE phone IS NOT NULL AND phone_verified_at IS NULL;"

    # Numbers not stored in the +998XXXXXXXXX form. The new duplicate check
    # matches these only exactly as they were typed.
    $DC exec postgres psql -U postgres -d bluestream_db -c "SELECT count(*) FROM users WHERE phone !~ '^\+998[0-9]{9}$';"

The first count can miss numbers parked through the old bot "Change number"
flow, because that flow kept the verified mark of the previous number. Use the
`telegram_bot` Loki query above for those.

If you find abuse, decide whether to rotate `JWT_SECRET_KEY`. That logs out
every user, and production refresh tokens live 7 days, so any stolen login
stays valid for up to a week unless the key changes.

## 5. Follow-ups not done here

- The bot signature does not cover the HTTP method, the path or a timestamp,
  and the same secret is used in both directions.
- Swagger (API docs) is public in production.
- The cabinet and admin hosts have no auth rate limit.
- `rate_limit` stops limiting when Redis is down (it fails open).
- The `ACCOUNT_MATCH_FOUND` branch, and the missing status check in
  `telegram-register`.
- One shared eligibility rule for self-linking accounts.
