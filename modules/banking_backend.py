import os
import sys
import json
import re
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

parent_dir = Path(__file__).parent.parent
sys.path.append(str(parent_dir))

try:
    import database_manager
    print(" Successfully imported database_manager for Banking module")
except ImportError:
    database_manager = None

from modules.zoho_connector import zoho

def normalize_account_name(name):
    """Normalize account name for fuzzy/case-insensitive matching."""
    if not name:
        return ""
    # Strip extra whitespace, lower case, normalize dashes and slashes
    s = str(name).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    return s

def get_all_zoho_accounts_map():
    """
    Retrieve all accounts from Zoho:
    1. /bankaccounts (Bank, Cash, Credit Card, Payment Clearing)
    2. /chartofaccounts (Liabilities, Assets, Equity, Expenses)
    Returns: dict mapping normalized account name -> account_id, and id -> account info
    """
    account_map = {}

    # 1. Bank accounts
    resp_ba = zoho.api_call("GET", "/bankaccounts", params={"per_page": 200})
    if resp_ba and resp_ba.get("code") == 0:
        for acc in resp_ba.get("bankaccounts", []):
            name = acc.get("account_name", "")
            aid = str(acc.get("account_id", ""))
            norm = normalize_account_name(name)
            if norm and aid:
                account_map[norm] = aid
                # Also store without spaces around hyphens/slashes
                no_sep = norm.replace(" - ", "-").replace(" / ", "/")
                account_map[no_sep] = aid

    # 2. Chart of accounts
    page = 1
    while page <= 5:
        resp_coa = zoho.api_call("GET", "/chartofaccounts", params={"per_page": 1000, "page": page})
        if not resp_coa or resp_coa.get("code") != 0:
            break
        coas = resp_coa.get("chartofaccounts", [])
        if not coas:
            break
        for acc in coas:
            name = acc.get("account_name", "")
            aid = str(acc.get("account_id", ""))
            norm = normalize_account_name(name)
            if norm and aid and norm not in account_map:
                account_map[norm] = aid
                no_sep = norm.replace(" - ", "-").replace(" / ", "/")
                account_map[no_sep] = aid
        page_context = resp_coa.get("page_context", {})
        if not page_context.get("has_more_page") and len(coas) < 1000:
            break
        page += 1

    return account_map

def find_zoho_account(tally_name, account_map):
    """Smart lookup for Zoho account matching Tally ledger name."""
    if not tally_name:
        return None
    raw = normalize_account_name(tally_name)

    # 1. Exact match in account_map
    if account_map and raw in account_map:
        return account_map[raw]

    # 2. Match without spaces around separators
    no_sep = raw.replace(" - ", "-").replace(" / ", "/")
    if account_map and no_sep in account_map:
        return account_map[no_sep]

    # 3. Substring match
    if account_map:
        for acc_name, acc_id in account_map.items():
            if raw == acc_name or (len(raw) > 5 and raw in acc_name) or (len(acc_name) > 5 and acc_name in raw):
                return acc_id

    # 4. Bank keywords heuristics
    keywords = ["hdfc", "icici", "sbi", "axis", "karnataka", "canara", "kotak", "yes"]
    for kw in keywords:
        if kw in raw and account_map:
            for acc_name, acc_id in account_map.items():
                if kw in acc_name:
                    return acc_id

    # 5. Cash heuristics
    if "cash" in raw and account_map:
        if "petty" in raw:
            for acc_name, acc_id in account_map.items():
                if "petty" in acc_name:
                    return acc_id
        for acc_name, acc_id in account_map.items():
            if acc_name in ("cash", "cash-in-hand", "cash in hand"):
                return acc_id

    # 6. Fallback: Lookup local SQLite ledgers table (stores migrated zoho_contact_id / account_id)
    if database_manager:
        try:
            conn_l = database_manager.get_db_connection()
            clean_tn = str(tally_name).strip()
            row_l = conn_l.execute(
                "SELECT zoho_contact_id FROM ledgers WHERE LOWER(name) = ? OR LOWER(name) = ? OR LOWER(REPLACE(name, ' ', '')) = ?",
                (raw, clean_tn.lower(), clean_tn.lower().replace(' ', ''))
            ).fetchone()
            conn_l.close()
            if row_l and row_l["zoho_contact_id"]:
                zid = str(row_l["zoho_contact_id"]).strip()
                if zid and zid.lower() != "none":
                    if account_map is not None:
                        account_map[raw] = zid
                    return zid
        except Exception:
            pass

    # 7. Fallback: Search Zoho Books dynamically via /chartofaccounts?search_text=
    try:
        clean_search = tally_name.replace(" - ", " ").replace(" / ", " ").strip()
        resp_s = zoho.api_call("GET", "/chartofaccounts", params={"search_text": clean_search[:50]})
        if resp_s and resp_s.get("code") == 0:
            for acc in resp_s.get("chartofaccounts", []):
                aid = str(acc.get("account_id") or "")
                anm = normalize_account_name(acc.get("account_name") or "")
                if aid and anm:
                    if account_map is not None:
                        account_map[anm] = aid
                    if anm == raw or raw in anm or anm in raw:
                        return aid
    except Exception:
        pass

    return None

