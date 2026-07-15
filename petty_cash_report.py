"""
Zoho ERP - Petty Cash monthly activity export (journals + vendor payments +
customer payments, merged into one dated, typed list) for the dashboard.

Single self-contained script - no other project .py files needed.

Setup:
    1. pip install requests python-dotenv
    2. Fill in .env (see .env in this folder)
    3. First run: uses ZOHO_GRANT_TOKEN to get an access+refresh token and
       prints the refresh_token - copy it into ZOHO_REFRESH_TOKEN in .env
       and you won't need the grant token again (grant tokens expire in
       minutes; refresh tokens don't).
    4. python petty_cash_report.py                # current month, up to yesterday
                                                    # (full previous month if today
                                                    # is the 1st - no completed day
                                                    # exists in the new month yet)
       python petty_cash_report.py --month 2026-07 # a specific past month, in full
"""

import argparse
import calendar
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

# Real transaction notes contain characters (e.g. currency symbols, non-Latin
# scripts) that Windows' default console codepage can't encode, which crashes
# print() especially when stdout is redirected. Force UTF-8 output instead.
# line_buffering=True: when stdout isn't an interactive tty (redirected,
# piped, or run from some IDE task runners) Python block-buffers by default,
# so progress prints can sit invisible for minutes even though the script is
# working - force each line out immediately instead.
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

load_dotenv()

CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
GRANT_TOKEN = os.getenv("ZOHO_GRANT_TOKEN")
REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ORG_ID = os.getenv("ZOHO_ORGANIZATION_ID")
ACCOUNTS_URL = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
API_DOMAIN = os.getenv("ZOHO_API_DOMAIN", "https://www.zohoapis.in/erp/v3")

ACCOUNT_ID = "3545384000000056141"
ACCOUNT_NAME = "Petty Cash"
PROGRESS_EVERY = 200  # ~once per page, since per_page=200 below
DASHBOARD_PATH = "petty_cash_dashboard.html"

