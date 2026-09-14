import requests
import os
import re
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bs4 import BeautifulSoup

def _date_range(from_date_str, to_date_str):
    """Yield all dates between from and to as YYYYMMDD strings."""
    start = datetime.strptime(from_date_str, "%Y%m%d")
    end   = datetime.strptime(to_date_str,   "%Y%m%d")
    cur   = start
    while cur <= end:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=1)


def _fetch_day(date_str, retries=2):
    """Fetch Payment vouchers for a single day. Retries on timeout."""
    import time
    xml = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
    <SVFROMDATE>{date_str}</SVFROMDATE><SVTODATE>{date_str}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    timeouts = [45, 90]   # giving Tally more time (some days take ~33s to compute)
    for attempt in range(retries):
        try:
            response = requests.post(TALLY_URL, data=xml.encode('utf-8'), timeout=timeouts[attempt])
            soup = BeautifulSoup(response.content, 'lxml-xml')
            return soup.find_all('VOUCHER')
        except requests.exceptions.ConnectionError as e:
            raise Exception(f"ConnectionError: Failed to connect to Tally on {TALLY_URL}. Is Tally running and configured for XML export? Error: {str(e)}")
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


def fetch_tally_payments(from_date="20250401", to_date="20250430", limit=None, company_name=None, *, log=None, stop_event=None):
    """
    Fetch Payment vouchers from Tally day by day.
    Single-day queries are used because Tally's Voucher Register times out
    on full-month Payment queries for large datasets.
    """
    import time
    def _emit(message: str):
        try:
            if callable(log):
                log(message)
            else:
                print(message)
        except Exception:
            pass

    def _should_stop() -> bool:
        try:
            return bool(stop_event and stop_event.is_set())
        except Exception:
            return False

    _emit(f"Fetching payments: {from_date} -> {to_date} (day by day)...")
    payments = []

    for day in _date_range(from_date, to_date):
        if _should_stop():
            _emit("Stopped by user.")
            break
        vouchers = _fetch_day(day)
        if vouchers:
            _emit(f"  {day}: {len(vouchers)} payment(s)")
        time.sleep(1)   # give Tally a 1s gap between requests

        for v in vouchers:
            payment_date   = v.find('DATE').text.strip()            if v.find('DATE')          else day
            payment_number = v.find('VOUCHERNUMBER').text.strip()   if v.find('VOUCHERNUMBER') else ""
            voucher_type   = v.find('VOUCHERTYPENAME').text.strip() if v.find('VOUCHERTYPENAME') else "Payment"
            tally_guid     = v.find('GUID').text.strip()            if v.find('GUID')           else ""
            narration      = v.find('NARRATION').text.strip()       if v.find('NARRATION')      else ""
            reference_number = v.find('REFERENCE').text.strip()    if v.find('REFERENCE')      else ""
            if not reference_number:
                reference_number = v.find('CHEQUENUMBER').text.strip() if v.find('CHEQUENUMBER') else ""

            # ---- Parse ledger entries ----
            ledger_entries = []
            bank_account   = ""
            payment_mode   = ""
            account_current_balance = 0.0
            rounding_amount = 0.0
            rounding_ledger = ""
            vendor_name     = ""
            vendor_ledger_amount = 0.0
            
            dr_candidates = []
            cr_candidates = []

            raw_entries = v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST')
            for entry in raw_entries:
                ename = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                eamt  = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0.0

                # Current balance
                cb_tag = entry.find('CURRENTBALANCE')
                current_balance = 0.0
                if cb_tag:
                    cb_text = cb_tag.text.strip()
                    m = re.search(r'([\d,]+\.?\d*)', cb_text)
                    if m:
                        current_balance = float(m.group(1).replace(',', ''))
                        if 'Dr' in cb_text:
                            current_balance = -current_balance

                ledger_entries.append({
                    "ledger_name": ename,
                    "amount": eamt,
                    "current_balance": current_balance
                })

                ename_lower = ename.lower()
                if 'rounding' in ename_lower:
                    rounding_amount = eamt
                    rounding_ledger = ename
                elif any(k in ename_lower for k in ['bank', 'cash', 'sbi', 'hdfc', 'icici', 'axis', 'kotak', 'idfc', 'petty']):
                    cr_candidates.append((ename, eamt, current_balance, "Cash" if 'cash' in ename_lower or 'petty' in ename_lower else "Bank Transfer"))
                else:
                    dr_candidates.append((ename, eamt))

            # Determine Vendor / Expense Ledger and Source Bank Account
            if dr_candidates:
                main_dr = dr_candidates[0]
                vendor_name = main_dr[0]
                vendor_ledger_amount = abs(main_dr[1])
            elif ledger_entries:
                # If all ledgers were bank/cash or dr was missing
                vendor_name = ledger_entries[0]["ledger_name"]
                vendor_ledger_amount = abs(ledger_entries[0]["amount"])

            if cr_candidates:
                def _cr_priority(cr):
                    name_l = cr[0].lower()
                    is_bank_cash = any(w in name_l for w in ['cash', 'petty', 'bank', 'hdfc', 'karnataka', 'credit card'])
                    return (1 if is_bank_cash else 0, abs(cr[1]))

                main_cr = max(cr_candidates, key=_cr_priority)
                bank_account = main_cr[0]
                payment_mode = main_cr[3]
                account_current_balance = main_cr[2]
            else:
                # Fallback to any positive entry or last entry
                pos_entries = [e for e in ledger_entries if e["amount"] > 0]
                if pos_entries:
                    bank_account = pos_entries[0]["ledger_name"]
                    payment_mode = "Cash" if 'cash' in bank_account.lower() or 'petty' in bank_account.lower() else "Bank Transfer"

            # ---- Bill allocations ----
            bill_allocations = []
            against_reference = ""
            for ba in v.find_all('BILLALLOCATIONS.LIST'):
                bname  = ba.find('NAME').text.strip()   if ba.find('NAME')    else ""
                bamt   = float(ba.find('AMOUNT').text or 0) if ba.find('AMOUNT') else 0.0
                btype  = ba.find('BILLTYPE').text.strip() if ba.find('BILLTYPE') else "Agst Ref"
                if not bname and btype == "On Account":
                    bname = "On Account"
                if bname:
                    if not against_reference:
                        against_reference = bname
                    bill_allocations.append({
                        "bill_number": bname,
                        "bill_type":   btype,
                        "amount":      abs(bamt) if bamt != 0 else vendor_ledger_amount
                    })

            # ---- Cost center allocations ----
            cost_center_allocations = []
            for ca in v.find_all('CATEGORYALLOCATIONS.LIST'):
                cat_name = ca.find('CATEGORY').text.strip() if ca.find('CATEGORY') else ""
                for cc in ca.find_all('COSTCENTREALLOCATIONS.LIST'):
                    cc_name = cc.find('NAME').text.strip()   if cc.find('NAME')   else ""
                    cc_amt  = float(cc.find('AMOUNT').text or 0) if cc.find('AMOUNT') else 0.0
                    full    = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full:
                        cost_center_allocations.append({"category": full, "amount": abs(cc_amt)})

            payments.append({
                "date":                     payment_date,
                "payment_number":           payment_number,
                "voucher_type":             voucher_type,
                "vendor_name":              vendor_name,
                "vendor_ledger_amount":     vendor_ledger_amount,
                "payment_mode":             payment_mode,
                "bank_account":             bank_account,
                "account_current_balance":  account_current_balance,
                "amount":                   vendor_ledger_amount,
                "reference_number":         reference_number,
                "against_reference":        against_reference,
                "narration":                narration,
                "bill_allocations":         bill_allocations,
                "ledger_entries":           ledger_entries,
                "cost_center_allocations":  cost_center_allocations,
                "rounding_amount":          rounding_amount,
                "rounding_ledger":          rounding_ledger,
                "tally_guid":               tally_guid
            })

            if limit and len(payments) >= limit:
                print(f"  Limit {limit} reached. Stopping early.")
                return payments

    _emit(f"Fetched {len(payments)} payments from Tally")
    return payments


