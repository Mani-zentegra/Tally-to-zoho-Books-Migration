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
from journel.journel_backend import _get_creds, get_access_token

def get_zoho_customers(token=None):
    """Fetch all customer contacts from Zoho Books dynamically using _get_creds() & get_access_token()"""
    creds = _get_creds()
    if not token:
        token = get_access_token()
    customers = {}
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    while True:
        url = f"{creds['base_url']}/contacts"
        params = {"organization_id": creds["org_id"], "page": page, "per_page": 200}
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if res.status_code == 200 and data.get("code") == 0:
                contacts_list = data.get("contacts", [])
                if not contacts_list:
                    break
                for c in contacts_list:
                    customers[c["contact_name"].lower().strip()] = c["contact_id"]
                page_context = data.get("page_context", {})
                if not page_context.get("has_more_page", False):
                    break
                page += 1
            else:
                print(f"  [ERROR] Fetch contacts page {page} failed ({res.status_code}): {data.get('message')}")
                break
        except Exception as e:
            print(f"Error fetching customers page {page}: {e}")
            break
    print(f" Fetched {len(customers)} Zoho customer contacts across {page} page(s)")
    return customers

def get_zoho_items(token=None):
    """Fetch all items across all pages in Zoho Books dynamically using _get_creds() & get_access_token()"""
    creds = _get_creds()
    if not token:
        token = get_access_token()
    items = {}
    page = 1
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    while True:
        url = f"{creds['base_url']}/items"
        params = {"organization_id": creds["org_id"], "page": page, "per_page": 200}
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if res.status_code == 200 and data.get("code") == 0:
                items_list = data.get("items", [])
                if not items_list:
                    break
                for i in items_list:
                    items[i["name"].lower().strip()] = i["item_id"]
                page_context = data.get("page_context", {})
                if not page_context.get("has_more_page", False):
                    break
                page += 1
            else:
                print(f"  [ERROR] Fetch items page {page} failed ({res.status_code}): {data.get('message')}")
                break
        except Exception as e:
            print(f"Error fetching items page {page}: {e}")
            break
    print(f" Fetched {len(items)} Zoho items across {page} page(s)")
    return items


