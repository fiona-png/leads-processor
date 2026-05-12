# cole-leads-processor

Rebuild of the n8n "Cole Leads Processor" workflow as a Python project.

Watches `leads@colellc.com` / `leads@cole.co` / `leads@colegroup.com`, uses Claude (one call, with native `web_search`) to parse forwarded leads and research the hiring company, then creates Search records in Airtable.

## Status

- [x] Scaffold + dependencies
- [x] Models (`ParsedEmail`, `CompanyResearch`, `Lead`, `SearchRecord`)
- [x] Filters (forward/reply detection, inner-header parsing)
- [x] Combined Claude parse+research call
- [ ] Gmail fetch
- [ ] Airtable client (exact-match lookups, idempotent Search creation)
- [ ] End-to-end pipeline
- [ ] CI cron on GitHub Actions

## Local dev

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in real values
pytest
```

## How it differs from the n8n workflow

1. **Idempotent.** Gmail message ID is stored on the Search record; we skip if it already exists.
2. **One Claude call, not two.** Parse + research collapsed into a single tool-use call that also uses `web_search_20250305`.
3. **Exact-match Airtable lookups.** No more substring collisions between similar client names.
4. **Inner forwarded headers parsed deterministically** before the LLM sees the message, so the LLM doesn't have to guess the original sender.
5. **Runs on GitHub Actions cron every 15 min** instead of n8n's 1-minute poll.
6. **Tested.** Pytest fixtures from real (sanitized) leads.

## Secrets

Nothing is hardcoded. Required env vars (see `.env.example`):

- `ANTHROPIC_API_KEY`
- `AIRTABLE_PAT`
- `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `GMAIL_USER`