def parse_tally_json(json_path):
    """
    Parse a JSON file exported from Tally containing Payment vouchers.
    Dynamically extracts all fields using robust, defensive methods (dict.get, type handling, loops).
    """
    import json
    try:
        with open(json_path, 'r', encoding='utf-16') as f:
            data = json.load(f)
    except UnicodeError:
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f" Could not decode JSON file: {e}")
            return []
    except Exception as e:
        print(f" Error reading JSON file: {e}")
        return []

    # Defensively get the main voucher list
    vouchers = data.get('tallymessage', [])
    if not isinstance(vouchers, list):
        if isinstance(vouchers, dict):
            vouchers = [vouchers]
        else:
            vouchers = []

    payments = []

    for v in vouchers:
        # Tally messages might randomly have nested outer tags or missing fields, so we do type checks
        if not isinstance(v, dict):
            continue
            
        # Ignore purely structural objects with no actual voucher data
        if 'vouchernumber' not in v and 'vouchertypename' not in v:
            continue
            
        # 1. Base Voucher Identifiers
        payment_date = str(v.get('date', '')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        payment_number = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or tally_guid or '').strip()
        if not payment_number:
            import hashlib
            payment_number = "AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
        if 'seen_payment_number' not in locals(): seen_payment_number = set()
        original_no = payment_number
        counter = 1
        while payment_number in seen_payment_number:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            payment_number = f"{original_no}_{suffix}"
            counter += 1
        seen_payment_number.add(payment_number)
        voucher_type = str(v.get('vouchertypename', 'Payment')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        narration = str(v.get('narration', '')).strip()

        # Many Tally versions export the main party explicitely:
        explicit_vendor_name = str(v.get('partyledgername', '')).strip()
        
        # 2. Defensively parse arrays
        all_entries = v.get('allledgerentries', [])
        if not isinstance(all_entries, list):
            all_entries = [all_entries] if all_entries else []

        # Containers for aggregated data
        reference_number = ""
        ledger_entries = []
        bank_account = ""
        payment_mode = ""
        account_current_balance = 0.0
        rounding_amount = 0.0
        rounding_ledger = ""
        vendor_name = explicit_vendor_name
        vendor_ledger_amount = 0.0
        against_reference = ""
        bill_allocations = []
        # Use dict instead of list for deduplication using full_name as key
        cost_centers_dict = {}

        dr_candidates = []
        cr_candidates = []

        # 3. Process Ledger Entries dynamically
        for entry in all_entries:
            if not isinstance(entry, dict):
                continue
                
            ename = str(entry.get('ledgername', '')).strip()
            
            # Robust float conversion
            try: eamt = float(entry.get('amount', '0'))
            except (ValueError, TypeError): eamt = 0.0

            # Usually current balance is missing in JSON exports, default to 0
            current_balance = 0.0
            ledger_entries.append({
                "ledger_name": ename,
                "amount": eamt,
                "current_balance": current_balance
            })

            ename_lower = ename.lower()
            if 'rounding' in ename_lower:
                rounding_amount = eamt
                rounding_ledger = ename
            elif eamt > 0:
                # Credit side (positive) in Payment voucher is the Paid-From Bank or Cash account
                pm = "Cash" if 'cash' in ename_lower or 'petty' in ename_lower else "Bank Transfer"
                ref = ""
                # Dynamic array check for bank allocations
                bank_allocs = entry.get('bankallocations', [])
                if not isinstance(bank_allocs, list):
                    bank_allocs = [bank_allocs] if bank_allocs else []
                    
                if bank_allocs and isinstance(bank_allocs[0], dict):
                    alloc = bank_allocs[0]
                    ref = str(alloc.get('uniquereferencenumber', '')).strip()
                    if not ref:
                        ref = str(alloc.get('instrumentnumber', '')).strip()
                cr_candidates.append((ename, eamt, current_balance, pm, ref))
            else:
                # Debit side (negative) in Payment voucher is the Expense or Vendor account
                dr_candidates.append((ename, eamt))

        # Determine Vendor / Expense Ledger and Source Bank Account
        if dr_candidates:
            main_dr = dr_candidates[0]
            vendor_name = main_dr[0]
            vendor_ledger_amount = abs(main_dr[1])
        elif ledger_entries:
            vendor_name = ledger_entries[0]["ledger_name"]
            vendor_ledger_amount = abs(ledger_entries[0]["amount"])

        if cr_candidates:
            # Prioritize genuine Bank / Cash accounts over adjustment credit ledgers (like Salary Advance)
            def _cr_priority(cr):
                name_l = cr[0].lower()
                is_bank_cash = any(w in name_l for w in ['cash', 'petty', 'bank', 'hdfc', 'karnataka', 'credit card'])
                return (1 if is_bank_cash else 0, abs(cr[1]))

            main_cr = max(cr_candidates, key=_cr_priority)
            bank_account = main_cr[0]
            account_current_balance = main_cr[2]
            payment_mode = main_cr[3]
            if main_cr[4]:
                reference_number = main_cr[4]
        else:
            pos_entries = [e for e in ledger_entries if e["amount"] > 0]
            if pos_entries:
                bank_account = pos_entries[0]["ledger_name"]
                payment_mode = "Cash" if 'cash' in bank_account.lower() or 'petty' in bank_account.lower() else "Bank Transfer"

        # 4. Bill Allocations parsing (defensive)
        for entry in all_entries:
            if not isinstance(entry, dict): continue
            bills = entry.get('billallocations', [])
            if not isinstance(bills, list):
                bills = [bills] if bills else []
                
            for ba in bills:
                if not isinstance(ba, dict): continue
                
                bname = str(ba.get('name', '')).strip()
                try: bamt = float(ba.get('amount', '0'))
                except (ValueError, TypeError): bamt = 0.0
                btype = str(ba.get('billtype', 'Agst Ref')).strip()
                
                if not bname and btype == "On Account":
                    bname = "On Account"
                
                if bname:
                    if not against_reference:
                        against_reference = bname
                    # Only append if we haven't already for this exact bill to prevent double-entry duplicates
                    exists = any(b['bill_number'] == bname for b in bill_allocations)
                    if not exists:
                        bill_allocations.append({
                            "bill_number": bname,
                            "bill_type": btype,
                            "amount": abs(bamt)
                        })
                    
            # 5. Cost center allocations parsing (defensive array/dict checks)
            cats = entry.get('categoryallocations', [])
            if not isinstance(cats, list):
                cats = [cats] if cats else []
                
            for ca in cats:
                if not isinstance(ca, dict): continue
                
                cat_name = str(ca.get('category', '')).strip()
                ccs = ca.get('costcentreallocations', [])
                if not isinstance(ccs, list):
                    ccs = [ccs] if ccs else []
                    
                for cc in ccs:
                    if not isinstance(cc, dict): continue
                    
                    cc_name = str(cc.get('name', '')).strip()
                    try: cc_amt = float(cc.get('amount', '0'))
                    except (ValueError, TypeError): cc_amt = 0.0
                    
                    full_name = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full_name:
                        # Deduplicate by using dictionary
                        cost_centers_dict[full_name] = abs(cc_amt)

        # Convert back to list
        cost_center_allocations = [{"category": k, "amount": v} for k, v in cost_centers_dict.items()]

        # 6. Final Data Assembly with Fallbacks
        payments.append({
            "date": payment_date,
            "payment_number": payment_number,
            "voucher_type": voucher_type,
            "vendor_name": vendor_name or "Unknown Vendor",
            "vendor_ledger_amount": vendor_ledger_amount,
            "payment_mode": payment_mode or "Other",
            "bank_account": bank_account,
            "account_current_balance": account_current_balance,
            "amount": vendor_ledger_amount,
            "reference_number": reference_number,
            "against_reference": against_reference,
            "narration": narration,
            "bill_allocations": bill_allocations,
            "ledger_entries": ledger_entries,
            "cost_center_allocations": cost_center_allocations,
            "rounding_amount": rounding_amount,
            "rounding_ledger": rounding_ledger,
            "tally_guid": tally_guid
        })

    print(f" Defensively parsed {len(payments)} payments from JSON")
    return payments





# Add parent directory to path to access shared cache
parent_dir = Path(__file__).parent.parent
sys.path.append(str(parent_dir))

# Import database manager
try:
    import database_manager
except ImportError:
    print("️ Warning: Could not import database_manager. SQLite sync will be skipped.")
    database_manager = None

# Load environment variables
load_dotenv()

# Zoho API credentials
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
ORGANIZATION_ID = os.getenv("ORGANIZATION_ID")

# URLs
BASE_URL = "https://www.zohoapis.in/books/v3"
TALLY_URL = "http://localhost:9000"
from modules.zoho_connector import zoho

# ----------------------------------------------------------
# ZOHO BOOKS INTEGRATION
# ----------------------------------------------------------

import difflib
from journel.journel_backend import _get_creds, get_access_token

def _clean_float(val, default=0.0):
    if not val: return float(default)
    if isinstance(val, (int, float)): return float(val)
    try:
        s = str(val).split('/')[0]
        c = re.sub(r'[^0-9.-]', '', s)
        return float(c) if c else float(default)
    except:
        return float(default)

def _clean_key(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())

def get_zoho_vendors(token=None, use_cache=True):
    """Fetch all vendors from Zoho Books using SQLite cache first to avoid heavy API usage"""
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    
    if use_cache and database_manager:
        cached = database_manager.get_zoho_master_cache("contacts", expected_org_id=org_id)
        if cached and isinstance(cached, dict):
            vendors = {}
            for k, c in cached.items():
                if isinstance(c, dict) and c.get("contact_type") == "vendor":
                    v_id = c.get("contact_id")
                    v_name = c.get("original_name") or k
                    clean_k = _clean_key(v_name)
                    info = {"vendor_id": v_id, "email": c.get("email", ""), "contact_name": v_name}
                    vendors[clean_k] = info
                    vendors[str(v_name).lower().strip()] = info
            if vendors:
                print(f" Loaded {len(vendors)} Zoho vendor contacts from DB cache")
                return vendors

    if not token:
        token = get_access_token()
    vendors = {}
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    while True:
        url = f"{creds['base_url']}/contacts"
        params = {"organization_id": creds["org_id"], "contact_type": "vendor", "page": page, "per_page": 200}
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if res.status_code == 200 and data.get("code") == 0:
                contacts_list = data.get("contacts", [])
                if not contacts_list: break
                for c in contacts_list:
                    v_name = c.get("contact_name", "")
                    clean_k = _clean_key(v_name)
                    info = {
                        "vendor_id": c["contact_id"],
                        "email": c.get("email", ""),
                        "contact_name": v_name
                    }
                    vendors[clean_k] = info
                    vendors[v_name.lower().strip()] = info
                page_context = data.get("page_context", {})
                if not page_context.get("has_more_page", False): break
                page += 1
            else:
                break
        except Exception:
            break
    print(f" Fetched {len(vendors)} Zoho vendor contacts across {page} page(s)")
    return vendors

def get_zoho_bills(vendor_id=None, token=None):
    """Fetch bills from Zoho Books for a specific vendor"""
    creds = _get_creds()
    if not token:
        token = get_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    if vendor_id:
        params["vendor_id"] = vendor_id
        
    try:
        res = requests.get(f"{creds['base_url']}/bills", headers=headers, params=params)
        data = res.json()
        if res.status_code == 200 and data.get("code") == 0:
            bills = data.get("bills", [])
            bill_map = {}
            for bill in bills:
                bill_map[bill["bill_number"]] = {
                    "bill_id": bill["bill_id"],
                    "balance": float(bill.get("balance", 0)),
                    "total": float(bill.get("total", 0))
                }
            return bill_map
    except Exception:
        pass
    return {}

def get_zoho_bank_accounts(token=None, use_cache=True):
    """Fetch bank and cash accounts from SQLite cache first, fallback to Zoho API"""
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    
    if use_cache and database_manager:
        cached = database_manager.get_zoho_master_cache("bank_accounts", expected_org_id=org_id)
        if cached and isinstance(cached, dict):
            accounts = {}
            for k, acc_id in cached.items():
                clean_k = _clean_key(k)
                accounts[clean_k] = acc_id
                accounts[str(k).lower().strip()] = acc_id
            if accounts:
                print(f" Loaded {len(accounts)} Zoho bank/cash accounts from DB cache")
                return accounts

    if not token:
        token = get_access_token()
    accounts = {}
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    while True:
        url = f"{creds['base_url']}/bankaccounts"
        params = {"organization_id": creds["org_id"], "page": page, "per_page": 200}
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if res.status_code == 200 and data.get("code") == 0:
                acc_list = data.get("bankaccounts", [])
                if not acc_list: break
                for acc in acc_list:
                    acc_name = acc.get("account_name", "")
                    clean_k = _clean_key(acc_name)
                    acc_id = acc.get("account_id")
                    accounts[clean_k] = acc_id
                    accounts[acc_name.lower().strip()] = acc_id
                page_context = data.get("page_context", {})
                if not page_context.get("has_more_page", False): break
                page += 1
            else:
                break
        except Exception:
            break
    print(f" Fetched {len(accounts)} Zoho bank/cash accounts across {page} page(s)")
    return accounts

def get_zoho_chart_of_accounts(token=None, use_cache=True):
    """Fetch Chart of Accounts from SQLite cache first, fallback to Zoho API"""
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    
    if use_cache and database_manager:
        cached = database_manager.get_zoho_master_cache("accounts", expected_org_id=org_id)
        if cached and isinstance(cached, dict):
            coa_map = {}
            for k, acc_id in cached.items():
                clean_k = _clean_key(k)
                coa_map[clean_k] = acc_id
                coa_map[str(k).lower().strip()] = acc_id
            if coa_map:
                print(f" Loaded {len(coa_map)} Zoho chart of accounts from DB cache")
                return coa_map

    if not token:
        token = get_access_token()
    coa_map = {}
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    while True:
        url = f"{creds['base_url']}/chartofaccounts"
        params = {"organization_id": creds["org_id"], "page": page, "per_page": 200}
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if res.status_code == 200 and data.get("code") == 0:
                acc_list = data.get("chartofaccounts", [])
                if not acc_list: break
                for acc in acc_list:
                    name = acc.get("account_name", "")
                    clean_k = _clean_key(name)
                    acc_id = acc.get("account_id", "")
                    if name and acc_id:
                        coa_map[clean_k] = acc_id
                        coa_map[name.lower().strip()] = acc_id
                page_context = data.get("page_context", {})
                if not page_context.get("has_more_page", False): break
                page += 1
            else:
                break
        except Exception:
            break
    print(f" Fetched {len(coa_map)} Zoho chart of accounts across {page} page(s)")
    return coa_map


def create_zoho_payment_made(payment_data, vendor_map, bill_map, bank_account_map, token=None):
    """Create a payment made (vendor payment) in Zoho Books dynamically"""
    creds = _get_creds()
    if not token:
        token = get_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}

    vendor_raw = str(payment_data.get("vendor_name") or "").strip()
    vendor_clean = vendor_raw.lower().strip()
    vendor_key = _clean_key(vendor_raw)

    vendor_info = vendor_map.get(vendor_key) or vendor_map.get(vendor_clean) if vendor_map else None
    if not vendor_info and vendor_map:
        matches = difflib.get_close_matches(vendor_clean, list(vendor_map.keys()), n=1, cutoff=0.70)
        if matches:
            vendor_info = vendor_map[matches[0]]

    if not vendor_info:
        return False, f"Vendor '{vendor_raw}' not found in Zoho Books"

    vendor_id = vendor_info["vendor_id"]
    
    # Resolve Paid-Through Bank/Cash Account (paid_through_account_id)
    bank_account_name = str(payment_data.get("bank_account") or "").strip()
    account_id = None

    def _match_bank_id(name):
        if not name or not bank_account_map: return None
        n_clean = name.lower().strip()
        n_key = _clean_key(name)
        acc_id = bank_account_map.get(n_key) or bank_account_map.get(n_clean)
        if not acc_id and n_clean:
            matches = difflib.get_close_matches(n_clean, list(bank_account_map.keys()), n=1, cutoff=0.75)
            if matches:
                acc_id = bank_account_map[matches[0]]
        return acc_id

    account_id = _match_bank_id(bank_account_name)

    # If not matched directly, check ledger_entries for genuine bank/cash
    ledger_entries = payment_data.get("ledger_entries") or []
    if isinstance(ledger_entries, str):
        try: ledger_entries = json.loads(ledger_entries)
        except: ledger_entries = []

    if not account_id and isinstance(ledger_entries, list):
        for le in ledger_entries:
            if not isinstance(le, dict): continue
            lname = str(le.get("ledger_name") or "").strip()
            e_amt = float(le.get("amount") or 0.0)
            if e_amt > 0 and 'round' not in lname.lower():
                acc_id = _match_bank_id(lname)
                if acc_id:
                    account_id = acc_id
                    bank_account_name = lname
                    break

    if not account_id:
        return False, f"Bank/Cash account '{bank_account_name}' not found in Zoho Books"
            
    date_str = str(payment_data.get("date", "")).replace("-", "").strip()
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else datetime.now().strftime("%Y-%m-%d")
        
    p_num = str(payment_data.get("payment_number") or "").strip()
    ref_no = p_num if p_num else str(payment_data.get("reference_number") or "").strip()
    if not ref_no:
        alloc_bills = [str(a.get("bill_number") or a.get("reference") or "").strip() for a in payment_data.get("bill_allocations", []) if a.get("bill_number") or a.get("reference")]
        if alloc_bills:
            ref_no = f"AGST-{'/'.join(dict.fromkeys(alloc_bills))}"[:100]

    pm_raw = str(payment_data.get("payment_mode") or "Bank Transfer").strip()
    pm_zoho = "Cash" if 'cash' in pm_raw.lower() or 'petty' in pm_raw.lower() else "Bank Transfer"

    payload = {
        "vendor_id": vendor_id,
        "payment_mode": pm_zoho,
        "amount": _clean_float(payment_data.get("amount"), 0.0),
        "date": formatted_date,
        "reference_number": ref_no,
        "description": str(payment_data.get("narration") or "").strip(),
        "paid_through_account_id": account_id,
        "account_id": account_id,
        "bills": []
    }
    
    # Check bill allocations
    allocations = payment_data.get("bill_allocations") or []
    if isinstance(allocations, str):
        try: allocations = json.loads(allocations)
        except: allocations = []

    if bill_map and isinstance(allocations, list):
        for allocation in allocations:
            if not isinstance(allocation, dict): continue
            bill_number = str(allocation.get("bill_number") or allocation.get("reference") or "").strip()
            if not bill_number or bill_number.lower() == "on account": continue
            
            # Match bill in bill_map
            matched_bill = bill_map.get(bill_number) or bill_map.get(bill_number.lower())
            if not matched_bill:
                b_key = _clean_key(bill_number)
                matched_bill = bill_map.get(b_key)

            if matched_bill:
                payload["bills"].append({
                    "bill_id": matched_bill["bill_id"],
                    "amount_applied": _clean_float(allocation.get("amount"), 0.0)
                })
            
    try:
        url = f"{creds['base_url']}/vendorpayments"
        res = requests.post(url, headers=headers, params=params, json=payload)
        data = res.json()
        if data.get("code") == 0:
            return True, None
        return False, data.get("message", "Error creating vendor payment")
    except Exception as e:
        return False, f"Connection error: {e}"