def get_existing_zoho_banking_map():
    """
    Fetch existing bank transactions (transfer_fund) from Zoho Books.
    Maps reference_number (in uppercase) -> transaction_id.
    """
    ref_map = {}
    page = 1
    while page <= 10:
        resp = zoho.api_call("GET", "/banktransactions", params={
            "transaction_type": "transfer_fund",
            "per_page": 200,
            "page": page
        })
        if not resp or resp.get("code") != 0:
            break
        txns = resp.get("banktransactions", [])
        if not txns:
            break
        for t in txns:
            tx_id = str(t.get("transaction_id") or "").strip()
            ref = str(t.get("reference_number") or "").strip()
            if ref and tx_id:
                ref_map[ref.upper()] = tx_id
        page_context = resp.get("page_context", {})
        if not page_context.get("has_more_page"):
            break
        page += 1
    return ref_map

def create_zoho_banking_transfer(voucher_data, account_map):
    """
    Create a bank transfer in Zoho Books matching the exact user-verified format:
    POST /banktransactions with:
      transaction_type = "transfer_fund"
      from_account_id = Bank / Paid From Account
      to_account_id = Destination Account (Credit Card / COA ledger)
      amount = amount
      date = YYYY-MM-DD
      reference_number = Payment #
      description = Narration / Payment #
    """
    bank_account_name = str(voucher_data.get("bank_account", "")).strip()
    destination_account_name = str(voucher_data.get("vendor_name", "")).strip()

    from_account_id = find_zoho_account(bank_account_name, account_map)
    to_account_id = find_zoho_account(destination_account_name, account_map)

    if not from_account_id or not to_account_id:
        missing = []
        if not from_account_id:
            missing.append(f"Paid-From Bank Account '{bank_account_name}'")
        if not to_account_id:
            missing.append(f"Destination Account '{destination_account_name}'")
        return False, None, f"Account not found in Zoho Chart of Accounts: {', '.join(missing)}"

    if from_account_id == to_account_id:
        return False, None, f"From Account and To Account map to the same Zoho account ({bank_account_name})"

    raw_date = str(voucher_data.get("date", "")).replace("-", "").strip()
    if len(raw_date) == 8:
        iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    else:
        iso_date = datetime.now().strftime("%Y-%m-%d")

    payment_no = str(voucher_data.get("payment_number", "")).strip()
    narration = str(voucher_data.get("narration", "")).strip()
    desc = narration if narration else payment_no

    payload = {
        "transaction_type": "transfer_fund",
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount": round(float(voucher_data.get("amount") or 0), 2),
        "date": iso_date,
        "reference_number": payment_no[:50],
        "description": desc[:500]
    }

    resp = zoho.api_call("POST", "/banktransactions", payload=payload)
    if resp and resp.get("code") == 0:
        tx_id = (resp.get("banktransaction") or resp.get("transaction") or {}).get("transaction_id", "")
        return True, tx_id, resp.get("message", "Success")
    else:
        err_msg = resp.get("message", "Zoho Error") if resp else "No response from Zoho"
        return False, None, err_msg

