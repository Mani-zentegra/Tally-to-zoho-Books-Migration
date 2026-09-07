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
    print(" Successfully imported database_manager for Debit Note module")
except ImportError:
    print("️ Warning: Could not import database_manager.")
    database_manager = None

load_dotenv()
TALLY_URL = "http://localhost:9000"
BASE_URL = "https://www.zohoapis.com/books/v3"
ORGANIZATION_ID = os.getenv("ORGANIZATION_ID")
from modules.zoho_connector import zoho

def _date_range(from_date_str, to_date_str):
    start = datetime.strptime(from_date_str, "%Y%m%d")
    end   = datetime.strptime(to_date_str,   "%Y%m%d")
    cur   = start
    while cur <= end:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=1)


def _fetch_day_debit_note(date_str, retries=2):
    import time
    xml = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Debit Note</VOUCHERTYPENAME>
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


def fetch_tally_debit_note(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    import time
    print(f" Fetching Debit Note vouchers: {from_date} → {to_date} (day by day)...")
    debit_notes = []

    for day in _date_range(from_date, to_date):
        vouchers = _fetch_day_debit_note(day)
        if vouchers:
            print(f"   {day}: {len(vouchers)} debit_note(s)")
        time.sleep(1)

        for v in vouchers:
            debit_note_date   = v.find('DATE').text.strip() if v.find('DATE') else day
            debit_note_number = v.find('VOUCHERNUMBER').text.strip() if v.find('VOUCHERNUMBER') else ""
            tally_guid    = v.find('GUID').text.strip() if v.find('GUID') else ""
            narration     = v.find('NARRATION').text.strip() if v.find('NARRATION') else ""

            # Explicit party if any
            explicit_party = v.find('PARTYLEDGERNAME').text.strip() if v.find('PARTYLEDGERNAME') else ""

            # ---- Ledger entries (XML fetch) ----
            ledger_entries = []
            from_account_name = ""  # Credit side
            to_account_name = ""    # Debit side
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
                    # Positive amount = Debit
                    if not to_account_name: to_account_name = ename
                    if amount == 0: amount = abs(eamt)
                elif eamt < 0:
                    # Negative amount = Credit
                    if not from_account_name: from_account_name = ename
                    if amount == 0: amount = abs(eamt)

            # If explicit party exists, it is often the main account (usually Debit for DN)
            if explicit_party:
                if not to_account_name: to_account_name = explicit_party
                elif not from_account_name: from_account_name = explicit_party

            # ---- Inventory entries (XML fetch) ----
            line_items = []
            inv_entries = v.find_all('ALLINVENTORYENTRIES.LIST') or v.find_all('INVENTORYENTRIES.LIST')
            for inv in inv_entries:
                item_name = inv.find('STOCKITEMNAME').text.strip() if inv.find('STOCKITEMNAME') else ""
                qty_str   = inv.find('BILLEDQTY').text.strip() if inv.find('BILLEDQTY') else "0"
                rate_str  = inv.find('RATE').text.strip() if inv.find('RATE') else "0"
                iamt_str  = inv.find('AMOUNT').text.strip() if inv.find('AMOUNT') else "0"
                
                try: iamt = abs(float(iamt_str))
                except: iamt = 0.0
                
                if item_name:
                    line_items.append({
                        "item_name": item_name,
                        "quantity": qty_str,
                        "rate": rate_str,
                        "amount": iamt
                    })

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

            debit_notes.append({
                "date": debit_note_date,
                "debit_note_number": debit_note_number,
                "voucher_type": "Debit Note",
                "from_account": from_account_name,
                "to_account": to_account_name,
                "amount": amount,
                "narration": narration,
                "ledger_entries": ledger_entries,
                "line_items": line_items,
                "cost_center_allocations": cost_center_allocations,
                "tally_guid": tally_guid
            })

            if limit and len(debit_notes) >= limit:
                print(f"  Limit {limit} reached. Stopping early.")
                return debit_notes

    print(f" Fetched {len(debit_notes)} debit_note vouchers from Tally")
    return debit_notes


def parse_tally_json(json_path):
    """Parse JSON for Debit Note vouchers."""
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

    debit_notes = []

    for v in vouchers:
        if not isinstance(v, dict): continue
        if 'vouchernumber' not in v and 'vouchertypename' not in v and 'voucherkey' not in v: continue

        debit_note_date = str(v.get('date', '')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        debit_note_number = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or tally_guid or '').strip()
        if not debit_note_number:
            import hashlib
            debit_note_number = "AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
        if 'seen_debit_note_number' not in locals(): seen_debit_note_number = set()
        original_no = debit_note_number
        counter = 1
        while debit_note_number in seen_debit_note_number:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            debit_note_number = f"{original_no}_{suffix}"
            counter += 1
        seen_debit_note_number.add(debit_note_number)
        voucher_type = str(v.get('vouchertypename', 'Debit Note')).strip()
        
        # Only process Debit Note types just in case JSON includes multiple types
        if voucher_type.lower().replace(' ', '_') != 'debit_note':
            continue
            
        tally_guid = str(v.get('guid', '')).strip()
        narration = str(v.get('narration', '')).strip()

        all_entries = v.get('allledgerentries', [])
        if not isinstance(all_entries, list):
            all_entries = [all_entries] if all_entries else []

        ledger_entries = []
        from_account_name = ""
        to_account_name = ""
        amount = 0.0

        for entry in all_entries:
            if not isinstance(entry, dict): continue
            ename = str(entry.get('ledgername', '')).strip()
            try: eamt = float(entry.get('amount', '0'))
            except (ValueError, TypeError): eamt = 0.0

            ledger_entries.append({"ledger_name": ename, "amount": eamt})

            is_deemed_positive = str(entry.get('isdeemedpositive', '')).strip().lower() in ['yes', 'true', '1']
            
            if is_deemed_positive:
                if not to_account_name: to_account_name = ename
                if amount == 0.0: amount = abs(eamt)
            else:
                if not from_account_name: from_account_name = ename
                if amount == 0.0: amount = abs(eamt)

        # Fallback to PARTYLEDGERNAME
        party_name = str(v.get('partyledgername', '')).strip()
        if party_name:
            if not to_account_name: to_account_name = party_name
            elif not from_account_name: from_account_name = party_name

        # ---- Inventory entries (JSON parse) ----
        line_items = []
        inv_entries = v.get('allinventoryentries', [])
        if not isinstance(inv_entries, list): inv_entries = [inv_entries] if inv_entries else []
        for inv in inv_entries:
            if not isinstance(inv, dict): continue
            item_name = str(inv.get('stockitemname', '')).strip()
            qty = str(inv.get('billedqty', '0')).strip()
            rate = str(inv.get('rate', '0')).strip()
            try: iamt = abs(float(inv.get('amount', '0')))
            except: iamt = 0.0
            
            if item_name:
                line_items.append({
                    "item_name": item_name,
                    "quantity": qty,
                    "rate": rate,
                    "amount": iamt
                })

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
                    try: cc_amt = float(cc.get('amount', '0'))
                    except (ValueError, TypeError): cc_amt = 0.0
                    full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full:
                        cost_centers_dict[full] = abs(cc_amt)
        cost_center_allocations = [{"category": k, "amount": v} for k, v in cost_centers_dict.items()]

        debit_notes.append({
            "date": debit_note_date,
            "debit_note_number": debit_note_number,
            "voucher_type": voucher_type,
            "from_account": from_account_name,
            "to_account": to_account_name,
            "amount": amount,
            "narration": narration,
            "ledger_entries": ledger_entries,
            "line_items": line_items,
            "cost_center_allocations": cost_center_allocations,
            "tally_guid": tally_guid
        })

    print(f" Defensively parsed {len(debit_notes)} debit_note vouchers from JSON")
    return debit_notes


# Removed manual get_access_token in favor of ZohoConnector

def get_zoho_vendors():
    resp = zoho.api_call("GET", "/contacts", params={"contact_type": "vendor"})
    if resp.get("code") == 0:
        contacts = resp.get("contacts", [])
        return {c["contact_name"].lower(): c["contact_id"] for c in contacts}
    return {}

def get_zoho_items():
    resp = zoho.api_call("GET", "/items")
    if resp.get("code") == 0:
        items = resp.get("items", [])
        return {i["name"].lower(): i["item_id"] for i in items}
    return {}


def create_zoho_vendor_credit(debit_note_data, vendor_map, item_map, tags_list=None):
    """Create a Vendor Credit in Zoho Books (from Tally Debit Note)"""
    
    # Parse and match reporting tags
    zoho_tags = []
    if tags_list:
        tally_ccs = debit_note_data.get("cost_center_allocations", [])
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

    vendor_name = debit_note_data.get("from_account", "").lower()
    vendor_id = vendor_map.get(vendor_name)
    
    if not vendor_id:
        vendor_name = debit_note_data.get("to_account", "").lower()
        vendor_id = vendor_map.get(vendor_name)
        
    if not vendor_id:
        return False, f"Vendor '{vendor_name}' not found in Zoho"

    date_str = debit_note_data.get("date", "")
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else datetime.now().strftime("%Y-%m-%d")

    line_items = []
    tally_items = debit_note_data.get("line_items", [])
    if isinstance(tally_items, str):
        try: tally_items = json.loads(tally_items)
        except: tally_items = []
    
    if tally_items:
        for item in tally_items:
            item_name = item.get("item_name", "").lower()
            item_id = item_map.get(item_name)
            if item_id:
                line_items.append({
                    "item_id": item_id,
                    "quantity": float(str(item.get("quantity", "1")).split()[0] or 1),
                    "rate": item.get("rate", 0)
                })
    
    if not line_items:
        return False, "No matching items found in Zoho for this Debit Note"

    payload = {
        "vendor_id": vendor_id,
        "date": formatted_date,
        "vendor_credit_number": debit_note_data.get("debit_note_number", ""),
        "line_items": line_items,
        "description": debit_note_data.get("narration", "")
    }

    if zoho_tags:
        payload["tags"] = zoho_tags


    resp = zoho.api_call("POST", "/vendorcredits", payload={"JSONString": json.dumps(payload)})
    if resp.get("code") == 0:
        return True, "Success"
    return False, resp.get("message", "Error")


def sync_debit_note_to_zoho(selected_debit_notes=None, from_date="20250401", to_date="20250430", limit=None, company_name=None):
    if database_manager:
        database_manager.init_db()
        if selected_debit_notes:
            debit_notes = [database_manager.get_debit_note_by_number(c) for c in selected_debit_notes if database_manager.get_debit_note_by_number(c)]
        else:
            debit_notes = database_manager.get_all_debit_notes()
            if not debit_notes:
                # Fallback to Tally fetch
                debit_notes = fetch_tally_debit_note(from_date, to_date, limit, company_name)
    else:
        debit_notes = fetch_tally_debit_note(from_date, to_date, limit, company_name)

    if isinstance(debit_notes, dict) and "error" in debit_notes:
        return debit_notes

    if not debit_notes:
        return {"status": "error", "message": "No debit_note vouchers found to sync."}

    vendor_map = get_zoho_vendors()
    item_map = get_zoho_items()
    tags_list = zoho.get_reporting_tags()

    results = {"total": len(debit_notes), "success": 0, "failed": 0, "errors": []}

    for debit_note in debit_notes:
        if hasattr(debit_note, 'keys'):
            debit_note_data = dict(debit_note)
        else:
            debit_note_data = debit_note

        success, error = create_zoho_vendor_credit(debit_note_data, vendor_map, item_map, tags_list)
        if success:
            results["success"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({
                "debit_note_number": debit_note_data.get("debit_note_number", ""),
                "error": error
            })

    results["status"] = "success"
    results["message"] = f"Synced {results['success']} out of {results['total']} debit_note vouchers"
    return results


def get_all_debit_note_data(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    if database_manager:
        database_manager.init_db()

    debit_notes = fetch_tally_debit_note(from_date, to_date, limit, company_name)

    if database_manager and debit_notes:
        db_data_list = []
        for debit_note in debit_notes:
            db_data = {
                "debit_note_number": debit_note.get("debit_note_number", ""),
                "voucher_type": debit_note.get("voucher_type", ""),
                "date": debit_note.get("date", ""),
                "from_account": debit_note.get("from_account", ""),
                "to_account": debit_note.get("to_account", ""),
                "amount": debit_note.get("amount", 0) or 0,
                "narration": debit_note.get("narration", ""),
                "ledger_entries": json.dumps(debit_note.get("ledger_entries", [])),
                "line_items": json.dumps(debit_note.get("line_items", [])),
                "cost_center_allocations": json.dumps(debit_note.get("cost_center_allocations", [])),
                "tally_guid": debit_note.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            db_data_list.append(db_data)

        try:
            database_manager.bulk_save_debit_notes(db_data_list)
        except AttributeError:
            print("Warning: database_manager.bulk_save_debit_notes not found yet.")

    total_amount = sum(c.get("amount", 0) for c in debit_notes)
    return {"debit_notes": debit_notes, "total_amount": total_amount}
