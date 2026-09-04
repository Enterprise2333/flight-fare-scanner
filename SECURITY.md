# Security policy

## Never commit

Do not commit any of the following:

- `credentials.env`, `.env`, API keys, SMTP passwords, DingTalk webhooks, cookies, or OAuth tokens;
- `config.yaml` if it contains personal watches, email recipients, passport/visa details, or local paths;
- `fares.sqlite3`, `reports/`, logs, `.token.json`, or the runtime `visa-cache.json`.

Only `credentials.env.example` and `config.example.yaml` are safe templates for version control. Copy them to the runtime state directory, then populate them locally.

## Reporting a security issue

Do not open a public issue containing credentials or personal itinerary data. Rotate any exposed credential immediately, remove it from history, and contact the repository maintainer through a private channel.
