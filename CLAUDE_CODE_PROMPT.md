# Zoho ERP — Petty Cash Monthly Report

Python project that connects to the Zoho ERP REST API (v3, India data
center) using OAuth2 and exports one account's monthly activity
(Petty Cash) to JSON for the dashboard. Credentials live in `.env`
(already filled in with real IDs — do not print or log their values).

## Files

- **`petty_cash_report.py`** — the only script in the project; fully
  self-contained (no other project `.py` files to import). For the
  hardcoded Petty Cash account (`account_id = 3545384000000056141`), it:
  1. Authenticates (refresh token if present, else grant token).
  2. Fetches `/chartofaccounts/accounttransactions` for a lifetime
     entity-type count summary.
  3. Fetches `/journals` (server-side filterable by `account_id`), then
     pulls line-item detail for each journal in range.
  4. Fetches `/vendorpayments` and `/customerpayments` — both scanned
     org-wide and filtered **client-side** (`paid_through_account_id` /
     `account_id`), since neither endpoint's `account_id` query param
     actually filters server-side despite accepting it.
  5. Merges all three into one date-sorted list, each entry tagged
     `"type": "journal" | "vendor_payment" | "customer_payment"`.
  6. Writes `petty_cash.json` — a single cumulative ledger, not one file
     per month. Each run splices its fetched period back into whatever
     was already saved for every other period, so past months are kept
     but the currently-fetched period is always replaced with Zoho's
     latest state for it.
- **`petty_cash_dashboard.html`** — self-contained HTML/CSS/JS dashboard
  (no build step, no server). Reads data that's embedded directly in the
  file (not a live `fetch()` of the JSON), so refreshing it means
  re-embedding a fetch script's output into the HTML.
- **`.env`** — Zoho OAuth credentials (see Setup below).

## Setup

1. `pip install requests python-dotenv`
2. Fill in `.env`:
   - `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` — from
     [api-console.zoho.in](https://api-console.zoho.in) (Self Client)
   - `ZOHO_GRANT_TOKEN` — generate from the "Generate Code" tab, scope:
     `ERP.accountants.READ,ERP.settings.READ,ERP.vendorpayments.READ,ERP.customerpayments.READ`
     (single-use, expires in minutes — only needed for first-time setup)
   - `ZOHO_REFRESH_TOKEN` — leave blank on first run; the script will
     print one after exchanging the grant token, then paste it in here.
     Once set, the grant token is no longer needed.
   - `ZOHO_ORGANIZATION_ID` — from `GET /organizations` or Settings >
     Manage Organizations in Zoho ERP
   - `ZOHO_ACCOUNTS_URL` / `ZOHO_API_DOMAIN` — already set for the India
     data center; change only if the org is on a different DC.

## Running it

```
python petty_cash_report.py                 # current month, up to today
python petty_cash_report.py --month 2026-07  # a specific past month, in full
```

Read-only (`GET` requests only) — never creates, updates, or deletes
Zoho ERP data.

## Known-correct baseline (for verifying nothing's broken)

`python petty_cash_report.py --month 2026-07` prints a "Fetched N entries
... for 2026-07-01 to 2026-07-31" line followed by a "Saved N total entries
to petty_cash.json" line (the second N is the whole cumulative file, not
just July). The specific counts drift as real transactions get added in
Zoho, so there's no fixed number to check against — but a run that instead
errors, returns 0 entries, or fetches only a fraction of a normally-active
month is worth investigating before trusting the output.

Do not commit or display the contents of `.env` in any output, logs, or
git history.
