import requests
import json
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from pathlib import Path

# Add parent directory to path to access shared modules
parent_dir = Path(__file__).parent.parent
import sys
sys.path.append(str(parent_dir))

from dotenv import load_dotenv
import os

try:
    import database_manager
    print(" Successfully imported database_manager for Contra module")
except ImportError:
    print("️ Warning: Could not import database_manager.")
    database_manager = None

load_dotenv()
TALLY_URL = "http://localhost:9000"
BASE_URL = "https://www.zohoapis.com/books/v3"
ORGANIZATION_ID = os.getenv("ORGANIZATION_ID")
from modules.zoho_connector import zoho
import re

def get_contra_fy(date_str):
    s = str(date_str or '').replace('-', '').strip()
    if len(s) >= 6:
        try:
            year = int(s[:4])
            month = int(s[4:6])
            start_y = year if month >= 4 else year - 1
            end_y = year + 1 if month >= 4 else year
            return f"{str(start_y)[-2:]}-{str(end_y)[-2:]}"
        except:
            pass
    return "18-19"

def format_contra_number(raw_num, date_str):
    """
    Format contra voucher number to CON/001/18-19 format.
    Extracts base numeric sequence, zero-pads to 3 digits, and computes FY.
    If already formatted like CON/001/18-19, preserves it.
    """
    s = str(raw_num or "").strip()
    if not s:
        return f"CON/001/{get_contra_fy(date_str)}"
    
    m_full = re.match(r'^CON/(\d+)/(\d{2}-\d{2})$', s, re.IGNORECASE)
    if m_full:
        n = int(m_full.group(1))
        fy = m_full.group(2)
        return f"CON/{n:03d}/{fy}"

    if '_' in s:
        s = s.split('_')[0]
        
    m = re.search(r'(\d+)', s)
    num_val = int(m.group(1)) if m else 1
    fy = get_contra_fy(date_str)
    return f"CON/{num_val:03d}/{fy}"

def _date_range(from_date_str, to_date_str):
    start = datetime.strptime(from_date_str, "%Y%m%d")
    end   = datetime.strptime(to_date_str,   "%Y%m%d")
    cur   = start
    while cur <= end:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=1)


def _fetch_day_contra(date_str, retries=2):
    import time
    xml = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Contra</VOUCHERTYPENAME>
    <SVFROMDATE>{date_str}</SVFROMDATE><SVTODATE>{date_str}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    timeouts = [45, 90]
    for attempt in range(retries):
        try:
            response = requests.post(TALLY_URL, data=xml.encode('utf-8'), timeout=timeouts[attempt])
            soup = BeautifulSoup(response.content, 'lxml-xml')
            return soup.find_all('VOUCHER')
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                print(f"  ⏱ Timeout for {date_str} (attempt {attempt+1}) — retrying in 3s...")
                time.sleep(3)
            else:
                print(f"  ⏱ Timeout for {date_str} after {retries} attempts — skipping")
                return []
        except Exception as e:
            print(f"   Error for {date_str}: {e}")
            return []
    return []