def create_zoho_expense(payment_data, vendor_map, bank_account_map, coa_map=None, token=None):
    """Create a standalone expense in Zoho Books dynamically using Chart of Accounts"""
    if not payment_data or not isinstance(payment_data, dict):
        return False, "Invalid expense data format"

    creds = _get_creds()
    if not token:
        token = get_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}

    raw_vendor = str(payment_data.get("vendor_name") or payment_data.get("from_account") or "").strip()
    bank_account_name = str(payment_data.get("bank_account") or payment_data.get("to_account") or "").strip()

    # Parse ledger_entries if present
    ledger_entries = payment_data.get("ledger_entries") or []
    if isinstance(ledger_entries, str):
        try: ledger_entries = json.loads(ledger_entries)
        except: ledger_entries = []
    if not isinstance(ledger_entries, list): ledger_entries = []

    known_bank_cash = set([k.lower() for k in bank_account_map.keys()]) if bank_account_map else set()
    known_bank_cash.update(['cash', 'petty cash', 'cash account', 'cash in hand', 'cash-in-hand', 'bank', 'bank account', 'bank accounts'])
    if bank_account_name: known_bank_cash.add(bank_account_name.lower())

    # Detect genuine Paid-Through Bank/Cash account:
    # Check bank_account field first, then scan ledger_entries for recognized bank/cash
    paid_through_id = None
    resolved_bank_name = bank_account_name

    def _match_bank_id(name):
        if not name or not bank_account_map: return None
        n_clean = name.lower().strip()
        n_key = _clean_key(name)
        acc_id = bank_account_map.get(n_key) or bank_account_map.get(n_clean)
        if not acc_id and n_clean:
            matches = difflib.get_close_matches(n_clean, list(bank_account_map.keys()), n=1, cutoff=0.75)
            if matches:
                acc_id = bank_account_map[matches[0]]
        return acc_id

    paid_through_id = _match_bank_id(bank_account_name)

    # If not matched directly, inspect credit entries inside ledger_entries
    if not paid_through_id and ledger_entries:
        for le in ledger_entries:
            if not isinstance(le, dict): continue
            lname = str(le.get("ledger_name") or "").strip()
            e_amt = float(le.get("amount") or 0.0)
            if e_amt > 0 and 'round' not in lname.lower(): # Credit side
                acc_id = _match_bank_id(lname)
                if acc_id:
                    paid_through_id = acc_id
                    resolved_bank_name = lname
                    break

    if not paid_through_id:
        return False, f"Bank/Cash account '{bank_account_name}' not found in Zoho Books"

    bank_clean = resolved_bank_name.lower().strip()
    bank_key = _clean_key(resolved_bank_name)

    target_items = []
    for le in ledger_entries:
        if not isinstance(le, dict): continue
        lname = str(le.get("ledger_name") or "").strip()
        e_amt = float(le.get("amount") or 0.0)
        if 'rounding' in lname.lower():
            continue
        
        l_clean = lname.lower().strip()
        l_key = _clean_key(lname)
        # Skip the primary paid_through bank/cash ledger
        if l_clean == bank_clean or l_key == bank_key:
            continue

        target_items.append((lname, abs(e_amt)))

    date_str = str(payment_data.get("date", "")).replace("-", "").strip()
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else datetime.now().strftime("%Y-%m-%d")
    
    # Use formatted payment number (e.g. PY/708/18-19) as the clean reference_number
    p_num = str(payment_data.get("payment_number") or "").strip()
    ref_no = p_num if p_num else str(payment_data.get("reference_number") or "").strip()
    narration = str(payment_data.get("narration") or "").strip()

    # Case A: Multi-ledger expense (Itemized line_items — matches reference 3615610000000325975)
    if len(target_items) > 1:
        line_items = []
        total_expense_amount = 0.0
        missing_ledgers = []

        for lname, lamt in target_items:
            l_clean = lname.lower().strip()
            l_key = _clean_key(lname)
            acc_id = None
            if coa_map:
                acc_id = coa_map.get(l_key) or coa_map.get(l_clean)
                if not acc_id:
                    matches = difflib.get_close_matches(l_clean, list(coa_map.keys()), n=1, cutoff=0.60)
                    if matches:
                        acc_id = coa_map[matches[0]]
            
            if not acc_id:
                missing_ledgers.append(lname)
            else:
                line_items.append({
                    "account_id": acc_id,
                    "amount": lamt,
                    "description": narration
                })
                total_expense_amount += lamt

        if missing_ledgers:
            return False, f"Expense account(s) not found in Zoho Chart of Accounts: {', '.join(missing_ledgers)}"

        payload = {
            "paid_through_account_id": paid_through_id,
            "amount": total_expense_amount,
            "date": formatted_date,
            "reference_number": ref_no,
            "description": narration,
            "line_items": line_items
        }

    # Case B: Single ledger expense
    else:
        target_name = target_items[0][0] if target_items else raw_vendor
        target_amt = target_items[0][1] if target_items else _clean_float(payment_data.get("amount"), 0.0)

        target_clean = target_name.lower().strip()
        target_key = _clean_key(target_name)
        account_id = None
        if coa_map:
            account_id = coa_map.get(target_key) or coa_map.get(target_clean)
            if not account_id and target_clean:
                matches = difflib.get_close_matches(target_clean, list(coa_map.keys()), n=1, cutoff=0.60)
                if matches:
                    account_id = coa_map[matches[0]]

        if not account_id:
            return False, f"Expense account '{target_name}' not found in Zoho Chart of Accounts"

        payload = {
            "account_id": account_id,
            "paid_through_account_id": paid_through_id,
            "amount": target_amt,
            "date": formatted_date,
            "reference_number": ref_no,
            "description": narration
        }
    
    try:
        url = f"{creds['base_url']}/expenses"
        res = requests.post(url, headers=headers, params=params, json=payload)
        data = res.json()
        if data.get("code") == 0:
            return True, None
        return False, data.get("message", "Error creating expense")
    except Exception as e:
        return False, f"Connection error: {e}"


