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
    print(" Successfully imported database_manager for Credit Note module")
except ImportError:
    print("️ Warning: Could not import database_manager.")
    database_manager = None

load_dotenv()
TALLY_URL = "http://localhost:9000"
BASE_URL = "https://www.zohoapis.com/books/v3"
ORGANIZATION_ID = os.getenv("ORGANIZATION_ID")
from modules.zoho_connector import zoho

import re

def _clean_float(val, default=0.0):
    if val is None or val == "":
        return float(default)
    if isinstance(val, (int, float)):
        return float(val)
    try:
        # Split by '/' first to handle unit rates like '27.50/nos' -> '27.50'
        s = str(val).split('/')[0]
        cleaned = re.sub(r'[^0-9.-]', '', s)
        return float(cleaned) if cleaned else float(default)
    except Exception:
        return float(default)

def _date_range(from_date_str, to_date_str):
    start = datetime.strptime(from_date_str, "%Y%m%d")
    end   = datetime.strptime(to_date_str,   "%Y%m%d")
    cur   = start
    while cur <= end:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=1)


def _fetch_day_credit_note(date_str, retries=2):
    import time
    xml = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Credit Note</VOUCHERTYPENAME>
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


def fetch_tally_credit_note(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    import time
    print(f" Fetching Credit Note vouchers: {from_date} → {to_date} (day by day)...")
    credit_notes = []

    for day in _date_range(from_date, to_date):
        vouchers = _fetch_day_credit_note(day)
        if vouchers:
            print(f"   {day}: {len(vouchers)} credit_note(s)")
        time.sleep(1)

        for v in vouchers:
            credit_note_date   = v.find('DATE').text.strip() if v.find('DATE') else day
            tally_guid    = v.find('GUID').text.strip() if v.find('GUID') else ""
            credit_note_number = v.find('VOUCHERNUMBER').text.strip() if v.find('VOUCHERNUMBER') else ""
            if not credit_note_number:
                import hashlib
                credit_note_number = "AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
            
            if 'seen_credit_note_number' not in locals(): seen_credit_note_number = set()
            original_no = credit_note_number
            counter = 1
            while credit_note_number in seen_credit_note_number:
                suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
                credit_note_number = f"{original_no}_{suffix}"
                counter += 1
            seen_credit_note_number.add(credit_note_number)
            
            narration     = v.find('NARRATION').text.strip() if v.find('NARRATION') else ""
            party_name = v.find('PARTYLEDGERNAME').text.strip() if v.find('PARTYLEDGERNAME') else ""
            reference_no = v.find('REFERENCE').text.strip() if v.find('REFERENCE') else ""

            # ---- Ledger entries (XML fetch) ----
            ledger_entries = []
            from_account_name = party_name  # Party side
            to_account_name = ""            # Sales/Expense side
            amount = 0.0

            raw_entries = v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST')
            
            # Also check accounting allocations inside inventory entries
            inv_entries = v.find_all('ALLINVENTORYENTRIES.LIST') or v.find_all('INVENTORYENTRIES.LIST')
            for inv in inv_entries:
                for aa in inv.find_all('ACCOUNTINGALLOCATIONS.LIST'):
                    ename = aa.find('LEDGERNAME').text.strip() if aa.find('LEDGERNAME') else ""
                    eamt  = _clean_float(aa.find('AMOUNT').text if aa.find('AMOUNT') else "0", 0.0)
                    if ename:
                        ledger_entries.append({"ledger_name": ename, "amount": abs(eamt)})
                        if not to_account_name and ename.lower() != party_name.lower():
                            to_account_name = ename

            for entry in raw_entries:
                ename = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                eamt  = _clean_float(entry.find('AMOUNT').text if entry.find('AMOUNT') else "0", 0.0)
                is_deemed_pos = entry.find('ISDEEMEDPOSITIVE').text.strip().lower() in ['yes', 'true', '1'] if entry.find('ISDEEMEDPOSITIVE') else False
                
                if ename:
                    ledger_entries.append({"ledger_name": ename, "amount": abs(eamt)})

                if party_name and ename.lower() == party_name.lower():
                    from_account_name = ename
                    if abs(eamt) > 0 and amount == 0: amount = abs(eamt)
                elif is_deemed_pos:
                    if not to_account_name and ename.lower() != party_name.lower():
                        to_account_name = ename
                    if amount == 0 and abs(eamt) > 0: amount = abs(eamt)
                else:
                    if not from_account_name and ename.lower() != party_name.lower():
                        from_account_name = ename
                    if amount == 0 and abs(eamt) > 0: amount = abs(eamt)

            if party_name:
                from_account_name = party_name

            # Fallback for to_account_name
            if not to_account_name:
                for le in ledger_entries:
                    lname = le.get("ledger_name", "")
                    if lname and lname.lower() != from_account_name.lower():
                        if 'sales' in lname.lower() or 'gst' in lname.lower() or not to_account_name:
                            to_account_name = lname
                            if 'sales' in lname.lower(): break

            # ---- Inventory entries (XML fetch) ----
            line_items = []
            total_inv_amount = 0.0
            for inv in inv_entries:
                item_name = inv.find('STOCKITEMNAME').text.strip() if inv.find('STOCKITEMNAME') else ""
                qty_str   = inv.find('BILLEDQTY').text.strip() if inv.find('BILLEDQTY') else "0"
                rate_str  = inv.find('RATE').text.strip() if inv.find('RATE') else "0"
                iamt_str  = inv.find('AMOUNT').text.strip() if inv.find('AMOUNT') else "0"
                
                iamt = _clean_float(iamt_str, 0.0)
                total_inv_amount += abs(iamt)
                
                if item_name:
                    line_items.append({
                        "item_name": item_name,
                        "quantity": qty_str,
                        "rate": rate_str,
                        "amount": abs(iamt)
                    })

            # Calculate total voucher amount if 0
            if amount == 0.0:
                non_party_sum = sum(abs(le.get("amount", 0)) for le in ledger_entries if le.get("ledger_name", "").lower() != from_account_name.lower())
                if non_party_sum > 0:
                    amount = non_party_sum
                elif total_inv_amount > 0:
                    amount = total_inv_amount

            # ---- Cost center allocations (XML fetch) ----
            cost_center_allocations = []
            seen_cc = set()
            for entry in raw_entries:
                for ca in entry.find_all('CATEGORYALLOCATIONS.LIST'):
                    cat_name = ca.find('CATEGORY').text.strip() if ca.find('CATEGORY') else ''
                    for cc in ca.find_all('COSTCENTREALLOCATIONS.LIST'):
                        cc_name = cc.find('NAME').text.strip() if cc.find('NAME') else ''
                        cc_amt = _clean_float(cc.find('AMOUNT').text if cc.find('AMOUNT') else "0", 0.0)
                        full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                        if full and full not in seen_cc:
                            seen_cc.add(full)
                            cost_center_allocations.append({"category": full, "amount": abs(cc_amt)})

            credit_notes.append({
                "date": credit_note_date,
                "credit_note_number": credit_note_number,
                "voucher_type": "Credit Note",
                "from_account": from_account_name,
                "to_account": to_account_name,
                "amount": amount,
                "narration": narration,
                "reference_number": reference_no,
                "ledger_entries": ledger_entries,
                "line_items": line_items,
                "cost_center_allocations": cost_center_allocations,
                "tally_guid": tally_guid
            })

            if limit and len(credit_notes) >= limit:
                print(f"  Limit {limit} reached. Stopping early.")
                return credit_notes

    print(f" Fetched {len(credit_notes)} credit_note vouchers from Tally")
    return credit_notes


def parse_tally_json(json_path):
    """Parse JSON for Credit Note vouchers."""
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

    credit_notes = []

    for v in vouchers:
        if not isinstance(v, dict): continue
        if 'vouchernumber' not in v and 'vouchertypename' not in v and 'voucherkey' not in v: continue

        credit_note_date = str(v.get('date', '')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        credit_note_number = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or tally_guid or '').strip()
        if not credit_note_number:
            import hashlib
            credit_note_number = "AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
            
        if 'seen_credit_note_number' not in locals(): seen_credit_note_number = set()
        original_no = credit_note_number
        counter = 1
        while credit_note_number in seen_credit_note_number:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            credit_note_number = f"{original_no}_{suffix}"
            counter += 1
        seen_credit_note_number.add(credit_note_number)
        voucher_type = str(v.get('vouchertypename', 'Credit Note')).strip()
        
        # Only process Credit Note types just in case JSON includes multiple types
        if voucher_type.lower().replace(' ', '_') != 'credit_note':
            continue
            
        tally_guid = str(v.get('guid', '')).strip()
        narration = str(v.get('narration', '')).strip()
        party_name = str(v.get('partyledgername', '')).strip()
        reference_no = str(v.get('reference', '')).strip()

        all_entries = v.get('allledgerentries', [])
        if not isinstance(all_entries, list):
            all_entries = [all_entries] if all_entries else []

        ledger_entries = []
        from_account_name = party_name
        to_account_name = ""
        amount = 0.0

        for entry in all_entries:
            if not isinstance(entry, dict): continue
            ename = str(entry.get('ledgername', '')).strip()
            eamt = _clean_float(entry.get('amount', '0'), 0.0)

            ledger_entries.append({"ledger_name": ename, "amount": abs(eamt)})

            is_deemed_pos = str(entry.get('isdeemedpositive', '')).strip().lower() in ['yes', 'true', '1']
            
            if party_name and ename.lower() == party_name.lower():
                from_account_name = ename
                if abs(eamt) > 0 and amount == 0: amount = abs(eamt)
            elif is_deemed_pos:
                if not to_account_name and ename.lower() != party_name.lower():
                    to_account_name = ename
                if amount == 0 and abs(eamt) > 0: amount = abs(eamt)
            else:
                if not from_account_name and ename.lower() != party_name.lower():
                    from_account_name = ename
                if amount == 0 and abs(eamt) > 0: amount = abs(eamt)

        if party_name:
            from_account_name = party_name

        if not to_account_name:
            for le in ledger_entries:
                lname = le.get("ledger_name", "")
                if lname and lname.lower() != from_account_name.lower():
                    if 'sales' in lname.lower() or 'gst' in lname.lower() or not to_account_name:
                        to_account_name = lname
                        if 'sales' in lname.lower(): break

        # ---- Inventory entries (JSON parse) ----
        line_items = []
        total_inv_amount = 0.0
        inv_entries = v.get('allinventoryentries', [])
        if not isinstance(inv_entries, list): inv_entries = [inv_entries] if inv_entries else []
        for inv in inv_entries:
            if not isinstance(inv, dict): continue
            item_name = str(inv.get('stockitemname', '')).strip()
            qty = str(inv.get('billedqty', '0')).strip()
            rate = str(inv.get('rate', '0')).strip()
            iamt = _clean_float(inv.get('amount', '0'), 0.0)
            total_inv_amount += abs(iamt)
            
            if item_name:
                line_items.append({
                    "item_name": item_name,
                    "quantity": qty,
                    "rate": rate,
                    "amount": abs(iamt)
                })

        if amount == 0.0:
            non_party_sum = sum(abs(le.get("amount", 0)) for le in ledger_entries if le.get("ledger_name", "").lower() != from_account_name.lower())
            if non_party_sum > 0:
                amount = non_party_sum
            elif total_inv_amount > 0:
                amount = total_inv_amount

        # ---- Cost center allocations (JSON parse, deduplicated) ----
        cost_centers_dict = {}
        for entry in all_entries:
            if not isinstance(entry, dict): continue
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
                    cc_amt = _clean_float(cc.get('amount', '0'), 0.0)
                    full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full:
                        cost_centers_dict[full] = abs(cc_amt)
        cost_center_allocations = [{"category": k, "amount": v} for k, v in cost_centers_dict.items()]

        credit_notes.append({
            "date": credit_note_date,
            "credit_note_number": credit_note_number,
            "voucher_type": voucher_type,
            "from_account": from_account_name,
            "to_account": to_account_name,
            "amount": amount,
            "narration": narration,
            "reference_number": reference_no,
            "ledger_entries": ledger_entries,
            "line_items": line_items,
            "cost_center_allocations": cost_center_allocations,
            "tally_guid": tally_guid
        })

    print(f" Defensively parsed {len(credit_notes)} credit_note vouchers from JSON")
    return credit_notes


def get_fy(d_str):
    s = str(d_str).replace('-', '').strip()
    if len(s) >= 6:
        try:
            year = int(s[:4])
            month = int(s[4:6])
            if month >= 4:
                start_y = year
                end_y = year + 1
            else:
                start_y = year - 1
                end_y = year
            return f"{str(start_y)[-2:]}-{str(end_y)[-2:]}"
        except Exception:
            pass
    return "18-19"


def parse_tally_xml(xml_path_or_content, company_name=None):
    """Parse Tally XML export (like DayBook.xml) for Credit Note vouchers with full fidelity."""
    if os.path.exists(xml_path_or_content):
        content = None
        for enc in ['utf-16', 'utf-16-le', 'utf-8-sig', 'utf-8']:
            try:
                with open(xml_path_or_content, 'r', encoding=enc) as f:
                    content = f.read()
                break
            except Exception:
                continue
        if content is None:
            raise ValueError("Could not decode XML file with supported encodings")
    else:
        content = xml_path_or_content

    soup = BeautifulSoup(content, 'lxml-xml')
    vouchers = soup.find_all('VOUCHER')
    
    credit_notes = []
    seen_keys = set()
    
    for v in vouchers:
        vtype_tag = v.find('VOUCHERTYPENAME') or v.find('VCHTYPE')
        vtype = vtype_tag.text.strip() if vtype_tag else 'Credit Note'
        if 'credit' not in vtype.lower() or 'note' not in vtype.lower():
            continue
            
        tally_guid = v.find('GUID').text.strip() if v.find('GUID') else ''
        raw_vnum = v.find('VOUCHERNUMBER').text.strip() if v.find('VOUCHERNUMBER') else ''
        date_str = v.find('DATE').text.strip() if v.find('DATE') else ''
        fy = get_fy(date_str)
        
        # Format voucher number with FY to ensure uniqueness across financial years
        if raw_vnum:
            if raw_vnum.isdigit():
                formatted_no = f"CN/{int(raw_vnum):03d}/{fy}"
            elif '/' in raw_vnum:
                formatted_no = raw_vnum
            else:
                formatted_no = f"CN/{raw_vnum}/{fy}"
        else:
            import hashlib
            formatted_no = f"CN/AUTO-{hashlib.md5(str(v).encode('utf-8')).hexdigest()[:6]}/{fy}"

        final_cn_number = formatted_no
        counter = 1
        while final_cn_number in seen_keys:
            final_cn_number = f"{formatted_no}_{counter}"
            counter += 1
        seen_keys.add(final_cn_number)

        party_name = v.find('PARTYLEDGERNAME').text.strip() if v.find('PARTYLEDGERNAME') else ''
        if not party_name and v.find('BASICBUYERNAME'):
            party_name = v.find('BASICBUYERNAME').text.strip()
            
        narration = v.find('NARRATION').text.strip() if v.find('NARRATION') else ''
        reference_no = v.find('REFERENCE').text.strip() if v.find('REFERENCE') else ''
        reference_date = v.find('REFERENCEDATE').text.strip() if v.find('REFERENCEDATE') else ''
        party_gstin = v.find('PARTYGSTIN').text.strip() if v.find('PARTYGSTIN') else ''
        place_of_supply = v.find('PLACEOFSUPPLY').text.strip() if v.find('PLACEOFSUPPLY') else ''
        if not place_of_supply and v.find('STATENAME'):
            place_of_supply = v.find('STATENAME').text.strip()

        # Parse Ledger Entries
        ledger_entries = []
        raw_entries = v.find_all('ALLLEDGERENTRIES.LIST') or v.find_all('LEDGERENTRIES.LIST')
        
        from_account = party_name
        to_account = ''
        total_voucher_amt = 0.0
        tax_amount = 0.0
        party_found_amt = 0.0

        for entry in raw_entries:
            ename = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ''
            if not ename:
                continue
            eamt_str = entry.find('AMOUNT').text.strip() if entry.find('AMOUNT') else '0'
            try:
                eamt = float(eamt_str)
            except Exception:
                eamt = 0.0
                
            is_deemed_pos = entry.find('ISDEEMEDPOSITIVE').text.strip().lower() in ['yes', 'true', '1'] if entry.find('ISDEEMEDPOSITIVE') else False

            bill_allocs = []
            for ba in entry.find_all('BILLALLOCATIONS.LIST'):
                bname = ba.find('NAME').text.strip() if ba.find('NAME') else ''
                btype = ba.find('BILLTYPE').text.strip() if ba.find('BILLTYPE') else ''
                bamt_str = ba.find('AMOUNT').text.strip() if ba.find('AMOUNT') else '0'
                try:
                    bamt = float(bamt_str)
                except Exception:
                    bamt = 0.0
                if bname or bamt:
                    bill_allocs.append({'name': bname, 'type': btype, 'amount': abs(bamt)})

            # Classify entry
            entry_type = 'ledger'
            if party_name and ename.lower() == party_name.lower():
                entry_type = 'party'
                from_account = ename
                party_found_amt = abs(eamt)
            elif 'gst' in ename.lower() or 'tax' in ename.lower() or 'duty' in ename.lower():
                entry_type = 'tax'
                tax_amount += abs(eamt)
            else:
                entry_type = 'income_expense'
                if not to_account:
                    to_account = ename

            ledger_entries.append({
                'ledger_name': ename,
                'amount': abs(eamt),
                'raw_amount': eamt,
                'is_deemed_positive': is_deemed_pos,
                'type': entry_type,
                'bill_allocations': bill_allocs
            })

        if party_found_amt > 0:
            total_voucher_amt = party_found_amt
        else:
            total_voucher_amt = sum(e['amount'] for e in ledger_entries if e['type'] != 'party')

        if not to_account:
            for e in ledger_entries:
                if e['type'] != 'party' and e['type'] != 'tax':
                    to_account = e['ledger_name']
                    break
            if not to_account and ledger_entries:
                non_party = [e for e in ledger_entries if e['ledger_name'].lower() != party_name.lower()]
                if non_party:
                    to_account = non_party[0]['ledger_name']

        taxable_amount = max(0.0, total_voucher_amt - tax_amount)

        # Parse line items if any
        line_items = []
        for inv in v.find_all(['ALLINVENTORYENTRIES.LIST', 'INVENTORYENTRIES.LIST']):
            item_name = inv.find('STOCKITEMNAME').text.strip() if inv.find('STOCKITEMNAME') else ''
            if not item_name:
                continue
            bqty = inv.find('BILLEDQTY').text.strip() if inv.find('BILLEDQTY') else '1'
            rate_str = inv.find('RATE').text.strip() if inv.find('RATE') else '0'
            amt_str = inv.find('AMOUNT').text.strip() if inv.find('AMOUNT') else '0'
            try:
                iamt = abs(float(amt_str))
            except Exception:
                iamt = 0.0
            line_items.append({
                'item_name': item_name,
                'quantity': bqty,
                'rate': rate_str,
                'amount': iamt
            })

        credit_notes.append({
            'credit_note_number': final_cn_number,
            'voucher_number': raw_vnum,
            'voucher_type': 'Credit Note',
            'date': date_str,
            'financial_year': fy,
            'party_name': party_name,
            'from_account': from_account,
            'to_account': to_account,
            'amount': round(total_voucher_amt, 2),
            'tax_amount': round(tax_amount, 2),
            'taxable_amount': round(taxable_amount, 2),
            'narration': narration,
            'reference_number': reference_no,
            'reference_date': reference_date,
            'party_gstin': party_gstin,
            'place_of_supply': place_of_supply,
            'ledger_entries': ledger_entries,
            'line_items': line_items,
            'cost_center_allocations': [],
            'tally_guid': tally_guid,
            'company_name': company_name or 'Think Tree Media House'
        })

    print(f" Successfully parsed {len(credit_notes)} credit_note vouchers from XML")
    return credit_notes


# Removed manual get_access_token in favor of ZohoConnector

import difflib
from journel.journel_backend import _get_creds, get_access_token, get_zoho_contacts

_UNIFIED_ACCOUNTS_CACHE = {}

def get_unified_zoho_accounts(token=None, force_refresh=False):
    """Fetch unified Chart of Accounts including parent accounts and sub-accounts (Income/Expense/Liability/etc.)"""
    global _UNIFIED_ACCOUNTS_CACHE
    creds = _get_creds()
    org_id = creds["org_id"]
    if not force_refresh and org_id in _UNIFIED_ACCOUNTS_CACHE:
        return _UNIFIED_ACCOUNTS_CACHE[org_id]

    if not token:
        token = get_access_token()
    base_url = creds["base_url"]
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    
    accounts = {}
    
    # 1. Base COA
    page = 1
    while True:
        try:
            res = requests.get(f"{base_url}/chartofaccounts", headers=headers, params={"organization_id": org_id, "per_page": 200, "page": page}).json()
            acc_list = res.get("chartofaccounts", [])
            if not acc_list: break
            for a in acc_list:
                accounts[a["account_name"].lower().strip()] = a["account_id"]
            if not res.get("page_context", {}).get("has_more_page", False): break
            page += 1
        except Exception as e:
            print(f"Error fetching base COA page {page}: {e}")
            break

    # 2. Sub-accounts by type
    for at in ["AccountType.Income", "AccountType.OtherIncome", "AccountType.Expense", "AccountType.OtherExpense"]:
        page = 1
        while True:
            try:
                res = requests.get(f"{base_url}/chartofaccounts", headers=headers, params={"organization_id": org_id, "per_page": 200, "page": page, "filter_by": at}).json()
                acc_list = res.get("chartofaccounts", [])
                if not acc_list: break
                for a in acc_list:
                    accounts[a["account_name"].lower().strip()] = a["account_id"]
                if not res.get("page_context", {}).get("has_more_page", False): break
                page += 1
            except Exception as e:
                print(f"Error fetching COA sub-accounts for {at} page {page}: {e}")
                break

    _UNIFIED_ACCOUNTS_CACHE[org_id] = accounts
    print(f"    Loaded {len(accounts)} unified accounts from Zoho Books")
    return accounts

def _match_account_id(ledger_name, account_map):
    """Robust matcher for ledger names against Zoho Books Chart of Accounts"""
    if not ledger_name or not account_map:
        return None
    norm = ledger_name.lower().strip()
    if norm in account_map:
        return account_map[norm]

    # Clean punctuation / spaces / & vs and
    clean = norm.replace("/", " ").replace(".", "").replace("-", " ").replace("&", "and").strip()
    clean = " ".join(clean.split())
    for a_name, a_id in account_map.items():
        clean_a = a_name.replace("/", " ").replace(".", "").replace("-", " ").replace("&", "and").strip()
        clean_a = " ".join(clean_a.split())
        if clean == clean_a:
            return a_id

    # Try fuzzy match
    matches = difflib.get_close_matches(norm, list(account_map.keys()), n=1, cutoff=0.75)
    if matches:
        return account_map[matches[0]]

    return None

def create_zoho_journal_from_credit_note(credit_note_data, account_map, contact_map, token=None):
    """
    Create a Manual Journal in Zoho Books for a Credit Note voucher.
    Matches the exact structure of verified Zoho Journal 3615610000000410250:
    - Customer line: credited to Accounts Receivable with customer_id
    - Revenue/Expense lines: debited
    - Tax lines (CGST/SGST/IGST): debited
    - Reference number: CN/{num:02d}/{fy} or voucher number
    """
    if not credit_note_data or not isinstance(credit_note_data, dict):
        return False, "Invalid credit note data format"

    cn_no = credit_note_data.get("credit_note_number", "")
    
    # Check if already synced
    if credit_note_data.get("zoho_status") == "synced" and credit_note_data.get("zoho_journal_id"):
        return True, credit_note_data.get("zoho_journal_id")

    creds = _get_creds()
    if not token:
        token = get_access_token()
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    params = {"organization_id": creds["org_id"]}

    party_name = (credit_note_data.get("party_name") or credit_note_data.get("from_account") or "").strip()
    norm_party = party_name.lower()

    # 1. Resolve Contact
    contact_info = contact_map.get(norm_party)
    if not contact_info:
        matches = difflib.get_close_matches(norm_party, list(contact_map.keys()), n=1, cutoff=0.75)
        if matches:
            contact_info = contact_map[matches[0]]

    if not contact_info:
        err = f"Customer '{party_name}' not found in Zoho Books contacts."
        if database_manager:
            database_manager.update_credit_note_sync_status(cn_no, None, status='failed', error=err)
        return False, err

    customer_id = contact_info.get("contact_id")
    ar_account_id = account_map.get("accounts receivable")
    if not ar_account_id:
        err = "Accounts Receivable account not found in Zoho Chart of Accounts."
        if database_manager:
            database_manager.update_credit_note_sync_status(cn_no, None, status='failed', error=err)
        return False, err

    total_amount = float(credit_note_data.get("amount", 0) or 0)
    
    # 2. Build line items
    line_items = []

    # Party line (Credit)
    line_items.append({
        "account_id": ar_account_id,
        "customer_id": customer_id,
        "debit_or_credit": "credit",
        "amount": total_amount,
        "description": f"Credit Note {cn_no} - {party_name}"
    })

    # Debit lines (Revenue/Expense + Taxes)
    ledgers = credit_note_data.get("ledger_entries") or []
    if isinstance(ledgers, str):
        try: ledgers = json.loads(ledgers)
        except: ledgers = []

    debit_sum = 0.0
    for l in ledgers:
        lname = (l.get("ledger_name") or "").strip()
        if not lname or l.get("type") == "party" or lname.lower() == norm_party:
            continue
        l_amt = float(l.get("amount", 0) or 0)
        if l_amt <= 0:
            continue

        acc_id = _match_account_id(lname, account_map)
        if not acc_id:
            err = f"Account '{lname}' not found in Zoho Chart of Accounts."
            if database_manager:
                database_manager.update_credit_note_sync_status(cn_no, None, status='failed', error=err)
            return False, err

        debit_sum += l_amt
        line_items.append({
            "account_id": acc_id,
            "debit_or_credit": "debit",
            "amount": l_amt,
            "description": lname
        })

    # If ledgers didn't produce debit lines, fallback to to_account
    if not any(li["debit_or_credit"] == "debit" for li in line_items):
        to_acc = (credit_note_data.get("to_account") or "").strip()
        acc_id = _match_account_id(to_acc, account_map)
        if not acc_id:
            err = f"Account '{to_acc}' not found in Zoho Chart of Accounts."
            if database_manager:
                database_manager.update_credit_note_sync_status(cn_no, None, status='failed', error=err)
            return False, err
        line_items.append({
            "account_id": acc_id,
            "debit_or_credit": "debit",
            "amount": total_amount,
            "description": to_acc
        })
        debit_sum = total_amount

    # Balance check
    if abs(total_amount - debit_sum) > 0.05:
        diff = round(total_amount - debit_sum, 2)
        round_off_id = account_map.get("round off") or account_map.get("round off account")
        if round_off_id:
            line_items.append({
                "account_id": round_off_id,
                "debit_or_credit": "debit" if diff > 0 else "credit",
                "amount": abs(diff),
                "description": "Round Off"
            })
        else:
            for li in line_items:
                if li["debit_or_credit"] == "debit":
                    li["amount"] = round(li["amount"] + diff, 2)
                    break

    # Format date
    raw_date = str(credit_note_data.get("date", "")).replace("-", "").strip()
    zoho_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}" if len(raw_date) == 8 else datetime.now().strftime("%Y-%m-%d")

    # Format reference number matching pattern CN/01/19-20
    vnum = str(credit_note_data.get("voucher_number", "")).strip()
    fy = credit_note_data.get("financial_year", "")
    if vnum and fy:
        if vnum.isdigit():
            ref_no = f"CN/{int(vnum):02d}/{fy}"
        else:
            ref_no = f"CN/{vnum}/{fy}"
    else:
        ref_no = cn_no

    payload = {
        "journal_date": zoho_date,
        "reference_number": ref_no,
        "notes": (credit_note_data.get("narration") or "").strip()[:900],
        "line_items": line_items,
        "status": "published"
    }

    url = f"{creds['base_url']}/journals"
    try:
        res = requests.post(url, headers=headers, params=params, json=payload, timeout=30)
        data = res.json()
        if data.get("code") == 0:
            jid = data.get("journal", {}).get("journal_id")
            if database_manager:
                database_manager.update_credit_note_sync_status(cn_no, jid, status='synced', error=None)
            print(f"    SUCCESS: Synced Credit Note {cn_no} as Zoho Journal #{jid} (Ref: {ref_no})")
            return True, jid
        else:
            msg = data.get("message", "Unknown Zoho error")
            print(f"    FAILED: {cn_no} - {msg}")
            if database_manager:
                database_manager.update_credit_note_sync_status(cn_no, None, status='failed', error=msg)
            return False, msg
    except Exception as e:
        err = f"Connection error: {e}"
        print(f"    ERROR: {cn_no} - {err}")
        if database_manager:
            database_manager.update_credit_note_sync_status(cn_no, None, status='failed', error=err)
        return False, err