def fetch_tally_contra(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    import time
    print(f" Fetching Contra vouchers: {from_date} → {to_date} (day by day)...")
    contras = []

    for day in _date_range(from_date, to_date):
        vouchers = _fetch_day_contra(day)
        if vouchers:
            print(f"   {day}: {len(vouchers)} contra(s)")
        time.sleep(1)

        for v in vouchers:
            contra_date   = v.find('DATE').text.strip() if v.find('DATE') else day
            raw_vch_num   = v.find('VOUCHERNUMBER').text.strip() if v.find('VOUCHERNUMBER') else ""
            contra_number = format_contra_number(raw_vch_num, contra_date)
            tally_guid    = v.find('GUID').text.strip() if v.find('GUID') else ""
            narration     = v.find('NARRATION').text.strip() if v.find('NARRATION') else ""

            # Explicit party if any
            explicit_party = v.find('PARTYLEDGERNAME').text.strip() if v.find('PARTYLEDGERNAME') else ""

            ledger_entries = []
            from_account_name = "" # Credit side (negative amount)
            to_account_name = ""   # Debit side (positive amount)
            amount = 0.0

            raw_entries = v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST')
            for entry in raw_entries:
                ename = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                eamt  = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0.0
                
                ledger_entries.append({
                    "ledger_name": ename,
                    "amount": eamt
                })

                if eamt > 0:
                    # Positive amount = Debit (receiving account)
                    to_account_name = ename
                    if amount == 0:
                        amount = abs(eamt)
                elif eamt < 0:
                    # Negative amount = Credit (sending account)
                    from_account_name = ename
                    if amount == 0:
                        amount = abs(eamt)

            # ---- Cost center allocations (XML fetch) ----
            cost_center_allocations = []
            seen_cc = set()
            for entry in raw_entries:
                for ca in entry.find_all('CATEGORYALLOCATIONS.LIST'):
                    cat_name = ca.find('CATEGORY').text.strip() if ca.find('CATEGORY') else ''
                    for cc in ca.find_all('COSTCENTREALLOCATIONS.LIST'):
                        cc_name = cc.find('NAME').text.strip() if cc.find('NAME') else ''
                        try: cc_amt = float(cc.find('AMOUNT').text or 0) if cc.find('AMOUNT') else 0.0
                        except: cc_amt = 0.0
                        full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                        if full and full not in seen_cc:
                            seen_cc.add(full)
                            cost_center_allocations.append({"category": full, "amount": abs(cc_amt)})

            contras.append({
                "date": contra_date,
                "contra_number": contra_number,
                "voucher_type": "Contra",
                "from_account": from_account_name,
                "to_account": to_account_name,
                "amount": amount,
                "narration": narration,
                "ledger_entries": ledger_entries,
                "cost_center_allocations": cost_center_allocations,
                "tally_guid": tally_guid
            })

            if limit and len(contras) >= limit:
                print(f"  Limit {limit} reached. Stopping early.")
                return contras

    print(f" Fetched {len(contras)} contra vouchers from Tally")
    return contras


def parse_tally_json(json_path):
    """Parse JSON for Contra vouchers."""
    try:
        with open(json_path, 'r', encoding='utf-16') as f:
            data = json.load(f)
    except UnicodeError:
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f" Could not decode JSON: {e}")
            return []
    except Exception as e:
        print(f" Error reading JSON: {e}")
        return []

    vouchers = data.get('tallymessage', [])
    if not isinstance(vouchers, list):
        if isinstance(vouchers, dict): vouchers = [vouchers]
        else: vouchers = []

    contras = []
    seen_contra_number = set()

    for v in vouchers:
        if not isinstance(v, dict): continue
        if 'vouchernumber' not in v and 'vouchertypename' not in v: continue

        contra_date = str(v.get('date', '')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        voucher_type = str(v.get('vouchertypename', 'Contra')).strip()
        
        # Only process Contra types just in case JSON includes multiple types
        if voucher_type.lower() != 'contra':
            continue

        raw_num = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or '').strip()
        contra_number = format_contra_number(raw_num, contra_date)
        if contra_number in seen_contra_number:
            suffix = tally_guid[-4:] if tally_guid else str(len(seen_contra_number) + 1)
            contra_number = f"{contra_number}_{suffix}"
        seen_contra_number.add(contra_number)

        narration = str(v.get('narration', '')).strip()

        all_entries = v.get('allledgerentries', [])
        if not isinstance(all_entries, list):
            all_entries = [all_entries] if all_entries else []

        ledger_entries = []
        from_account_name = ""
        to_account_name = ""
        amount = 0.0
        cost_centers_dict = {}

        for entry in all_entries:
            if not isinstance(entry, dict): continue
            ename = str(entry.get('ledgername', '')).strip()
            try: eamt = float(entry.get('amount', '0'))
            except (ValueError, TypeError): eamt = 0.0

            ledger_entries.append({"ledger_name": ename, "amount": eamt})

            is_deemed_positive = entry.get('isdeemedpositive', False)
            if is_deemed_positive:
                to_account_name = ename
                if amount == 0.0: amount = abs(eamt)
            else:
                from_account_name = ename
                if amount == 0.0: amount = abs(eamt)

            # ---- Cost center allocations ----
            cats = entry.get('categoryallocations', [])
            if not isinstance(cats, list): cats = [cats] if cats else []
            for ca in cats:
                if not isinstance(ca, dict): continue
                cat_name = str(ca.get('category', '')).strip()
                ccs = ca.get('costcentreallocations', [])
                if not isinstance(ccs, list): ccs = [ccs] if ccs else []
                for cc in ccs:
                    if not isinstance(cc, dict): continue
                    cc_name = str(cc.get('name', '')).strip()
                    try: cc_amt = float(cc.get('amount', '0'))
                    except (ValueError, TypeError): cc_amt = 0.0
                    full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full:
                        cost_centers_dict[full] = abs(cc_amt)

        cost_center_allocations = [{"category": k, "amount": v} for k, v in cost_centers_dict.items()]

        contras.append({
            "date": contra_date,
            "contra_number": contra_number,
            "voucher_type": voucher_type,
            "from_account": from_account_name,
            "to_account": to_account_name,
            "amount": amount,
            "narration": narration,
            "ledger_entries": ledger_entries,
            "cost_center_allocations": cost_center_allocations,
            "tally_guid": tally_guid
        })

    print(f" Defensively parsed {len(contras)} contra vouchers from JSON")
    return contras