def sync_payments_to_zoho(selected_payments=None, from_date="20250401", to_date="20250430", limit=None, company_name=None):
    if selected_payments is None:
        payments = fetch_tally_payments(from_date, to_date, limit, company_name)
    else:
        payments = selected_payments
        
    if not payments:
        return {"status": "error", "message": "No payments to sync"}
        
    print(" Fetching Zoho Books data dynamically...")
    token = get_access_token()
    vendor_map = get_zoho_vendors(token)
    bank_account_map = get_zoho_bank_accounts(token)
    coa_map = get_zoho_chart_of_accounts(token)
    
    results = {"total": len(payments), "success": 0, "failed": 0, "errors": [], "synced_items": []}
    banking_account_map = None
     
    for payment in payments:
        raw_v = str(payment.get("vendor_name") or "").strip()
        v_clean = raw_v.lower().strip()
        v_key = _clean_key(raw_v)
        
        vendor_info = vendor_map.get(v_key) or vendor_map.get(v_clean) if vendor_map else None
        if not vendor_info and vendor_map:
            matches = difflib.get_close_matches(v_clean, list(vendor_map.keys()), n=1, cutoff=0.70)
            if matches:
                vendor_info = vendor_map[matches[0]]

        cat = str(payment.get("payment_category") or payment.get("voucher_type") or "").lower().strip()
        created_tx_id = "SYNCED"

        # 1. Non-expense Account-to-Account Transfer (Banking: Credit Cards, Duties, Provisions, Capital, Loans)
        if cat == "banking":
            try:
                from modules.banking_backend import get_all_zoho_accounts_map, create_zoho_banking_transfer
                if banking_account_map is None:
                    banking_account_map = get_all_zoho_accounts_map()
                ok_bank, tx_id, err_bank = create_zoho_banking_transfer(payment, banking_account_map)
                success = ok_bank
                error = None if ok_bank else err_bank
                if tx_id:
                    created_tx_id = str(tx_id)
            except Exception as be:
                success = False
                error = f"Banking transfer error: {be}"
        # 2. Check if vendor_name exists in vendor_map
        elif vendor_info:
            vendor_id = vendor_info["vendor_id"]
            bill_map = get_zoho_bills(vendor_id, token)
            success, error = create_zoho_payment_made(payment, vendor_map, bill_map, bank_account_map, token)
        else:
            success, error = create_zoho_expense(payment, vendor_map, bank_account_map, coa_map, token)
            
        if success:
            results["success"] += 1
            results["synced_items"].append({
                "payment_number": payment.get("payment_number", ""),
                "vendor_name": payment.get("vendor_name", ""),
                "date": payment.get("date", ""),
                "amount": payment.get("amount", 0)
            })
            if database_manager:
                try:
                    database_manager.update_payment_made_status(
                        payment.get("payment_number"),
                        payment.get("date"),
                        zoho_payment_id=created_tx_id,
                        zoho_status="synced",
                        zoho_error=""
                    )
                except Exception:
                    pass
        else:
            results["failed"] += 1
            results["errors"].append({
                "payment_number": payment.get("payment_number", ""),
                "vendor": payment.get("vendor_name", ""),
                "date": payment.get("date", ""),
                "amount": payment.get("amount", 0),
                "error": error
            })
            if database_manager:
                try:
                    database_manager.update_payment_made_status(
                        payment.get("payment_number"),
                        payment.get("date"),
                        zoho_payment_id=None,
                        zoho_status="failed",
                        zoho_error=str(error or "Sync failed")
                    )
                except Exception:
                    pass
            
    results["status"] = "success"
    results["message"] = f"Synced {results['success']} out of {results['total']} payments/expenses"
    return results

