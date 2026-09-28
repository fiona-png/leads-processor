# cole-leads-processor

Rebuild of the n8n "Cole Leads Processor" workflow as a Python project.

Watches `leads@colellc.com` / `leads@cole.co` / `leads@colegroup.com`, uses Claude (one call, with native `web_search`) to parse forwarded leads and research the hiring company, then creates Search records in Airtable.

## Status

- [x] Scaffold + dependencies
- [x] Models (`ParsedEmail`, `CompanyResearch`, `Lead`, `SearchRecord`)
- [x] Filters (forward/reply detection, inner-header parsing)
- [x] Combined Claude parse+research call
- [x] Airtable client (exact-match lookups, idempotent Search creation)
- [x] Gmail fetch
- [x] End-to-end pipeline (dry-run + real)
- [x] Real-services integration test
- [x] CI cron on GitHub Actions

## Local dev

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in real values
pytest                 # 120 unit/integration-of-the-mocked-kind tests, all mocked
pytest -m integration  # ONE real-services test; needs .env + costs ~$0.05
```

## How it differs from the n8n workflow

1. **Idempotent.** Gmail message ID is stored on the Search record; we skip if it already exists.
2. **One Claude call, not two.** Parse + research collapsed into a single tool-use call that also uses `web_search_20250305`.
3. **Exact-match Airtable lookups.** No more substring collisions between similar client names.
4. **Inner forwarded headers parsed deterministically** before the LLM sees the message, so the LLM doesn't have to guess the original sender.
5. **Runs on GitHub Actions cron every 15 min** instead of n8n's 1-minute poll.
6. **Lead Source Type checked against Cole's own history.** The model only
   sees the email, so it can't know that Paul Cho is Sequoia's talent partner or
   that TRM Labs hired Cole in 2022. `lead_source.py` loads Clients, Investors and
   every Search once per run and overrides the model:
   * **VC** — referrer's email domain / signature firm / past leads point at a
     firm that has sent Cole VC leads before (also fills *Lead Source (VC Only)*).
     VC intros stay VC even if Cole once ran a search for the VC firm itself.
   * **Existing Client** — hiring company (or the referrer's company) had a
     search Won / Closed / Abandoned / Canceled *before the Lead Date*. Matched by
     record, website domain, and normalized name so duplicate client rows
     ("Atob" / "AtoB") don't hide history.
   * Precedence: VC > Existing Client > Company > referrer history > model.
   * Anything ambiguous (VC intro to an existing client, a duplicate-looking
     client, a "friend" at something that looks like a fund) is written anyway
     and ticked **Leads For Fiona's Review** with a note in *Leads Review Notes*.
   * Hiring companies are matched to existing Client records by website domain
     before creating a new one, so "TRM labs" doesn't spawn a duplicate.
7. **Every other field is checked too** (`derive.py`, runs after the model):
   * **Search Type** is computed from ARR at the Lead Date (<$16M Core, <$51M
     Strategic, else Franchise; Public = Franchise) - the model no longer picks it.
   * **Role / Seniority** use Airtable's exact options, and the literal job title
     wins if the model disagrees ("CRO" is always Chief/Sales). Multi-role leads
     ("CMO / VP Sales") get both roles.
   * **ARR, Series, investors** are researched *as of the Lead Date* (the date is
     in the prompt; rounds after it are ignored). ARR must come with a source and
     year. Raw-dollar ARR is converted to $M; implausible combos (Series A with
     $77M ARR, ARR equal to funding raised) are flagged for review.
   * **Investors** are matched to existing records ("Sequoia" -> "Sequoia
     Capital") instead of creating near-duplicates; genuinely new ones are listed
     in the review note.
   * **HQ** is matched to an existing Airtable option ("New York City" -> "New York").
   * **Rolo (optional):** set `ROLO_MCP_URL` / `ROLO_MCP_TOKEN` repo secrets and
     the model checks Rolo's revenue-by-year for the lead year before the web.
8. **Tested.** Pytest fixtures from real (sanitized) leads, plus a gated real-services smoke test.

## Cutover playbook

Steps to migrate off n8n. Run them in order; each one is safe and reversible until step 7.

```bash
# Step 1 — Dry-run against a fixture (no writes anywhere; LLM still runs).
python scripts/run_once.py --dry-run --fixture tests/fixtures/forwarded_vpm_lead.txt

# Step 2 — Real integration test. Creates one fake client + one fake search
# in the production Airtable base under the name "TestCo Integration <ts>",
# eyeballs it, then deletes both records in a finally block.
pytest -m integration

# Step 3 — Real run on ONE actual unprocessed lead from Gmail.
# Default --max is 1 so a typo can't process the entire inbox.
python scripts/run_once.py --max 1

# Step 4 — Eyeball the new Search record in Airtable. If it looks right,
# continue. If not, fix and rerun step 3 (idempotent: re-processing the same
# Gmail message_id is a no-op because of the Gmail Message ID field).
```

Then move to GitHub Actions:

```text
# Step 5 — Add Actions secrets at
# https://github.com/fiona-png/leads-processor/settings/secrets/actions :
#   ANTHROPIC_API_KEY
#   AIRTABLE_PAT
#   GMAIL_CLIENT_ID
#   GMAIL_CLIENT_SECRET
#   GMAIL_REFRESH_TOKEN
#   GMAIL_USER

# Step 6 — Manual Actions run. Actions tab -> "Run leads processor" ->
# "Run workflow" -> set max_leads (default 50). Confirm the job summary
# shows created/skipped/failed counts and the row(s) appear in Airtable.

# Step 7 — After 2-3 clean cron runs land correctly, disable the n8n
# workflow "Cole Leads Processor" in the n8n UI. Don't delete it — leave
# it disabled so you can compare behavior if anything looks off.
```

Rollback: if a run goes sideways, comment out the cron in `.github/workflows/run.yml` and re-enable n8n. State on Airtable is forward-only; nothing the new pipeline writes blocks the old one from running again.

## Secrets

Nothing is hardcoded. Required env vars (see `.env.example`):

- `ANTHROPIC_API_KEY`
- `AIRTABLE_PAT`
- `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `GMAIL_USER`

The `scripts/get_gmail_refresh_token.py` helper does the OAuth dance once and prints the refresh token to stdout — paste it into `.env`. The script does no file I/O on purpose so an accidental commit can't leak the token.