# Removed manual get_access_token in favor of ZohoConnector

def get_zoho_bank_accounts():
    """Retrieve Chart of Accounts that are of type Bank or Cash."""
    resp = zoho.api_call("GET", "/bankaccounts", params={"per_page": 200})
    if resp.get("code") == 0:
        accounts = resp.get("bankaccounts", [])
        account_map = {}
        for account in accounts:
            account_map[account["account_name"].strip().lower()] = account["account_id"]
        return account_map
    return {}

def find_zoho_bank_account(tally_name, bank_account_map):
    """Smart case-insensitive matching for Bank / Cash ledgers."""
    if not tally_name or not bank_account_map:
        return None
    raw = tally_name.strip().lower()

    # 1. Exact match
    if raw in bank_account_map:
        return bank_account_map[raw]

    # 2. Substring match
    for acc_name, acc_id in bank_account_map.items():
        if raw == acc_name or raw in acc_name or acc_name in raw:
            return acc_id

    # 3. Cash-specific heuristics
    if "cash" in raw:
        if "petty" in raw:
            for acc_name, acc_id in bank_account_map.items():
                if "petty" in acc_name:
                    return acc_id
        for acc_name, acc_id in bank_account_map.items():
            if acc_name in ("cash", "cash-in-hand", "cash in hand"):
                return acc_id

    # 4. Bank keywords heuristics (hdfc, icici, sbi, karnataka, axis, etc.)
    keywords = ["hdfc", "icici", "sbi", "axis", "karnataka", "canara", "kotak", "yes"]
    for kw in keywords:
        if kw in raw:
            for acc_name, acc_id in bank_account_map.items():
                if kw in acc_name:
                    return acc_id

    return None

_ZOHO_TAGS_CACHE = None

def get_zoho_reporting_tags():
    global _ZOHO_TAGS_CACHE
    if _ZOHO_TAGS_CACHE is not None:
        return _ZOHO_TAGS_CACHE
        
    resp = zoho.api_call("GET", "/settings/tags")
    tags = []
    if resp.get("code") == 0:
        for t in resp.get("reporting_tags", []):
            tag_id = t["tag_id"]
            t_resp = zoho.api_call("GET", f"/settings/tags/{tag_id}")
            if t_resp.get("code") == 0:
                options = t_resp.get("reporting_tag", {}).get("tag_options", [])
                tag_data = {
                    "tag_id": str(tag_id),
                    "tag_name": str(t.get("tag_name", "")).lower(),
                    "options": {str(o.get("tag_option_name", "")).lower(): str(o.get("tag_option_id", "")) for o in options}
                }
                tags.append(tag_data)
            
    _ZOHO_TAGS_CACHE = tags
    return tags