def get_all_payments_data(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    if database_manager:
        database_manager.init_db()
        
    payments = fetch_tally_payments(from_date, to_date, limit, company_name)
    
    if database_manager and payments:
        db_data_list = []
        for payment in payments:
            db_data = {
                "payment_number": payment.get("payment_number", ""),
                "voucher_type": payment.get("voucher_type", ""),
                "date": payment.get("date", ""),
                "vendor_name": payment.get("vendor_name", ""),
                "vendor_ledger_amount": payment.get("vendor_ledger_amount", 0) or 0,
                "payment_mode": payment.get("payment_mode", ""),
                "bank_account": payment.get("bank_account", ""),
                "account_current_balance": payment.get("account_current_balance", 0) or 0,
                "amount": payment.get("amount", 0) or 0,
                "reference_number": payment.get("reference_number", ""),
                "against_reference": payment.get("against_reference", ""),
                "payment_category": payment.get("payment_category", ""),
                "narration": payment.get("narration", ""),
                "bill_allocations": json.dumps(payment.get("bill_allocations", [])),
                "ledger_entries": json.dumps(payment.get("ledger_entries", [])),
                "cost_center_allocations": json.dumps(payment.get("cost_center_allocations", [])),
                "rounding_amount": payment.get("rounding_amount", 0) or 0,
                "rounding_ledger": payment.get("rounding_ledger", ""),
                "tally_guid": payment.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            db_data_list.append(db_data)
            
        try:
            database_manager.bulk_save_payments_made(db_data_list)
        except AttributeError:
            # We'll need to define this function in database_manager.py
            print("Warning: database_manager.bulk_save_payments_made not found yet. Needs to be created.")
            pass
            
    total_amount = sum(p.get("amount", 0) for p in payments)
    return {"payments": payments, "total_amount": total_amount}


def get_all_payments_data_day_by_day(from_date="20250401", to_date="20250430", limit=None, company_name=None, *, log=None, stop_event=None):
    def _emit(message: str):
        try:
            if callable(log):
                log(message)
            else:
                print(message)
        except Exception:
            pass

    if database_manager:
        try:
            database_manager.init_db()
        except Exception:
            pass

    payments = fetch_tally_payments(from_date, to_date, limit, company_name, log=log, stop_event=stop_event)

    if database_manager and payments:
        db_data_list = []
        for payment in payments:
            db_data_list.append({
                "payment_number": payment.get("payment_number", ""),
                "voucher_type": payment.get("voucher_type", ""),
                "date": payment.get("date", ""),
                "vendor_name": payment.get("vendor_name", ""),
                "vendor_ledger_amount": payment.get("vendor_ledger_amount", 0) or 0,
                "payment_mode": payment.get("payment_mode", ""),
                "bank_account": payment.get("bank_account", ""),
                "account_current_balance": payment.get("account_current_balance", 0) or 0,
                "amount": payment.get("amount", 0) or 0,
                "reference_number": payment.get("reference_number", ""),
                "against_reference": payment.get("against_reference", ""),
                "payment_category": payment.get("payment_category", ""),
                "narration": payment.get("narration", ""),
                "bill_allocations": json.dumps(payment.get("bill_allocations", []), ensure_ascii=False),
                "ledger_entries": json.dumps(payment.get("ledger_entries", []), ensure_ascii=False),
                "cost_center_allocations": json.dumps(payment.get("cost_center_allocations", []), ensure_ascii=False),
                "rounding_amount": payment.get("rounding_amount", 0) or 0,
                "rounding_ledger": payment.get("rounding_ledger", ""),
                "tally_guid": payment.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            })

        try:
            database_manager.bulk_save_payments_made(db_data_list)
            _emit(f"Saved {len(db_data_list)} payments to SQLite")
        except Exception as e:
            _emit(f"Failed saving to SQLite: {e}")

    total_amount = sum(float(p.get("amount", 0) or 0) for p in payments)
    return {"status": "success", "payments": payments, "total_amount": total_amount, "count": len(payments)}

if __name__ == "__main__":
    import json
    data = get_all_payments_data(limit=5)
    print(json.dumps(data, indent=2))

# ----------------------------------------------------------
# ZOHO SYNC (JOB MODE) — PAYMENTS ROUTING LOGIC
# ----------------------------------------------------------

def sync_payments_to_zoho_job(from_date="20250401", to_date="20250430", limit=None, company_name=None, *, cutoff_date="2025-03-31", opening_bill_id="", log=None, stop_event=None):
    """
    Sync Payments Made to Zoho Books using allocation-level routing rules:
    - Advance/On Account (Dr) -> Vendor Advance
    - Agst Ref (Dr) -> Vendor Payment applied to bills (with prev/current year accumulation)
    - New Ref (Dr) -> if ref in Purchase_Bill_Master -> Bill Payment else Vendor Advance
    """
    def _emit(msg: str):
        try:
            if callable(log):
                log(msg)
            else:
                print(msg)
        except Exception:
            pass

    try:
        from modules.zoho_connector import zoho
    except Exception as e:
        return {"status": "error", "message": f"Zoho connector not available: {e}"}

    if not database_manager:
        return {"status": "error", "message": "database_manager not available"}

    try:
        database_manager.init_db()
    except Exception:
        pass

    # Build Purchase_Bill_Master from local bills table
    purchase_bill_master = set()
    try:
        for bill in (database_manager.get_all_bills() or []):
            no = (bill.get("bill_number") or "").strip()
            if no:
                purchase_bill_master.add(no)
    except Exception:
        pass

    def _tally_to_iso(d: str) -> str:
        try:
            return datetime.strptime(str(d or ""), "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            return ""

    def _parse_cutoff(cut: str):
        try:
            return datetime.strptime((cut or "").strip(), "%Y-%m-%d").date()
        except Exception:
            return None

    cutoff_dt = _parse_cutoff(cutoff_date)

    def _safe_float(x) -> float:
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    def _norm(s: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    # Cache: vendors by normalized name
    vendor_cache = {}

    def _load_vendors():
        page = 1
        while True:
            resp = zoho.api_call("GET", "/contacts", params={"page": page, "per_page": 200, "contact_type": "vendor"})
            if resp.get("code") != 0:
                break
            items = resp.get("contacts", []) or []
            for v in items:
                nm = (v.get("contact_name") or "").strip()
                vid = (v.get("contact_id") or "").strip()
                if nm and vid:
                    vendor_cache[_norm(nm)] = vid
            has_more = resp.get("page_context", {}).get("has_more_page", False)
            if not has_more:
                break
            page += 1

    def _get_vendor_id(vendor_name: str) -> str:
        if not vendor_cache:
            _emit("Loading Zoho vendors...")
            _load_vendors()
            _emit(f"Loaded {len(vendor_cache)} vendors")
        return vendor_cache.get(_norm(vendor_name), "")

    # Cache: bank accounts
    bank_account_cache = {}

    def _load_bank_accounts():
        resp = zoho.api_call("GET", "/bankaccounts")
        if resp.get("code") == 0:
            accounts = resp.get("bankaccounts", []) or []
            for acc in accounts:
                nm = (acc.get("account_name") or "").strip()
                aid = (acc.get("account_id") or "").strip()
                if nm and aid:
                    bank_account_cache[_norm(nm)] = aid

    def _get_account_id(bank_name: str) -> str:
        if not bank_account_cache:
            _emit("Loading Zoho bank accounts...")
            _load_bank_accounts()
            _emit(f"Loaded {len(bank_account_cache)} bank accounts")
        if not bank_name:
            return list(bank_account_cache.values())[0] if bank_account_cache else ""
        norm_bank = _norm(bank_name)
        if norm_bank in bank_account_cache:
            return bank_account_cache[norm_bank]
        for nm, aid in bank_account_cache.items():
            if norm_bank in nm or nm in norm_bank:
                return aid
        return list(bank_account_cache.values())[0] if bank_account_cache else ""

    # Cache: bill_number -> zoho bill_id
    bill_id_cache = {}

    def _resolve_bill_id(bill_number: str) -> str:
        key = (bill_number or "").strip()
        if not key:
            return ""
        if key in bill_id_cache:
            return bill_id_cache[key]
        resp = zoho.api_call("GET", "/bills", params={"bill_number": key, "per_page": 200})
        if resp.get("code") == 0:
            for bill in resp.get("bills", []) or []:
                if (bill.get("bill_number") or "").strip() == key:
                    bid = (bill.get("bill_id") or "").strip()
                    bill_id_cache[key] = bid
                    return bid
        bill_id_cache[key] = ""
        return ""

    def _should_stop(event) -> bool:
        if event and hasattr(event, "is_set"):
            return event.is_set()
        return False

    payments = database_manager.get_all_payments_made() or []
    payments = [p for p in payments if (p.get("date") or "") >= from_date and (p.get("date") or "") <= to_date]
    if limit:
        try:
            payments = payments[: int(limit)]
        except Exception:
            pass

    if not payments:
        return {"status": "error", "message": "No payments found in DB for the selected date range"}

    stats = {"total": len(payments), "payments_created": 0, "advances_created": 0, "skipped": 0, "failed": 0}
    errors = []

    import json

    for idx, p in enumerate(payments, 1):
        if _should_stop(stop_event):
            _emit("Stopped by user.")
            return {"status": "stopped", "stats": stats, "errors": errors}

        payment_no = p.get("payment_number") or ""
        payment_date_iso = _tally_to_iso(p.get("date") or "")
        vendor_name = (p.get("vendor_name") or "").strip()

        cat = str(p.get("payment_category") or p.get("voucher_type") or "").lower().strip()
        if cat == "banking":
            try:
                from modules.banking_backend import get_all_zoho_accounts_map, create_zoho_banking_transfer
                if 'banking_job_account_map' not in locals() or banking_job_account_map is None:
                    banking_job_account_map = get_all_zoho_accounts_map()
                ok_bank, tx_id, err_bank = create_zoho_banking_transfer(p, banking_job_account_map)
                if ok_bank:
                    stats["payments_created"] += 1
                    if database_manager:
                        database_manager.update_payment_made_status(payment_no, p.get("date"), zoho_payment_id=str(tx_id or "SYNCED"), zoho_status="synced", zoho_error="")
                    _emit(f"[{idx}/{stats['total']}] Bank Transfer #{payment_no} synced -> Zoho ID: {tx_id}")
                else:
                    stats["failed"] += 1
                    errors.append({"payment_number": payment_no, "error": err_bank})
                    _emit(f"[{idx}/{stats['total']}] Bank Transfer #{payment_no} failed: {err_bank}")
            except Exception as be:
                stats["failed"] += 1
                errors.append({"payment_number": payment_no, "error": str(be)})
                _emit(f"[{idx}/{stats['total']}] Bank Transfer #{payment_no} error: {be}")
            continue

        vendor_id = _get_vendor_id(vendor_name)
        if not vendor_id:
            stats["failed"] += 1
            errors.append({"payment_number": payment_no, "error": f"Vendor not found in Zoho: {vendor_name}"})
            _emit(f"[{idx}/{stats['total']}] Vendor not found: {vendor_name} (payment {payment_no})")
            continue

        try:
            allocs = p.get("bill_allocations") or "[]"
            if isinstance(allocs, str):
                allocs = json.loads(allocs) if allocs.strip() else []
        except Exception:
            allocs = []

        if not isinstance(allocs, list):
            allocs = []

        prev_sum = 0.0
        curr_sum = 0.0
        bill_lines = []
        advance_lines = []

        # ================================================================
        # PHASE 1: ROUTING — based on Logic - Payment Made.txt blueprint
        # ================================================================
        for a in allocs:
            btype = str((a or {}).get("bill_type") or (a or {}).get("billtype") or "").strip() or "Agst Ref"
            ref = str((a or {}).get("bill_number") or (a or {}).get("name") or "").strip()
            amount = _safe_float((a or {}).get("amount"))
            drcr = str((a or {}).get("dr_cr") or (a or {}).get("drcr") or "").strip() or "Dr"

            if not ref:
                continue

            btype_norm = btype.lower()

            # --- Advance / On Account ---
            # Blueprint: IF 'Dr' -> Route to Vendor Advance Array | Else 'Cr' [Error]
            if "on account" in btype_norm or "advance" in btype_norm:
                if drcr.lower() == "dr":
                    advance_lines.append({"ref": ref, "amount": amount})
                else:
                    _emit(f"[ERROR] Payment {payment_no}: Cr entry on Advance/On Account line '{ref}' amount={amount} — skipped")
                    errors.append({"Payment Number": payment_no, "Vendor": vendor_name, "Bill Numbers": ref, "Type": "Advance/On Account Cr Error", "Error Message": f"Cr entry not allowed for Advance/On Account (ref: {ref})"})
                continue

            # --- Agst Ref ---
            # Blueprint: IF 'Dr' -> Route to Bill Payment Array | Else 'Cr' [Error]
            if "agst" in btype_norm:
                if drcr.lower() != "dr":
                    _emit(f"[ERROR] Payment {payment_no}: Cr entry on Agst Ref line '{ref}' amount={amount} — skipped")
                    errors.append({"Payment Number": payment_no, "Vendor": vendor_name, "Bill Numbers": ref, "Type": "Agst Ref Cr Error", "Error Message": f"Cr entry not allowed for Agst Ref (ref: {ref})"})
                    continue
                # Dr -> falls through to Bill Payment accumulation below

            # --- New Ref ---
            # Blueprint: IF 'Dr' -> Lookup Purchase Bill Master -> YES=Bill Payment, NO=Vendor Advance
            #            Else 'Cr' [Error]
            elif "new ref" in btype_norm or btype_norm == "new":
                if drcr.lower() != "dr":
                    _emit(f"[ERROR] Payment {payment_no}: Cr entry on New Ref line '{ref}' amount={amount} — skipped")
                    errors.append({"Payment Number": payment_no, "Vendor": vendor_name, "Bill Numbers": ref, "Type": "New Ref Cr Error", "Error Message": f"Cr entry not allowed for New Ref (ref: {ref})"})
                    continue
                # Dr -> check Purchase Bill Master
                if ref in purchase_bill_master:
                    _emit(f"Payment {payment_no}: New Ref '{ref}' found in Purchase Bill Master -> Bill Payment")
                    # falls through to Bill Payment accumulation below
                else:
                    _emit(f"Payment {payment_no}: New Ref '{ref}' NOT in Purchase Bill Master -> Vendor Advance")
                    advance_lines.append({"ref": ref, "amount": amount})
                    continue

            # Bill Payment accumulation
            bill_row = None
            try:
                bill_row = database_manager.get_bill_by_number(ref)
            except Exception:
                bill_row = None

            bill_date = (bill_row or {}).get("date") if isinstance(bill_row, dict) else None
            bill_dt = None
            try:
                bill_dt = datetime.strptime(str(bill_date or ""), "%Y%m%d").date()
            except Exception:
                bill_dt = None

            is_prev_year = bool(cutoff_dt and bill_dt and bill_dt <= cutoff_dt)
            if cutoff_dt and not bill_dt:
                is_prev_year = True

            if is_prev_year:
                if not opening_bill_id:
                    _emit(f"Payment {payment_no}: opening_bill_id not set, cannot post previous-year allocation for ref '{ref}'")
                    continue
                prev_sum += amount
                bill_lines.append({"bill_id": opening_bill_id, "amount_applied": amount, "ref": ref, "bucket": "previous"})
            else:
                zoho_bill_id = _resolve_bill_id(ref)
                if not zoho_bill_id:
                    _emit(f"Payment {payment_no}: Zoho bill not found for '{ref}' -> routing to advance")
                    advance_lines.append({"ref": ref, "amount": amount})
                    continue
                curr_sum += amount
                bill_lines.append({"bill_id": zoho_bill_id, "amount_applied": amount, "ref": ref, "bucket": "current"})

        bank_account_name = (p.get("bank_account") or "").strip()
        account_id = _get_account_id(bank_account_name)

        # Build and submit ONE vendor payment (accumulated)
        total_payment = prev_sum + curr_sum
        if total_payment > 0 and bill_lines:
            ref_no = (p.get("reference_number") or "").strip()
            if not ref_no:
                distinct_refs = []
                for x in bill_lines:
                    r = str(x.get("ref", "")).strip()
                    if r and r not in distinct_refs:
                        distinct_refs.append(r)
                if distinct_refs:
                    ref_no = f"AGST-{'/'.join(distinct_refs)}"[:100]

            payload = {
                "vendor_id": vendor_id,
                "payment_mode": "banktransfer" if (p.get("payment_mode") or "").lower().startswith("bank") else "cash",
                "amount": round(total_payment, 2),
                "date": payment_date_iso or datetime.now().strftime("%Y-%m-%d"),
                "reference_number": ref_no,
                "description": (p.get("narration") or "").strip(),
                "bills": [{"bill_id": x["bill_id"], "amount_applied": round(_safe_float(x["amount_applied"]), 2)} for x in bill_lines],
            }
            if account_id:
                payload["account_id"] = account_id

            resp = zoho.api_call("POST", "/vendorpayments", payload=payload)
            if resp.get("code") == 0:
                stats["payments_created"] += 1
                _emit(f"[{idx}/{stats['total']}] Payment created: payment {payment_no} amount={round(total_payment,2)}")
            else:
                stats["failed"] += 1
                msg = resp.get("message") or "Zoho error"
                bill_nums = ", ".join([x.get("ref", "") for x in bill_lines])
                errors.append({"Payment Number": payment_no, "Vendor": vendor_name, "Bill Numbers": bill_nums, "Type": "Bill Payment", "Error Message": msg})
                _emit(f"[{idx}/{stats['total']}] Payment failed: payment {payment_no} ({msg})")

        # Submit Vendor Advances per line
        for adv in advance_lines:
            if _should_stop(stop_event):
                _emit("Stopped by user.")
                break
            
            # Zoho Books requires transaction_type="vendor_advance" for Advances
            payload = {
                "vendor_id": vendor_id,
                "payment_mode": "banktransfer" if (p.get("payment_mode") or "").lower().startswith("bank") else "cash",
                "amount": round(_safe_float(adv.get("amount")), 2),
                "date": payment_date_iso or datetime.now().strftime("%Y-%m-%d"),
                "reference_number": str(adv.get("ref") or "")[:100],
                "description": (p.get("narration") or "").strip()
            }
            if account_id:
                payload["account_id"] = account_id

            resp = zoho.api_call("POST", "/vendorpayments", params={"transaction_type": "vendor_advance"}, payload=payload)
            if resp.get("code") == 0:
                stats["advances_created"] += 1
                _emit(f"[{idx}/{stats['total']}] Advance created: payment {payment_no} ref='{adv.get('ref')}' amount={payload['amount']}")
            else:
                stats["failed"] += 1
                msg = resp.get("message") or "Zoho error"
                errors.append({"Payment Number": payment_no, "Vendor": vendor_name, "Bill Numbers": adv.get("ref"), "Type": "Vendor Advance", "Error Message": msg})
                _emit(f"[{idx}/{stats['total']}] Advance failed: payment {payment_no} ref='{adv.get('ref')}' ({msg})")

        if idx % 25 == 0 or idx == 1 or idx == stats["total"]:
            _emit(f"Progress: {idx}/{stats['total']} payments={stats['payments_created']} advances={stats['advances_created']} failed={stats['failed']}")

    if errors:
        try:
            import pandas as pd
            import os
            from datetime import datetime
            df = pd.DataFrame(errors)
            filename = f"Payments_Made_Errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            filepath = os.path.join(os.getcwd(), filename)
            df.to_excel(filepath, index=False)
            _emit(f"Errors exported to Excel file: {filename}")
        except Exception as e:
            _emit(f"Failed to export errors to Excel: {e}")

    return {"status": "success", "stats": stats, "errors": errors}