def sync_banking_to_zoho_job(from_date=None, to_date=None, limit=None, company_name=None, payment_numbers=None, log=None, stop_event=None):
    """
    Background job to sync Banking transactions to Zoho Books with real-time logging.
    """
    def _emit(msg):
        if callable(log):
            try: log(msg)
            except: pass
        print(msg)

    def _should_stop():
        return bool(stop_event and hasattr(stop_event, 'is_set') and stop_event.is_set())

    if database_manager:
        database_manager.init_db()

    _emit("Loading Banking vouchers for Zoho Sync...")

    conn = database_manager.get_db_connection()
    c = conn.cursor()

    query = "SELECT * FROM payments_made WHERE payment_category = 'banking'"
    params = []

    if payment_numbers and isinstance(payment_numbers, list) and len(payment_numbers) > 0:
        placeholders = ','.join(['?'] * len(payment_numbers))
        query += f" AND payment_number IN ({placeholders})"
        params.extend([str(pn).strip() for pn in payment_numbers])
    else:
        if from_date:
            clean_from = str(from_date).replace('-', '').strip()
            query += " AND REPLACE(date, '-', '') >= ?"
            params.append(clean_from)
        if to_date:
            clean_to = str(to_date).replace('-', '').strip()
            query += " AND REPLACE(date, '-', '') <= ?"
            params.append(clean_to)

    query += " ORDER BY date ASC"
    if limit:
        query += f" LIMIT {int(limit)}"

    c.execute(query, params)
    rows = c.fetchall()
    vouchers = [dict(r) for r in rows]
    conn.close()

    if not vouchers:
        _emit("No banking vouchers found for the selected criteria.")
        return {"status": "success", "stats": {"total": 0, "success": 0, "failed": 0}, "errors": []}

    _emit(f"Found {len(vouchers)} banking voucher(s) to process.")
    _emit("Loading Zoho Bank & Chart of Accounts...")
    account_map = get_all_zoho_accounts_map()
    _emit(f"Loaded {len(account_map)} Zoho accounts.")

    _emit("Checking existing bank transactions in Zoho Books to prevent duplicates...")
    existing_transfers_map = get_existing_zoho_banking_map()
    _emit(f"Loaded {len(existing_transfers_map)} existing bank transfer references from Zoho.")

    stats = {"total": len(vouchers), "success": 0, "failed": 0}
    errors = []

    conn_write = database_manager.get_db_connection(write=True)
    cur_write = conn_write.cursor()

    for idx, v in enumerate(vouchers, 1):
        if _should_stop():
            _emit("Sync stopped by user.")
            conn_write.close()
            return {"status": "stopped", "stats": stats, "errors": errors, "total": stats["total"], "success": stats["success"], "failed": stats["failed"]}

        pno = str(v.get("payment_number") or "").strip()
        amt = float(v.get("amount") or 0)
        from_acc = str(v.get("bank_account") or "").strip()
        to_acc = str(v.get("vendor_name") or "").strip()

        # 1. Check if already marked as synced in DB or exists in Zoho
        existing_tx_id = v.get("zoho_payment_id")
        if not existing_tx_id:
            existing_tx_id = existing_transfers_map.get(pno.upper())

        if existing_tx_id and str(existing_tx_id).lower() != "manually_synced":
            stats["success"] += 1
            cur_write.execute(
                "UPDATE payments_made SET zoho_payment_id = ?, zoho_status = 'synced', zoho_error = NULL, updated_at = ? WHERE payment_number = ?",
                (str(existing_tx_id), datetime.now().isoformat(), pno)
            )
            conn_write.commit()
            _emit(f"[{idx}/{stats['total']}] Banking #{pno} already synced in Zoho (ID: {existing_tx_id})")
            continue

        # 2. Create bank transfer in Zoho Books
        success, tx_id, err_msg = create_zoho_banking_transfer(v, account_map)
        if success:
            stats["success"] += 1
            cur_write.execute(
                "UPDATE payments_made SET zoho_payment_id = ?, zoho_status = 'synced', zoho_error = NULL, updated_at = ? WHERE payment_number = ?",
                (str(tx_id or ""), datetime.now().isoformat(), pno)
            )
            conn_write.commit()
            existing_transfers_map[pno.upper()] = str(tx_id or "")
            _emit(f"[{idx}/{stats['total']}] Transfer recorded: #{pno} ({from_acc} -> {to_acc} ₹{amt:,.2f}) -> Zoho ID: {tx_id}")
        else:
            stats["failed"] += 1
            cur_write.execute(
                "UPDATE payments_made SET zoho_status = 'failed', zoho_error = ?, updated_at = ? WHERE payment_number = ?",
                (str(err_msg)[:500], datetime.now().isoformat(), pno)
            )
            conn_write.commit()
            errors.append({
                "payment_number": pno,
                "date": v.get("date", ""),
                "from_account": from_acc,
                "to_account": to_acc,
                "amount": amt,
                "error": err_msg
            })
            _emit(f"[{idx}/{stats['total']}] Banking #{pno} failed: {err_msg}")

    conn_write.close()
    _emit(f"Sync complete! Total: {stats['total']} | Synced: {stats['success']} | Failed: {stats['failed']}")
    return {
        "status": "success",
        "stats": stats,
        "errors": errors,
        "total": stats["total"],
        "success": stats["success"],
        "failed": stats["failed"]
    }

def sync_banking_to_zoho(selected_vouchers=None, from_date=None, to_date=None, limit=None, company_name=None):
    """Synchronous fallback method calling sync_banking_to_zoho_job."""
    return sync_banking_to_zoho_job(
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        company_name=company_name,
        payment_numbers=selected_vouchers
    )