def create_zoho_transfer(contra_data, bank_account_map, tags_list=None):
    """
    Create a bank transfer in Zoho Books using ZohoConnector.
    Returns: (success: bool, tx_id: str or None, message: str)
    """
    from_account = str(contra_data.get("from_account", "")).strip()
    to_account = str(contra_data.get("to_account", "")).strip()
    
    from_account_id = find_zoho_bank_account(from_account, bank_account_map)
    to_account_id = find_zoho_bank_account(to_account, bank_account_map)

    if not from_account_id or not to_account_id:
        missing = []
        if not from_account_id: missing.append(f"From Account '{from_account}'")
        if not to_account_id: missing.append(f"To Account '{to_account}'")
        return False, None, f"Bank/Cash account not found in Zoho: {', '.join(missing)}"

    if from_account_id == to_account_id:
        return False, None, f"From Account and To Account map to the same Zoho account ({from_account})"

    raw_date = str(contra_data.get("date", "")).replace("-", "").strip()
    if len(raw_date) == 8:
        iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    else:
        iso_date = datetime.now().strftime("%Y-%m-%d")

    # Parse and match reporting tags
    zoho_tags = []
    if tags_list:
        tally_ccs = contra_data.get("cost_center_allocations", [])
        if isinstance(tally_ccs, str):
            try: tally_ccs = json.loads(tally_ccs)
            except: tally_ccs = []

        existing_added = set()
        for cc in tally_ccs:
            cc_full = str(cc.get("category", ""))
            parts = cc_full.split(' - ', 1)
            cat_name = parts[0].strip().lower() if len(parts) > 0 else ""
            opt_name = parts[1].strip().lower() if len(parts) > 1 else cat_name
            
            if len(parts) == 1:
                opt_name = parts[0].strip().lower()
                cat_name = ""

            for tag in tags_list:
                if tag["tag_id"] in existing_added:
                    continue
                if opt_name in tag["options"]:
                    if not cat_name or cat_name in tag["tag_name"] or tag["tag_name"] in cat_name:
                        zoho_tags.append({
                            "tag_id": tag["tag_id"],
                            "tag_option_id": tag["options"][opt_name]
                        })
                        existing_added.add(tag["tag_id"])
                        break

    contra_no = format_contra_number(contra_data.get("contra_number", ""), contra_data.get("date", ""))
    narration = str(contra_data.get("narration", "")).strip()
    desc = narration if narration else contra_no

    payload = {
        "transaction_type": "transfer_fund",
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount": round(float(contra_data.get("amount") or 0), 2),
        "date": iso_date,
        "reference_number": contra_no[:50],
        "description": desc[:500]
    }

    if zoho_tags:
        payload["from_account_tags"] = zoho_tags
        payload["to_account_tags"] = zoho_tags

    resp = zoho.api_call("POST", "/banktransactions", payload=payload)
    if resp.get("code") == 0:
        tx_id = (resp.get("banktransaction") or resp.get("transaction") or {}).get("transaction_id", "")
        return True, tx_id, resp.get("message", "Success")
    else:
        return False, None, resp.get("message", "Zoho Error")