def sync_credit_note_to_zoho(selected_credit_notes=None, from_date="20250401", to_date="20250430", limit=None, company_name=None):
    if database_manager:
        database_manager.init_db()
        if selected_credit_notes:
            if isinstance(selected_credit_notes, list) and len(selected_credit_notes) > 0 and isinstance(selected_credit_notes[0], dict):
                credit_notes = selected_credit_notes
            else:
                credit_notes = [database_manager.get_credit_note_by_number(c) for c in selected_credit_notes if database_manager.get_credit_note_by_number(c)]
        else:
            credit_notes = database_manager.get_all_credit_notes()
            if not credit_notes:
                credit_notes = fetch_tally_credit_note(from_date, to_date, limit, company_name)
    else:
        if selected_credit_notes and isinstance(selected_credit_notes, list) and isinstance(selected_credit_notes[0], dict):
            credit_notes = selected_credit_notes
        else:
            credit_notes = fetch_tally_credit_note(from_date, to_date, limit, company_name)

    if isinstance(credit_notes, dict) and "error" in credit_notes:
        return credit_notes

    if not credit_notes:
        return {"status": "error", "message": "No credit_note vouchers found to sync."}

    token = get_access_token()
    account_map = get_unified_zoho_accounts(token=token)
    contact_map = get_zoho_contacts(token=token, use_cache=True)

    results = {"total": len(credit_notes), "success": 0, "failed": 0, "already_synced": 0, "errors": []}

    for credit_note in credit_notes:
        if not credit_note:
            continue
        if hasattr(credit_note, 'keys'):
            credit_note_data = dict(credit_note)
        else:
            credit_note_data = credit_note
        if not isinstance(credit_note_data, dict):
            continue

        # Skip if already synced
        if credit_note_data.get("zoho_status") == "synced" and credit_note_data.get("zoho_journal_id"):
            results["already_synced"] += 1
            results["success"] += 1
            continue

        success, res = create_zoho_journal_from_credit_note(credit_note_data, account_map, contact_map, token=token)
        if success:
            results["success"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({
                "credit_note_number": credit_note_data.get("credit_note_number", ""),
                "error": res
            })

    results["status"] = "success"
    results["message"] = f"Processed {results['total']} credit notes: {results['success']} synced ({results['already_synced']} pre-synced), {results['failed']} failed."
    return results



def get_all_credit_note_data(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    if database_manager:
        database_manager.init_db()

    credit_notes = fetch_tally_credit_note(from_date, to_date, limit, company_name)

    if database_manager and credit_notes:
        db_data_list = []
        for credit_note in credit_notes:
            db_data = {
                "credit_note_number": credit_note.get("credit_note_number", ""),
                "voucher_type": credit_note.get("voucher_type", ""),
                "date": credit_note.get("date", ""),
                "from_account": credit_note.get("from_account", ""),
                "to_account": credit_note.get("to_account", ""),
                "amount": credit_note.get("amount", 0) or 0,
                "narration": credit_note.get("narration", ""),
                "ledger_entries": json.dumps(credit_note.get("ledger_entries", [])),
                "line_items": json.dumps(credit_note.get("line_items", [])),
                "cost_center_allocations": json.dumps(credit_note.get("cost_center_allocations", [])),
                "tally_guid": credit_note.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            db_data_list.append(db_data)

        try:
            database_manager.bulk_save_credit_notes(db_data_list)
        except AttributeError:
            print("Warning: database_manager.bulk_save_credit_notes not found yet.")

    total_amount = sum(c.get("amount", 0) for c in credit_notes)
    return {"credit_notes": credit_notes, "total_amount": total_amount}