_DASHBOARD_DATA_BLOCK = re.compile(
    r'(<script type="application/json" id="petty-cash-data">\n).*?(\n</script>)',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Auth + low-level API helpers
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Get an access token, using refresh token if available, else grant token."""
    if REFRESH_TOKEN:
        resp = requests.post(
            f"{ACCOUNTS_URL}/oauth/v2/token",
            params={
                "refresh_token": REFRESH_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    elif GRANT_TOKEN:
        resp = requests.post(
            f"{ACCOUNTS_URL}/oauth/v2/token",
            params={
                "code": GRANT_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
    else:
        sys.exit("ERROR: set either ZOHO_REFRESH_TOKEN or ZOHO_GRANT_TOKEN in .env")

    data = resp.json()
    if "access_token" not in data:
        sys.exit(f"ERROR getting access token: {data}")

    if "refresh_token" in data:
        print(f"\n>>> SAVE THIS in .env as ZOHO_REFRESH_TOKEN:\n{data['refresh_token']}\n")

    return data["access_token"]


# Zoho ERP allows 100 requests/minute.
MIN_SECONDS_BETWEEN_CALLS = 0.65
_last_call_time = 0.0


def api_get(access_token: str, path: str, params: dict = None):
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)

    params = params or {}
    params["organization_id"] = ORG_ID

    # A run scanning hundreds of payments org-wide can take a while; a single
    # transient DNS/connection blip shouldn't throw away that whole run.
    # Retry connection-level failures a few times with backoff before giving up.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(
                f"{API_DOMAIN}{path}",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                params=params,
                timeout=30,
            )
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _last_call_time = time.monotonic()
            if attempt == max_attempts:
                raise
            wait = 2 ** attempt
            print(f"  [WARN] Network error on {path} (attempt {attempt}/{max_attempts}): {e}. Retrying in {wait}s...")
            time.sleep(wait)

    _last_call_time = time.monotonic()
    resp.raise_for_status()
    return resp.json()


def paginate(token: str, path: str, list_key: str, params: dict = None):
    """Yield every item across all pages of a Zoho v3 list endpoint."""
    base_params = dict(params or {})
    page = 1
    while True:
        query = dict(base_params)
        query["page"] = page
        query.setdefault("per_page", 200)
        data = api_get(token, path, params=query)
        for item in data.get(list_key, []):
            yield item
        if data.get("page_context", {}).get("has_more_page"):
            page += 1
        else:
            break


# ---------------------------------------------------------------------------
# CLI / period handling
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--month", metavar="YYYY-MM",
        help="Month to pull, e.g. 2026-07. Defaults to the current month.",
    )
    return parser.parse_args()


def _previous_month(year: int, month: int):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def compute_period(month_arg: str, today: date = None):
    """(start, end, output_path) for the target month.

    "Up to the previous completed day" - today is never included, since
    it isn't finished yet:
      - Current month, day > 1  -> 1st of the month through yesterday.
      - Current month, day == 1 -> no completed day exists in the new
        month yet, so fall back to the FULL previous month instead of an
        empty/inverted range. The new month only starts appearing once
        its 1st day is complete, i.e. from the 2nd onward.
      - Any other explicitly-requested month (past or future) -> the
        full calendar month, since it's either already fully in the past
        or (defensively) clamped to today below.

    `today` is injectable for testing; defaults to the real date.
    """
    today = today or date.today()
    if month_arg:
        try:
            year, month = (int(p) for p in month_arg.split("-"))
        except ValueError:
            sys.exit(f"ERROR: --month must be YYYY-MM, got '{month_arg}'")
    else:
        year, month = today.year, today.month

    is_current_month = (year, month) == (today.year, today.month)

    if is_current_month and today.day == 1:
        year, month = _previous_month(year, month)
        is_current_month = False

    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    month_end = date(year, month, last_day)

    end = (today - timedelta(days=1)) if is_current_month else month_end
    end = min(end, today)  # safety net if a future month is explicitly requested

    output_path = f"petty_cash_{year:04d}-{month:02d}.json"
    return start, end, output_path


def in_period(d: str, start_str: str, end_str: str) -> bool:
    return bool(d) and start_str <= d <= end_str


def safe_fetch(module_label: str, fetch_fn):
    """Run a fetch function; on a missing/insufficient scope (401/403), print
    a clear, specific message naming the module and return [] instead of
    crashing the whole script. Other HTTP errors are also reported clearly
    rather than left as a bare traceback.
    """
    try:
        return fetch_fn()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status in (401, 403):
            print(f"[ERROR] {module_label}: missing scope or permission (HTTP {status}) - skipping this module.")
        else:
            print(f"[ERROR] {module_label}: HTTP {status} - skipping this module.")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] {module_label}: {e} - skipping this module.")
        return []


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_summary_counts(token: str) -> dict:
    """/chartofaccounts/accounttransactions for this one account, as
    {entity_type: count}. Lifetime counts - not period-filtered, the API
    doesn't support that on this endpoint."""
    data = api_get(token, "/chartofaccounts/accounttransactions", params={"account_id": ACCOUNT_ID})
    return {
        entry.get("entity_type"): entry.get("count", 0)
        for entry in data.get("transaction_list", [])
    }


def fetch_month_journals(token: str, start_str: str, end_str: str) -> list:
    """Journals touching this account, filtered to the target month.

    /journals supports server-side account_id filtering (confirmed) and its
    list items already include journal_date, so out-of-range journals are
    dropped before ever calling /journals/{id} for line-item detail.
    """
    print("  Fetching /journals...")
    matches = []
    page = 1
    while True:
        listing = api_get(
            token, "/journals",
            params={"account_id": ACCOUNT_ID, "page": page, "per_page": 200},
        )
        for j in listing.get("journals", []):
            if in_period(j.get("journal_date", ""), start_str, end_str):
                matches.append(j)
        if listing.get("page_context", {}).get("has_more_page"):
            page += 1
        else:
            break
    print(f"  /journals: {len(matches)} in range, fetching line-item detail...")

    entries = []
    for j in matches:
        try:
            detail = api_get(token, f"/journals/{j['journal_id']}")
        except requests.exceptions.RequestException as e:
            print(f"  [WARN] Skipping journal {j.get('entry_number')}: {e}")
            continue
        journal = detail.get("journal", {})
        for li in journal.get("line_items", []):
            if li.get("account_id") != ACCOUNT_ID:
                continue
            amount = li.get("bcy_amount", li.get("amount", 0))
            entries.append({
                "type": "journal",
                "date": journal.get("journal_date"),
                "reference_number": journal.get("entry_number"),
                "description": li.get("description") or journal.get("notes") or "",
                "debit": amount if li.get("debit_or_credit") == "debit" else 0,
                "credit": amount if li.get("debit_or_credit") == "credit" else 0,
            })
    print(f"  /journals: done, {len(entries)} line item(s) for this account.")
    return entries


def fetch_month_vendor_payments(token: str, start_str: str, end_str: str) -> list:
    """Vendor payments paid FROM this account this month.

    /vendorpayments' account_id query param does NOT filter server-side
    (confirmed empirically against this org - passing account_id still
    returns payments made from other accounts, e.g. the bank account), so
    every payment org-wide is paginated and filtered client-side on
    paid_through_account_id + date. A vendor payment reduces the cash
    balance, so it posts as a credit - same convention as an expense
    journal line here.
    """
    print("  Fetching /vendorpayments (org-wide - no working server-side account filter)...")
    entries = []
    scanned = 0
    for p in paginate(token, "/vendorpayments", "vendorpayments", params={"per_page": 200}):
        scanned += 1
        if scanned % PROGRESS_EVERY == 0:
            print(f"    ...scanned {scanned} vendor payments so far, {len(entries)} matched")
        if p.get("paid_through_account_id") != ACCOUNT_ID:
            continue
        if not in_period(p.get("date", ""), start_str, end_str):
            continue
        vendor = p.get("vendor_name") or "Unknown vendor"
        bill = p.get("bill_numbers")
        desc = f"Vendor payment - {vendor}" + (f" (Bill #{bill})" if bill else "")
        amount = p.get("bcy_amount", p.get("amount", 0))
        entries.append({
            "type": "vendor_payment",
            "date": p.get("date"),
            "reference_number": p.get("payment_number"),
            "description": desc,
            "debit": 0,
            "credit": amount,
        })
    print(f"  /vendorpayments: done, scanned {scanned} org-wide, {len(entries)} matched this account+period.")
    return entries


def fetch_month_customer_payments(token: str, start_str: str, end_str: str) -> list:
    """Customer payments deposited INTO this account this month.

    /customerpayments' account_id is the deposit account, and (confirmed
    empirically, same as vendor payments) is not filtered server-side by
    the account_id query param either - other accounts (e.g. plain "Cash",
    a different account_id from Petty Cash) show up in the same unfiltered
    list, so this is filtered client-side too. A deposit increases the cash
    balance, so it posts as a debit.
    """
    print("  Fetching /customerpayments (org-wide - no working server-side account filter)...")
    entries = []
    scanned = 0
    for p in paginate(token, "/customerpayments", "customerpayments", params={"per_page": 200}):
        scanned += 1
        if scanned % PROGRESS_EVERY == 0:
            print(f"    ...scanned {scanned} customer payments so far, {len(entries)} matched")
        if p.get("account_id") != ACCOUNT_ID:
            continue
        if not in_period(p.get("date", ""), start_str, end_str):
            continue
        customer = p.get("customer_name") or "Unknown customer"
        desc = f"Customer payment - {customer}"
        amount = p.get("bcy_amount", p.get("amount", 0))
        entries.append({
            "type": "customer_payment",
            "date": p.get("date"),
            "reference_number": p.get("payment_number"),
            "description": desc,
            "debit": amount,
            "credit": 0,
        })
    print(f"  /customerpayments: done, scanned {scanned} org-wide, {len(entries)} matched this account+period.")
    return entries


def update_dashboard_html(result: dict, html_path: str = DASHBOARD_PATH) -> bool:
    """Re-embed freshly generated data into the dashboard's inline JSON
    <script> block, so petty_cash_dashboard.html always shows this run's
    data on next open - no server needed, since fetch() over file:// is
    blocked by browsers' CORS policy (confirmed empirically) and static
    double-click viewing is the whole point of this file.
    """
    if not os.path.exists(html_path):
        print(f"[WARN] {html_path} not found - skipping dashboard refresh.")
        return False

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    payload = json.dumps(result, indent=2, ensure_ascii=False)
    new_html, count = _DASHBOARD_DATA_BLOCK.subn(
        lambda m: m.group(1) + payload + m.group(2), html, count=1,
    )
    if count == 0:
        print(f"[WARN] Could not find embedded-data <script> block in {html_path} - dashboard not updated.")
        return False

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not ORG_ID:
        sys.exit("ERROR: set ZOHO_ORGANIZATION_ID in .env")

    args = parse_args()
    period_start, period_end, output_path = compute_period(args.month)
    start_str, end_str = period_start.isoformat(), period_end.isoformat()

    print(f"Period: {start_str} to {end_str}  ->  {output_path}")
    print("Authenticating...")
    token = get_access_token()

    print("Fetching account transaction summary...")
    summary_counts = safe_fetch(
        "/chartofaccounts/accounttransactions",
        lambda: fetch_summary_counts(token),
    )

    entries = []
    entries += safe_fetch("/journals", lambda: fetch_month_journals(token, start_str, end_str))
    entries += safe_fetch("/vendorpayments", lambda: fetch_month_vendor_payments(token, start_str, end_str))
    entries += safe_fetch("/customerpayments", lambda: fetch_month_customer_payments(token, start_str, end_str))
    entries.sort(key=lambda e: e["date"] or "")

    result = {
        "account_name": ACCOUNT_NAME,
        "account_id": ACCOUNT_ID,
        "period": f"{start_str} to {end_str}",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary_counts": summary_counts,
        "entries": entries,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if update_dashboard_html(result):
        print(f"Refreshed {DASHBOARD_PATH} with this run's data.")

    by_type = {}
    total_debit = total_credit = 0.0
    for e in entries:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        total_debit += e["debit"] or 0
        total_credit += e["credit"] or 0
    breakdown = ", ".join(f"{v} {k}" for k, v in by_type.items()) or "0 entries"
    print(f"Saved {len(entries)} entries ({breakdown}) to {output_path}")
    print(f"Total debit: {total_debit:,.2f} | Total credit: {total_credit:,.2f}")


if __name__ == "__main__":
    main()