def get_existing_zoho_transfers_map():
    """
    Fetch existing fund transfers from Zoho Books to prevent duplicate creations.
    Maps reference_number (in multiple casing and padding variants) to transaction_id.
    """
    ref_map = {}
    page = 1
    while page <= 5:
        resp = zoho.api_call("GET", "/banktransactions", params={"transaction_type": "transfer_fund", "per_page": 200, "page": page})
        if not resp or resp.get("code") != 0:
            break
        transfers = resp.get("banktransactions", [])
        if not transfers:
            break
        for t in transfers:
            tx_id = str(t.get("transaction_id") or "").strip()
            ref = str(t.get("reference_number") or "").strip()
            if ref and tx_id:
                ref_map[ref.upper()] = tx_id
                m = re.match(r'^CON/0*(\d+)/(\d{2}-\d{2})$', ref, re.IGNORECASE)
                if m:
                    unpadded = f"CON/{int(m.group(1))}/{m.group(2)}".upper()
                    padded = f"CON/{int(m.group(1)):03d}/{m.group(2)}".upper()
                    ref_map[unpadded] = tx_id
                    ref_map[padded] = tx_id
        page_context = resp.get("page_context", {})
        if not page_context.get("has_more_page"):
            break
        page += 1
    return ref_map


def sync_contra_to_zoho_job(from_date="20250401", to_date="20250430", limit=None, company_name=None, contra_numbers=None, log=None, stop_event=None):
    """
    Background job to sync Contra vouchers to Zoho Books with real-time log streaming.
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

    _emit("Loading Contra vouchers for Zoho Sync...")

    contras = []
    if contra_numbers and isinstance(contra_numbers, list):
        target_set = {str(cn).strip() for cn in contra_numbers if str(cn).strip()}
        if database_manager:
            all_c = database_manager.get_all_contra() or []
            contras = [dict(r) for r in all_c if str(dict(r).get('contra_number') or '').strip() in target_set]
        else:
            contras = []
    elif database_manager:
        all_c = database_manager.get_all_contra() or []
        for r in all_c:
            d = dict(r)
            c_date = str(d.get('date') or '').replace('-', '')
            if from_date and c_date < from_date:
                continue
            if to_date and c_date > to_date:
                continue
            contras.append(d)
    else:
        contras = fetch_tally_contra(from_date, to_date, limit, company_name)

    if limit and len(contras) > int(limit):
        contras = contras[:int(limit)]

    if not contras:
        _emit("No contra vouchers found for the selected criteria.")
        return {"status": "success", "stats": {"total": 0, "success": 0, "failed": 0}, "errors": []}

    _emit(f"Found {len(contras)} contra voucher(s) to process.")
    _emit("Loading Zoho Bank & Cash accounts...")
    bank_account_map = get_zoho_bank_accounts()
    _emit(f"Loaded {len(bank_account_map)} Zoho bank/cash accounts.")

    _emit("Checking existing transfers in Zoho Books to prevent duplicates...")
    existing_transfers_map = get_existing_zoho_transfers_map()
    _emit(f"Loaded {len(existing_transfers_map)} existing transfer references from Zoho.")

    tags_list = get_zoho_reporting_tags()

    stats = {"total": len(contras), "success": 0, "failed": 0}
    errors = []

    for idx, contra in enumerate(contras, 1):
        if _should_stop():
            _emit("Sync stopped by user.")
            return {"status": "stopped", "stats": stats, "errors": errors, "total": stats["total"], "success": stats["success"], "failed": stats["failed"]}

        contra_no = str(contra.get("contra_number") or "").strip()
        amt = float(contra.get("amount") or 0)
        raw_from = str(contra.get("from_account") or "").strip()
        raw_to = str(contra.get("to_account") or "").strip()

        # 1. Check if already synced in local DB or in Zoho
        existing_tx_id = contra.get("zoho_transfer_id")
        if not existing_tx_id:
            existing_tx_id = existing_transfers_map.get(contra_no.upper())
            if not existing_tx_id:
                m_match = re.match(r'^CON/0*(\d+)/(\d{2}-\d{2})$', contra_no, re.IGNORECASE)
                if m_match:
                    unp = f"CON/{int(m_match.group(1))}/{m_match.group(2)}".upper()
                    existing_tx_id = existing_transfers_map.get(unp)

        if existing_tx_id:
            stats["success"] += 1
            if database_manager:
                database_manager.update_contra_status(contra_no, zoho_transfer_id=str(existing_tx_id), zoho_status='synced', zoho_error=None)
            _emit(f"[{idx}/{stats['total']}] Contra #{contra_no} already synced in Zoho (ID: {existing_tx_id})")
            continue

        # 2. Create bank transfer in Zoho Books
        success, tx_id, err_or_msg = create_zoho_transfer(contra, bank_account_map, tags_list)
        if success:
            stats["success"] += 1
            if database_manager:
                database_manager.update_contra_status(contra_no, zoho_transfer_id=str(tx_id or ""), zoho_status='synced', zoho_error=None)
            existing_transfers_map[contra_no.upper()] = str(tx_id or "")
            _emit(f"[{idx}/{stats['total']}] Transfer created: #{contra_no} ({raw_from} -> {raw_to} ₹{amt:,.2f}) -> Zoho ID: {tx_id}")
        else:
            stats["failed"] += 1
            if database_manager:
                database_manager.update_contra_status(contra_no, zoho_transfer_id=None, zoho_status='failed', zoho_error=str(err_or_msg)[:500])
            errors.append({
                "contra_number": contra_no,
                "date": contra.get("date", ""),
                "from_account": raw_from,
                "to_account": raw_to,
                "amount": amt,
                "error": err_or_msg
            })
            _emit(f"[{idx}/{stats['total']}] Contra #{contra_no} failed: {err_or_msg}")

    _emit(f"Sync complete! Total: {stats['total']} | Synced: {stats['success']} | Failed: {stats['failed']}")
    return {
        "status": "success",
        "stats": stats,
        "errors": errors,
        "total": stats["total"],
        "success": stats["success"],
        "failed": stats["failed"]
    }


def sync_contra_to_zoho(selected_contras=None, from_date="20250401", to_date="20250430", limit=None, company_name=None):
    """Synchronous fallback method calling sync_contra_to_zoho_job."""
    return sync_contra_to_zoho_job(from_date=from_date, to_date=to_date, limit=limit, company_name=company_name, contra_numbers=selected_contras)


def fetch_tally_contra_job(from_date="20250401", to_date="20250430", limit=None, company_name=None, log=None, stop_event=None):
    """Background job to fetch Contra vouchers from Tally day-by-day with live streaming logs."""
    import time
    def _emit(msg):
        if callable(log):
            try: log(msg)
            except: pass
        print(msg)

    def _should_stop():
        return bool(stop_event and hasattr(stop_event, 'is_set') and stop_event.is_set())

    _emit(f"Starting Contra fetch from Tally: {from_date} -> {to_date}...")
    contras = []
    days = list(_date_range(from_date, to_date))
    total_days = len(days)

    for d_idx, day in enumerate(days, 1):
        if _should_stop():
            _emit("Import stopped by user.")
            break

        vouchers = _fetch_day_contra(day)
        if vouchers:
            _emit(f"[{d_idx}/{total_days}] {day}: found {len(vouchers)} contra(s)")

        for v in vouchers:
            contra_date = v.find('DATE').text.strip() if v.find('DATE') else day
            raw_vch_num = v.find('VOUCHERNUMBER').text.strip() if v.find('VOUCHERNUMBER') else ""
            contra_number = format_contra_number(raw_vch_num, contra_date)
            tally_guid = v.find('GUID').text.strip() if v.find('GUID') else ""
            narration = v.find('NARRATION').text.strip() if v.find('NARRATION') else ""

            ledger_entries = []
            from_account_name = ""
            to_account_name = ""
            amount = 0.0

            raw_entries = v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST')
            for entry in raw_entries:
                ename = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                try: eamt = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0.0
                except: eamt = 0.0

                ledger_entries.append({"ledger_name": ename, "amount": eamt})

                if eamt > 0:
                    to_account_name = ename
                    if amount == 0: amount = abs(eamt)
                elif eamt < 0:
                    from_account_name = ename
                    if amount == 0: amount = abs(eamt)

            cost_center_allocations = []
            seen_cc = set()
            for entry in raw_entries:
                for ca in entry.find_all('CATEGORYALLOCATIONS.LIST'):
                    cat_name = ca.find('CATEGORY').text.strip() if ca.find('CATEGORY') else ''
                    for cc in ca.find_all('COSTCENTREALLOCATIONS.LIST'):
                        cc_name = cc.find('NAME').text.strip() if cc.find('NAME') else ''
                        try: cc_amt = float(cc.find('AMOUNT').text or 0) if cc.find('AMOUNT') else 0.0
                        except: cc_amt = 0.0
                        full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                        if full and full not in seen_cc:
                            seen_cc.add(full)
                            cost_center_allocations.append({"category": full, "amount": abs(cc_amt)})

            contras.append({
                "date": contra_date,
                "contra_number": contra_number,
                "voucher_type": "Contra",
                "from_account": from_account_name,
                "to_account": to_account_name,
                "amount": amount,
                "narration": narration,
                "ledger_entries": ledger_entries,
                "cost_center_allocations": cost_center_allocations,
                "tally_guid": tally_guid
            })

            if limit and len(contras) >= int(limit):
                _emit(f"Limit {limit} reached. Stopping fetch.")
                break

        if limit and len(contras) >= int(limit):
            break
        time.sleep(0.1)

    _emit(f"Total fetched: {len(contras)} contra voucher(s). Saving to DB...")

    if database_manager and contras:
        db_data_list = []
        for contra in contras:
            db_data = {
                "contra_number": contra.get("contra_number", ""),
                "voucher_type": contra.get("voucher_type", "Contra"),
                "date": contra.get("date", ""),
                "from_account": contra.get("from_account", ""),
                "to_account": contra.get("to_account", ""),
                "amount": contra.get("amount", 0) or 0,
                "narration": contra.get("narration", ""),
                "ledger_entries": json.dumps(contra.get("ledger_entries", [])),
                "cost_center_allocations": json.dumps(contra.get("cost_center_allocations", [])),
                "tally_guid": contra.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            db_data_list.append(db_data)
        try:
            database_manager.bulk_save_contra(db_data_list)
            _emit("Saved contra vouchers to local database.")
        except Exception as e:
            _emit(f"Warning saving to DB: {e}")

    tot = sum(c.get("amount", 0) for c in contras)
    _emit(f"Import complete! {len(contras)} contras, Total: ₹{tot:,.2f}")
    return {"status": "success", "count": len(contras), "total_amount": tot, "contra_vouchers": contras}


def get_all_contra_data(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    """Synchronous fallback for fetching all contra data."""
    res = fetch_tally_contra_job(from_date, to_date, limit, company_name)
    return {"contra_vouchers": res.get("contra_vouchers", []), "total_amount": res.get("total_amount", 0)}


def generate_sync_errors_excel(errors):
    """Generate Excel spreadsheet for Contra sync errors with suggested actions."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Border, Side, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Contra Sync Errors"

    headers = [
        "Contra Number", "Date", "From Account (Source/Credit)", 
        "To Account (Destination/Debit)", "Amount", "Sync Error Message", "Action Required / Fix"
    ]
    ws.append(headers)

    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for err in errors:
        c_no = err.get("contra_number") or ""
        c_date = err.get("date") or ""
        c_from = err.get("from_account") or ""
        c_to = err.get("to_account") or ""
        c_amt = float(err.get("amount") or 0)
        c_err = str(err.get("error") or err.get("zoho_error") or "Unknown error")

        fix = "Review Zoho Bank/Cash accounts"
        low = c_err.lower()
        if "not found" in low or "account" in low:
            fix = "Create or rename Bank/Cash account in Zoho Books to match Tally ledger"
        elif "same account" in low:
            fix = "Source and destination accounts cannot be the same"
        elif "date" in low:
            fix = "Check transaction date is within open accounting period"

        row = [c_no, c_date, c_from, c_to, c_amt, c_err, fix]
        ws.append(row)

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()