def create_zoho_credit_note(credit_note_data, customer_map, item_map, tags_list=None, token=None):
    """Create a Credit Note in Zoho Books dynamically"""
    if not credit_note_data or not isinstance(credit_note_data, dict):
        return False, "Invalid credit note data format"

    creds = _get_creds()
    if not token:
        token = get_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    # Parse and match reporting tags safely
    zoho_tags = []
    if tags_list and isinstance(tags_list, list):
        tally_ccs = credit_note_data.get("cost_center_allocations") or []
        if isinstance(tally_ccs, str):
            try: tally_ccs = json.loads(tally_ccs)
            except: tally_ccs = []
        if not isinstance(tally_ccs, list):
            tally_ccs = []

        existing_added = set()
        for cc in tally_ccs:
            if not isinstance(cc, dict): continue
            cc_full = str(cc.get("category", ""))
            parts = cc_full.split(' - ', 1)
            cat_name = parts[0].strip().lower() if len(parts) > 0 else ""
            opt_name = parts[1].strip().lower() if len(parts) > 1 else cat_name
            
            if len(parts) == 1:
                opt_name = parts[0].strip().lower()
                cat_name = ""

            for tag in tags_list:
                if not isinstance(tag, dict) or "tag_id" not in tag:
                    continue
                if tag["tag_id"] in existing_added:
                    continue
                options = tag.get("options", {})
                if opt_name in options:
                    if not cat_name or cat_name in tag.get("tag_name", "").lower() or tag.get("tag_name", "").lower() in cat_name:
                        zoho_tags.append({
                            "tag_id": tag["tag_id"],
                            "tag_option_id": options[opt_name]
                        })
                        existing_added.add(tag["tag_id"])
                        break

    raw_from = str(credit_note_data.get("from_account") or "").lower().strip()
    raw_to = str(credit_note_data.get("to_account") or "").lower().strip()

    customer_id = customer_map.get(raw_from) or customer_map.get(raw_to)

    # Fuzzy match if exact match fails
    if not customer_id and (customer_map and len(customer_map) > 0):
        all_names = list(customer_map.keys())
        for raw_name in [raw_from, raw_to]:
            if not raw_name: continue
            matches = difflib.get_close_matches(raw_name, all_names, n=1, cutoff=0.75)
            if matches:
                customer_id = customer_map[matches[0]]
                print(f"   Fuzzy matched '{raw_name}' to '{matches[0]}'")
                break

    if not customer_id:
        return False, f"Customer '{raw_from or raw_to}' not found in Zoho Books"

    date_str = str(credit_note_data.get("date", "")).replace("-", "").strip()
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else datetime.now().strftime("%Y-%m-%d")

    line_items = []
    tally_items = credit_note_data.get("line_items") or []
    if isinstance(tally_items, str):
        try: tally_items = json.loads(tally_items)
        except: tally_items = []
    if not isinstance(tally_items, list):
        tally_items = []
    
    if tally_items:
        for item in tally_items:
            if not isinstance(item, dict): continue
            item_name = str(item.get("item_name", "")).lower().strip()
            item_id = item_map.get(item_name) if item_map else None
            if not item_id and item_name and item_map:
                matches = difflib.get_close_matches(item_name, list(item_map.keys()), n=1, cutoff=0.75)
                if matches:
                    item_id = item_map[matches[0]]
            if item_id:
                qty = _clean_float(item.get("quantity"), 1.0)
                rate = _clean_float(item.get("rate"), 0.0)
                line_items.append({
                    "item_id": item_id,
                    "quantity": qty if qty > 0 else 1.0,
                    "rate": rate
                })
    
    if not line_items:
        # Fallback to generic line item if inventory entry not present
        if item_map and len(item_map) > 0:
            first_item_id = list(item_map.values())[0]
            line_items.append({
                "item_id": first_item_id,
                "quantity": 1.0,
                "rate": _clean_float(credit_note_data.get("amount"), 0.0)
            })

    if not line_items:
        return False, "No matching items found in Zoho Books for this Credit Note"

    # Try to resolve linked invoice_id if reference_number exists
    ref_no = str(credit_note_data.get("reference_number") or credit_note_data.get("invoice_number") or "").strip()
    invoice_id = None

    if ref_no:
        try:
            inv_resp = requests.get(f"{creds['base_url']}/invoices", headers=headers, params={"organization_id": creds["org_id"], "invoice_number": ref_no})
            if inv_resp.status_code == 200 and inv_resp.json().get("code") == 0:
                invoices = inv_resp.json().get("invoices", [])
                if invoices:
                    invoice_id = invoices[0].get("invoice_id")
        except Exception as e:
            print(f"  Note: Invoice lookup for '{ref_no}' error: {e}")

    # If no existing invoice is linked, auto-create parent sales invoice for customer in Zoho Books
    if not invoice_id:
        print(f"   Auto-creating base sales invoice in Zoho Books for customer '{customer_id}'...")
        inv_payload = {
            "customer_id": customer_id,
            "date": formatted_date,
            "line_items": line_items,
            "notes": f"Base Sales Invoice for Credit Note #{credit_note_data.get('credit_note_number', '')}"
        }
        if ref_no:
            inv_payload["reference_number"] = ref_no

        try:
            inv_res = requests.post(f"{creds['base_url']}/invoices", headers=headers, params=params, json=inv_payload).json()
            if inv_res.get("code") == 0:
                invoice_id = inv_res.get("invoice", {}).get("invoice_id")
                if invoice_id:
                    # Mark status as sent so credit note can link
                    requests.post(f"{creds['base_url']}/invoices/{invoice_id}/status/sent", headers=headers, params=params)
                    print(f"   Created base invoice in Zoho Books with ID: {invoice_id}")
            else:
                print(f"  Note: Auto invoice creation message: {inv_res.get('message')}")
        except Exception as e:
            print(f"  Note: Auto invoice creation error: {e}")

    payload = {
        "customer_id": customer_id,
        "date": formatted_date,
        "creditnote_number": credit_note_data.get("credit_note_number", ""),
        "reference_number": str(credit_note_data.get("reference_number") or "").strip(),
        "reason_for_creditnote": "others",
        "line_items": line_items,
        "notes": credit_note_data.get("narration", "")
    }

    if invoice_id:
        payload["invoice_id"] = invoice_id
    else:
        payload["invoice_type"] = credit_note_data.get("invoice_type", "invoice")

    if zoho_tags:
        payload["tags"] = zoho_tags

    url = f"{creds['base_url']}/creditnotes"
    try:
        cn_params = dict(params)
        cn_params["ignore_auto_number_generation"] = "true"
        res = requests.post(url, headers=headers, params=cn_params, json=payload)
        data = res.json()
        if data.get("code") == 0:
            return True, "Success"
        elif data.get("code") == 4097:
            # If auto-numbering is strictly enforced by Zoho settings, let Zoho assign number automatically
            payload.pop("creditnote_number", None)
            res2 = requests.post(url, headers=headers, params=params, json=payload)
            data2 = res2.json()
            if data2.get("code") == 0:
                return True, "Success"
            return False, data2.get("message", "Error creating credit note")
        return False, data.get("message", "Error creating credit note")
    except Exception as e:
        return False, f"Connection error: {e}"


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
                # Fallback to Tally fetch
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
    customer_map = get_zoho_customers(token=token)
    item_map = get_zoho_items(token=token)
    tags_list = None
    try:
        raw_tags = zoho.get_reporting_tags()
        if isinstance(raw_tags, list):
            tags_list = raw_tags
    except Exception as e:
        print(f"Note: Reporting tags skipped: {e}")

    results = {"total": len(credit_notes), "success": 0, "failed": 0, "errors": []}

    for credit_note in credit_notes:
        if not credit_note:
            continue
        if hasattr(credit_note, 'keys'):
            credit_note_data = dict(credit_note)
        else:
            credit_note_data = credit_note
        if not isinstance(credit_note_data, dict):
            continue

        success, error = create_zoho_credit_note(credit_note_data, customer_map, item_map, tags_list, token=token)
        if success:
            results["success"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({
                "credit_note_number": credit_note_data.get("credit_note_number", ""),
                "error": error
            })

    results["status"] = "success"
    results["message"] = f"Synced {results['success']} out of {results['total']} credit_note vouchers"
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
