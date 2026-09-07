import os
import sys
import requests
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from collections import defaultdict
from dotenv import load_dotenv
import json
import re
from fuzzywuzzy import fuzz

# Add parent directory so we can import shared database_manager
sys.path.append(str(Path(__file__).parent.parent))

try:
    import database_manager
except ImportError:
    print("️ Warning: Could not import database_manager. SQLite sync will be skipped.")
    database_manager = None

from journel.journel_backend import (
    get_access_token,
    get_zoho_contacts as get_shared_zoho_contacts,
    find_or_create_contact as shared_find_or_create_contact,
    _get_creds,
    get_zoho_contacts_from_cache,
    save_zoho_contacts_to_cache
)

TALLY_URL = "http://localhost:9000"

# Cache for vendor payment terms to avoid repeated queries
vendor_payment_terms_cache = {}

def fetch_vendor_payment_terms(vendor_name):
    """Fetch payment terms from vendor ledger master in Tally"""
    if not vendor_name:
        return ""
    
    # Check cache first
    if vendor_name in vendor_payment_terms_cache:
        return vendor_payment_terms_cache[vendor_name]
    
    # XML request to fetch specific ledger details
    ledger_xml = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Ledgers</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    
    try:
        res = requests.post(TALLY_URL, data=ledger_xml, timeout=15)
        soup = BeautifulSoup(res.content, 'lxml-xml')
        
        # Find the specific vendor ledger
        for ledger in soup.find_all('LEDGER'):
            name = ledger.get('NAME', '').strip()
            if name.lower() == vendor_name.lower():
                # Check for CREDITPERIOD field
                credit_period = ledger.find('CREDITPERIOD')
                if credit_period and credit_period.text:
                    terms = credit_period.text.strip()
                    vendor_payment_terms_cache[vendor_name] = terms
                    return terms
                
                # Alternative: Check for BILLCREDITPERIOD in ledger
                bill_credit = ledger.find('BILLCREDITPERIOD')
                if bill_credit and bill_credit.text:
                    terms = bill_credit.text.strip()
                    vendor_payment_terms_cache[vendor_name] = terms
                    return terms
                
                break
    except:
        pass
    
    # Cache empty result to avoid repeated queries
    vendor_payment_terms_cache[vendor_name] = ""
    return ""

def get_payment_terms_hierarchical(voucher, party_name):
    """
    Extract payment terms using hierarchical method:
    1. Check BILLALLOCATIONS.LIST → BILLCREDITPERIOD
    2. Check BASICDUEDATEOFPYMT field
    3. Search for patterns like "30 days", "45 days" in bill text
    4. Fetch from vendor ledger master (CREDITPERIOD field)
    """
    # Method 1: Check BILLALLOCATIONS.LIST → BILLCREDITPERIOD
    bill_alloc = voucher.find('BILLALLOCATIONS.LIST')
    if bill_alloc:
        bill_credit = bill_alloc.find('BILLCREDITPERIOD')
        if bill_credit and bill_credit.text:
            return bill_credit.text.strip()
    
    # Method 2: Check BASICDUEDATEOFPYMT
    due_date = voucher.find('BASICDUEDATEOFPYMT')
    if due_date and due_date.text:
        return due_date.text.strip()
    
    # Method 3: Search for payment term patterns in entire bill text
    # Pattern: "30 days", "45 days", "net 30", etc.
    voucher_text = str(voucher)
    patterns = [
        r'(\d+)\s*days?',  # "30 days" or "30 day"
        r'net\s*(\d+)',     # "net 30"
        r'(\d+)\s*days?\s*credit',  # "30 days credit"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, voucher_text, re.IGNORECASE)
        if match:
            days = match.group(1)
            return f"{days} Days"
    
    # Method 4: Fetch from vendor ledger master
    vendor_terms = fetch_vendor_payment_terms(party_name)
    if vendor_terms:
        return vendor_terms
    
    return ""

def get_ledger_map_from_tally():
    """Builds a map that traces custom groups back to Sundry Creditors (Vendors)."""
    # 1. Fetch all Groups to build the 'Family Tree'
    group_xml = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Accounts</REPORTNAME>
    <STATICVARIABLES><ACCOUNTTYPE>Groups</ACCOUNTTYPE><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    
    children_map = defaultdict(list)
    try:
        res = requests.post(TALLY_URL, data=group_xml, timeout=15)
        soup = BeautifulSoup(res.content, 'lxml-xml')
        for g in soup.find_all('GROUP'):
            name = g.get('NAME', '').strip()
            parent = g.find('PARENT').text.strip() if g.find('PARENT') else ""
            if name: children_map[parent].append(name)
    except: pass

    # Recursive function to find ALL children/grandchildren of a group
    def get_all_subgroups(group_name, visited=None):
        if visited is None: visited = set()
        if group_name in visited: return set()
        visited.add(group_name)
        results = {group_name}
        for child in children_map.get(group_name, []):
            results.update(get_all_subgroups(child, visited))
        return results

    # Get all vendor groups (Sundry Creditors)
    creditor_groups = get_all_subgroups("Sundry Creditors")

    # 2. Fetch all Ledgers and map them
    ledger_xml = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Ledgers</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    
    l_map = {}
    try:
        res = requests.post(TALLY_URL, data=ledger_xml, timeout=15)
        soup = BeautifulSoup(res.content, 'lxml-xml')
        for l in soup.find_all('LEDGER'):
            name = l.get('NAME', '').strip()
            parent = l.find('PARENT').text.strip() if l.find('PARENT') else ""
            if parent in creditor_groups: l_map[name] = "(vendors)"
            else: l_map[name] = "(others)"
    except: pass
    return l_map

def fetch_tally_bills(bill_number="11"):
    """Fetch a specific bill by voucher number from Tally"""
    ledger_map = get_ledger_map_from_tally()
    
    print(f"[TALLY] Fetching bill with voucher number: {bill_number}...")
    
    # Use specific voucher type to narrow search (like invoice.py does)
    # Common bill voucher types: "Purchase", "Purchase Invoice", "Bill"
    # Adjust the VOUCHERTYPENAME based on your Tally setup
    xml_request = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
    <SVFROMDATE>20250401</SVFROMDATE><SVTODATE>20250430</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        print(f"[TALLY] Searching Purchase vouchers in April 2025...")
        response = requests.post(TALLY_URL, data=xml_request, timeout=30)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        # Get all vouchers and filter by bill number
        all_vouchers = soup.find_all('VOUCHER')
        print(f"[TALLY] Total Purchase vouchers found: {len(all_vouchers)}")
        
        vouchers = []
        for v in all_vouchers:
            v_no = v.find('VOUCHERNUMBER')
            if v_no and v_no.text.strip() == bill_number:
                vouchers.append(v)
                print(f"[TALLY]  Found bill #{bill_number}")
                break
        
        if not vouchers:
            print(f"[ERROR] Bill #{bill_number} not found in Purchase vouchers!")
            print(f"[ERROR] Please check:")
            print(f"  1. Bill number is correct")
            print(f"  2. Bill is in April 2025 date range")
            print(f"  3. Voucher type is 'Purchase' (adjust VOUCHERTYPENAME if different)")
            return []

        bill_data = []
        for idx, v in enumerate(vouchers, 1):
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get vendor from PARTYNAME field
            vendor_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            
            # Get Purchase Order Number
            po_number = v.find('BASICPURCHASEORDERNO').text if v.find('BASICPURCHASEORDERNO') else ""
            
            # Get Reference Number (Vendor Invoice Number)
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            
            # Get Vendor Address
            vendor_address = []
            vendor_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if vendor_addr_list:
                for addr in vendor_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        vendor_address.append(addr.text.strip())
            
            # Get Payment Terms using hierarchical method
            payment_terms = get_payment_terms_hierarchical(v, vendor_name)
            
            # Get Purchase Ledger using HIERARCHY METHOD
            purchase_ledger = ""
            purchase_ledger_from_item = ""
            
            # First, try to get purchase ledger from inventory entries
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                # Check if there's a ledger associated with this item
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    purchase_ledger_from_item = item_ledger.text.strip()
                    break
            
            # Method 2: If not found in items, find the ledger with LARGEST NEGATIVE amount
            if not purchase_ledger_from_item:
                max_negative_amount = 0
                for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                    name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amt = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0
                    
                    # Skip vendor ledger, tax ledgers, and rounding off / discount
                    name_lower = name.lower()
                    if name == vendor_name:  # Skip vendor
                        continue
                    if 'cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower or 'vat' in name_lower or 'gst' in name_lower or 'tax' in name_lower or 'duties' in name_lower:  # Skip taxes
                        continue
                    if 'round' in name_lower or 'rounding' in name_lower or 'discount' in name_lower:  # Skip rounding / discount
                        continue
                    
                    # Find the ledger with largest negative amount
                    if amt < max_negative_amount:
                        max_negative_amount = amt
                        purchase_ledger = name
            else:
                purchase_ledger = purchase_ledger_from_item
            
            # Get line items
            line_items = []
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_name = item.find('STOCKITEMNAME').text.strip() if item.find('STOCKITEMNAME') else ""
                
                # Get quantity
                qty_tag = item.find('ACTUALQTY') or item.find('BILLEDQTY')
                quantity = qty_tag.text.strip() if qty_tag else "0"
                
                # Get rate - handle currency conversion strings
                rate_tag = item.find('RATE')
                if rate_tag and rate_tag.text:
                    rate_text = rate_tag.text.split('/')[0].strip()
                    # Extract only numeric part (handle currency symbols and conversion strings)
                    numbers = re.findall(r'[-\d.]+', rate_text)
                    if numbers:
                        # Use the last number (usually the converted amount)
                        rate = float(numbers[-1])
                    else:
                        rate = 0.0
                else:
                    rate = 0.0
                
                # Get discount
                discount_tag = item.find('DISCOUNT')
                discount = discount_tag.text.strip() if discount_tag else "0"
                
                # Get amount - handle currency conversion strings
                amount_tag = item.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    # Extract only numeric part (handle currency symbols and conversion strings)
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    if numbers:
                        # Use the last number (usually the converted amount)
                        amount = float(numbers[-1])
                    else:
                        amount = 0.0
                else:
                    amount = 0.0
                
                # Get reporting tags (Category and Cost Centre)
                category = ""
                cost_centre = ""
                cat_alloc = item.find('CATEGORYALLOCATIONS.LIST')
                if cat_alloc:
                    category = cat_alloc.find('CATEGORY').text if cat_alloc.find('CATEGORY') else ""
                    cc_list = cat_alloc.find('COSTCENTREALLOCATIONS.LIST')
                    if cc_list:
                        cost_centre = cc_list.find('NAME').text if cc_list.find('NAME') else ""
                
                line_items.append({
                    "item_name": item_name,
                    "quantity": quantity,
                    "rate": rate,
                    "discount": discount,
                    "amount": abs(amount),
                    "category": category,
                    "cost_centre": cost_centre
                })
            
            # Get tax details from LEDGERENTRIES.LIST (ALL TAX TYPES)
            taxes = []
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                # Get amount - handle currency conversion strings
                amount_tag = entry.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    if numbers:
                        amt = float(numbers[-1])
                    else:
                        amt = 0.0
                else:
                    amt = 0.0
                
                # Check for ANY tax ledger (CGST, SGST, IGST, VAT, GST, etc.)
                name_lower = name.lower()
                if ('cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower or 'gst' in name_lower or 'tax' in name_lower or 'duties' in name_lower):
                    rate = ""
                    match = re.search(r'(\d+(?:\.\d+)?)\s*%', name)
                    if not match:
                        match = re.search(r'(?:cgst|sgst|igst|gst|tax)\s*@?\s*(\d+(?:\.\d+)?)', name, re.IGNORECASE)
                    if match:
                        rate = match.group(1)
                    
                    tax_type = "CGST" if 'cgst' in name_lower else ("SGST" if 'sgst' in name_lower else ("IGST" if 'igst' in name_lower else "GST"))
                    taxes.append({
                        "tax_name": name,
                        "tax_type": tax_type,
                        "tax_rate": rate,
                        "tax_amount": abs(amt)
                    })
            
            # Get rounding off
            rounding_off = 0.0
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                # Get amount - handle currency conversion strings
                amount_tag = entry.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    if numbers:
                        amt = float(numbers[-1])
                    else:
                        amt = 0.0
                else:
                    amt = 0.0
                
                if 'round' in name.lower() or 'r/o' in name.lower():
                    rounding_off = amt
                    break
            
            bill_data.append({
                "date": v_date,
                "bill_number": v_no,
                "vendor_name": vendor_name,
                "po_number": po_number,
                "reference_number": reference_number,
                "vendor_address": vendor_address,
                "payment_terms": payment_terms,
                "purchase_ledger": purchase_ledger,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": rounding_off,
                "narration": narration if narration else ""
            })
        
        return bill_data
    except Exception as e:
        print(f"Error fetching bills from Tally: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_zoho_contacts(token=None, use_cache=True, force_refresh=False):
    """Fetch all VENDOR / party contacts from Zoho Books with SQLite DB caching."""
    if not token:
        token = get_access_token()
    creds = _get_creds()
    org_id = creds.get("org_id", "")

    # Try SQLite DB cache first
    if use_cache and not force_refresh:
        cached = get_zoho_contacts_from_cache()
        if cached:
            return cached

    print("    Fetching contacts from Zoho Books API...")
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org_id, "per_page": 200}
    
    contact_map = {}
    page = 1
    total_contacts = 0
    
    try:
        while True:
            params["page"] = page
            res = requests.get(f"{creds['base_url']}/contacts", headers=headers, params=params)
            if res.status_code == 200 and res.json().get("code") == 0:
                contacts = res.json().get("contacts", [])
                if not contacts:
                    break
                
                for c in contacts:
                    c_name_lower = c.get("contact_name", "").lower().strip()
                    c_type = c.get("contact_type", "")
                    if c_name_lower in contact_map:
                        if contact_map[c_name_lower].get("contact_type") == "vendor" and c_type != "vendor":
                            continue
                    contact_map[c_name_lower] = {
                        "contact_id": c.get("contact_id"),
                        "contact_name": c.get("contact_name"),
                        "contact_type": c_type,
                        "original_name": c.get("contact_name"),
                        "place_of_contact": c.get("place_of_contact", ""),
                        "place_of_contact_formatted": c.get("place_of_contact_formatted", ""),
                        "gst_no": c.get("gst_no", ""),
                        "gst_treatment": c.get("gst_treatment", "")
                    }
                    total_contacts += 1
                
                page_context = res.json().get("page_context", {})
                if not page_context.get("has_more_page", False):
                    break
                page += 1
            else:
                print(f"Error fetching contacts on page {page}: {res.status_code}")
                break
        
        print(f"    Fetched {total_contacts} contacts across {page} page(s)")
        if contact_map:
            save_zoho_contacts_to_cache(contact_map)
        return contact_map
    except Exception as e:
        print(f"Error fetching contacts: {e}")
    return {}

def find_or_create_contact(token, contact_map, contact_name):
    """Find existing vendor contact using FUZZY MATCHING from cached contact map - NO AUTO-CREATE"""
    if not contact_name:
        return None
    contact_key = str(contact_name).lower().strip()
    
    # Exact match first
    if contact_key in contact_map:
        return contact_map[contact_key]
    
    # Fuzzy matching to find similar names
    best_match = None
    best_score = 0
    best_name = ""
    
    for existing_name, contact_data in contact_map.items():
        score = fuzz.ratio(contact_key, existing_name)
        if score > best_score:
            best_score = score
            best_match = contact_data
            best_name = existing_name
    
    # If similarity is >= 75%, use the match
    if best_match and best_score >= 75:
        print(f"  [FUZZY MATCH] Found vendor: '{best_match.get('contact_name') or best_match.get('original_name')}' for '{contact_name}' (Score: {best_score}%)")
        contact_map[contact_key] = best_match
        return best_match
    
    print(f"\n  [ERROR] Vendor '{contact_name}' not found in Zoho Books cache!")
    if best_match:
        print(f"  [SUGGESTION] Closest match: '{best_match.get('contact_name') or best_match.get('original_name')}' (Score: {best_score}%)")
    print(f"  [ACTION REQUIRED] Please create this vendor in Zoho Books / sync contacts and run again.\n")
    return None

def get_zoho_items(token=None, use_cache=True, force_refresh=False):
    """Fetch all items from Zoho Books with SQLite DB caching"""
    if not token:
        token = get_access_token()
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    if use_cache and not force_refresh and database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
        cached = database_manager.get_zoho_master_cache('items', expected_org_id=org_id)
        if cached is not None:
            return cached

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org_id, "per_page": 200}
    all_items = {}
    page = 1
    total_items = 0
    try:
        while True:
            params["page"] = page
            res = requests.get(f"{creds['base_url']}/items", headers=headers, params=params)
            if res.status_code == 200 and res.json().get("code") == 0:
                items = res.json().get("items", [])
                if not items:
                    break
                for item in items:
                    item_name = str(item.get("name", "")).lower().strip()
                    all_items[item_name] = {
                        "item_id": item.get("item_id"),
                        "name": item.get("name"),
                        "product_type": item.get("product_type") or item.get("item_type") or "goods",
                        "sku": item.get("sku", ""),
                        "rate": item.get("rate", 0),
                        "tags": item.get("tags", [])
                    }
                    total_items += 1
                page_context = res.json().get("page_context", {})
                if not page_context.get("has_more_page", False):
                    break
                page += 1
            else:
                break
        print(f"    Fetched {total_items} items across {page} page(s)")
        if all_items and database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
            database_manager.save_zoho_master_cache('items', all_items, org_id=org_id)
        return all_items
    except Exception as e:
        print(f"Error fetching items: {e}")
    return {}

def find_item_in_zoho(item_name, item_map):
    """Find existing item in Zoho item_map using exact match then fuzzy match - NO AUTO-CREATE"""
    if not item_name or not item_map:
        return None
    k = str(item_name).lower().strip()
    if k in item_map:
        return item_map[k]
    
    # Try normalized alphanumeric match
    k_clean = re.sub(r'[^a-z0-9]', '', k)
    for ex_name, it_data in item_map.items():
        ex_clean = re.sub(r'[^a-z0-9]', '', ex_name)
        if k_clean and k_clean == ex_clean:
            return it_data
            
    # Fuzzy match >= 85%
    best_match = None
    best_score = 0
    for ex_name, it_data in item_map.items():
        score = fuzz.ratio(k, ex_name)
        if score > best_score:
            best_score = score
            best_match = it_data
    if best_match and best_score >= 85:
        return best_match
    return None

def get_zoho_accounts(token=None, use_cache=True, force_refresh=False):
    """Fetch all accounts from Zoho Books with SQLite DB caching"""
    if not token:
        token = get_access_token()
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    if use_cache and not force_refresh and database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
        cached = database_manager.get_zoho_master_cache('accounts', expected_org_id=org_id)
        if cached is not None:
            return cached

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org_id}
    
    try:
        res = requests.get(f"{creds['base_url']}/chartofaccounts", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            acct_map = {a["account_name"].lower(): a["account_id"] for a in res.json().get("chartofaccounts", [])}
            if database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
                database_manager.save_zoho_master_cache('accounts', acct_map, org_id=org_id)
            return acct_map
    except Exception as e:
        print(f"Error fetching accounts: {e}")
    return {}

def get_zoho_tags(token=None, use_cache=True, force_refresh=False):
    """Fetch all tags from Zoho Books using reporting_tags API with SQLite DB caching"""
    if not token:
        token = get_access_token()
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    if use_cache and not force_refresh and database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
        cached = database_manager.get_zoho_master_cache('tags', expected_org_id=org_id)
        if cached is not None:
            return cached

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org_id}
    
    tag_map = {}
    try:
        res = requests.get(f"{creds['base_url']}/settings/tags", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            categories = res.json().get("reporting_tags", [])
            for category in categories:
                tag_id = category.get("tag_id")
                tag_name = category.get("tag_name")
                detail_res = requests.get(f"{creds['base_url']}/settings/tags/{tag_id}", headers=headers, params=params)
                if detail_res.status_code == 200:
                    detail_data = detail_res.json()
                    tag_obj = detail_data.get("tag", detail_data.get("reporting_tag", {}))
                    options = tag_obj.get("tag_options", [])
                    for option in options:
                        option_name = option.get("tag_option_name", "")
                        option_id = option.get("tag_option_id")
                        if option_name and option_id:
                            tag_map[option_name.lower()] = {
                                "tag_id": tag_id,
                                "tag_option_id": option_id,
                                "tag_name": tag_name,
                                "tag_option_name": option_name
                            }
            if database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
                database_manager.save_zoho_master_cache('tags', tag_map, org_id=org_id)
    except Exception as e:
        print(f"  [WARNING] Error fetching tags: {e}")
    return tag_map

def get_zoho_payment_terms_list(token=None, use_cache=True, force_refresh=False):
    """Fetch all payment terms from Zoho Books with SQLite DB caching"""
    if not token:
        token = get_access_token()
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    if use_cache and not force_refresh and database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
        cached = database_manager.get_zoho_master_cache('payment_terms', expected_org_id=org_id)
        if cached is not None:
            return cached

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org_id}
    
    try:
        res = requests.get(f"{creds['base_url']}/settings/paymentterms", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            terms_data = res.json().get("data", {})
            terms_list = terms_data.get("payment_terms", [])
            terms_map = {}
            for term in terms_list:
                term_label = term.get("payment_terms_label", "")
                term_id = term.get("payment_terms_id")
                if term_label and term_id:
                    terms_map[term_label.lower()] = term_id
            if database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
                database_manager.save_zoho_master_cache('payment_terms', terms_map, org_id=org_id)
            return terms_map
    except Exception as e:
        print(f"  [WARNING] Error fetching payment terms: {e}")
    return {}

def map_payment_terms(tally_terms, zoho_terms_map):
    """Map Tally payment terms to Zoho Books payment terms ID"""
    if not tally_terms or not zoho_terms_map:
        return None
    
    tally_terms_lower = tally_terms.lower().strip()
    
    if tally_terms_lower in zoho_terms_map:
        return zoho_terms_map[tally_terms_lower]
    
    numbers = re.findall(r'\d+', tally_terms)
    if numbers:
        days = numbers[0]
        variations = [
            f"net {days}",
            f"{days} days",
            f"net{days}",
        ]
        for variation in variations:
            if variation in zoho_terms_map:
                return zoho_terms_map[variation]
    
    if "due on receipt" in zoho_terms_map:
        return zoho_terms_map["due on receipt"]
    
    return None

def get_zoho_taxes(token=None, use_cache=True, force_refresh=False):
    """Fetch all tax rates and tax groups from Zoho Books with SQLite DB caching"""
    if not token:
        token = get_access_token()
    creds = _get_creds()
    org_id = creds.get("org_id", "")
    if use_cache and not force_refresh and database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
        cached = database_manager.get_zoho_master_cache('taxes', expected_org_id=org_id)
        if cached is not None:
            if isinstance(cached, dict):
                return cached
            elif isinstance(cached, list):
                tax_map = {}
                for t in cached:
                    if not isinstance(t, dict): continue
                    tax_name = t.get("tax_name") or t.get("tax_group_name") or ""
                    tax_id = t.get("tax_id") or t.get("tax_group_id")
                    tax_percentage = t.get("tax_percentage") or t.get("tax_group_percentage") or 0.0
                    if tax_name and tax_id:
                        tax_map[tax_name.lower()] = {
                            "tax_id": tax_id,
                            "tax_name": tax_name,
                            "tax_percentage": float(tax_percentage or 0),
                            "is_group": t.get("is_group", False)
                        }
                return tax_map

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": org_id}
    
    tax_map = {}
    
    try:
        # 1. Fetch individual tax rates
        res = requests.get(f"{creds['base_url']}/settings/taxes", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            taxes = res.json().get("taxes", [])
            for tax in taxes:
                tax_name = tax.get("tax_name", "")
                tax_id = tax.get("tax_id")
                tax_percentage = tax.get("tax_percentage", 0)
                if tax_name and tax_id:
                    tax_map[tax_name.lower()] = {
                        "tax_id": tax_id,
                        "tax_name": tax_name,
                        "tax_percentage": float(tax_percentage or 0),
                        "is_group": False
                    }
                    
        # 2. Fetch tax groups (e.g., IGST 18%, GST 18%)
        res_groups = requests.get(f"{creds['base_url']}/settings/taxgroups", headers=headers, params=params)
        if res_groups.status_code == 200 and res_groups.json().get("code") == 0:
            tax_groups = res_groups.json().get("tax_groups", [])
            for group in tax_groups:
                group_name = group.get("tax_group_name", "")
                group_id = group.get("tax_group_id")
                group_percentage = group.get("tax_group_percentage", 0)
                if group_name and group_id:
                    tax_map[group_name.lower()] = {
                        "tax_id": group_id,
                        "tax_name": group_name,
                        "tax_percentage": float(group_percentage or 0),
                        "is_group": True
                    }
        if tax_map and database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
            database_manager.save_zoho_master_cache('taxes', tax_map, org_id=org_id)
    except Exception as e:
        print(f"  [WARNING] Error fetching taxes: {e}")
    
    return tax_map

def refresh_all_zoho_masters(token=None):
    """
    One-click master refresh for Bills module: Fetches all master data from Zoho Books ONCE
    and caches it into SQLite DB (zoho_masters_cache and zoho_contacts tables).
    Includes: Contacts/Vendors, Chart of Accounts, Taxes & Tax Groups, Payment Terms, Reporting Tags.
    """
    if not token:
        token = get_access_token()
    results = {}
    print("\n" + "="*70)
    print(" REFRESHING ALL ZOHO MASTERS TO SQLITE DB CACHE (BILLS MODULE)...")
    print("="*70)
    
    try:
        contacts = get_zoho_contacts(token, force_refresh=True)
        results["contacts"] = len(contacts) if isinstance(contacts, dict) else 0
        print(f"   Contacts cached: {results['contacts']}")
    except Exception as e:
        results["contacts_error"] = str(e)
        print(f"   Contacts error: {e}")

    try:
        accounts = get_zoho_accounts(token, force_refresh=True)
        results["accounts"] = len(accounts) if isinstance(accounts, dict) else 0
        print(f"   Accounts cached: {results['accounts']}")
    except Exception as e:
        results["accounts_error"] = str(e)
        print(f"   Accounts error: {e}")

    try:
        taxes = get_zoho_taxes(token, force_refresh=True)
        results["taxes"] = len(taxes) if isinstance(taxes, (list, dict)) else 0
        print(f"   Taxes cached: {results['taxes']}")
    except Exception as e:
        results["taxes_error"] = str(e)
        print(f"   Taxes error: {e}")

    try:
        terms = get_zoho_payment_terms_list(token, force_refresh=True)
        results["payment_terms"] = len(terms) if isinstance(terms, dict) else 0
        print(f"   Payment Terms cached: {results['payment_terms']}")
    except Exception as e:
        results["payment_terms_error"] = str(e)
        print(f"   Payment Terms error: {e}")

    try:
        items = get_zoho_items(token, force_refresh=True)
        results["items"] = len(items) if isinstance(items, dict) else 0
        print(f"   Items cached: {results['items']}")
    except Exception as e:
        results["items_error"] = str(e)
        print(f"   Items error: {e}")

    try:
        tags = get_zoho_tags(token, force_refresh=True)
        results["tags"] = len(tags) if isinstance(tags, dict) else 0
        print(f"   Tags cached: {results['tags']}")
    except Exception as e:
        results["tags_error"] = str(e)
        print(f"   Tags error: {e}")

    print("="*70)
    print(" REFRESH ALL ZOHO MASTERS COMPLETE!")
    print("="*70 + "\n")
    return results

def calculate_total_tax_rate(taxes, subtotal=0):
    """Calculate total tax rate from CGST + SGST or IGST or Pre-GST taxes"""
    if isinstance(taxes, str):
        try:
            taxes = json.loads(taxes)
        except:
            taxes = []

    total_rate = 0.0
    total_tax_amt = 0.0
    has_multiple_taxes = len(taxes) > 1
    
    for tax in taxes:
        if not isinstance(tax, dict): continue
        tax_amt = float(tax.get("tax_amount", 0) or 0)
        total_tax_amt += tax_amt
        
        # 1. Check explicit tax_rate property
        rate_val = tax.get("tax_rate")
        if rate_val:
            try:
                total_rate += float(rate_val)
                continue
            except:
                pass
        
        # 2. Extract percentage from ledger name e.g. "Input VAT @ 5.5%" or "Excise Duty 12.5%"
        tax_name = tax.get("tax_name", "")
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', tax_name)
        if not match:
            match = re.search(r'(?:cgst|sgst|igst|gst|tax|vat|cst|excise)\s*@?\s*(\d+(?:\.\d+)?)', tax_name, re.IGNORECASE)
        if match:
            try:
                total_rate += float(match.group(1))
            except:
                pass

    # Dynamic check: if total_tax_amt > 0 and subtotal > 0
    if subtotal > 0 and total_tax_amt > 0:
        actual_effective_rate = round((total_tax_amt / float(subtotal)) * 100.0, 2)
        # If cascading tax (like Excise Duty 12.5% + VAT 5.5% where total is 28.82% instead of 18.0%)
        # or if nominal sum is 0
        if has_multiple_taxes or abs(actual_effective_rate - total_rate) > 0.5 or total_rate == 0.0:
            total_rate = actual_effective_rate

    return round(total_rate, 2)

def find_or_create_bill_tax(token, bill_data, tax_map):
    """
    Dynamically find or create the exact matching tax in Zoho Books for a Bill.
    Distinguishes between Pre-GST (date < 2017-07-01 or Excise/CST/VAT) and Post-GST transactions.
    """
    line_items = bill_data.get("line_items") or []
    if isinstance(line_items, str):
        try: line_items = json.loads(line_items)
        except: line_items = []

    # Calculate goods subtotal (excluding freight/charges) if tax was applied only to goods
    goods_subtotal = sum(
        float(it.get('amount') or 0) for it in line_items 
        if not (it.get("is_additional_charge") or any(k in str(it.get("item_name") or it.get("name") or "").lower() for k in ["freight", "fright", "transport", "transpot", "round"]))
    )
    raw_subtotal = float(bill_data.get("subtotal") or 0)
    tax_base_subtotal = goods_subtotal if goods_subtotal > 0 else raw_subtotal

    taxes = bill_data.get("taxes") or []
    if isinstance(taxes, str):
        try: taxes = json.loads(taxes)
        except: taxes = []

    total_tax_rate = calculate_total_tax_rate(taxes, tax_base_subtotal)

    # Check if this bill is Pre-GST (strictly before July 1, 2017)
    tally_date = str(bill_data.get("date", "")).replace("-", "")
    is_pre_gst = (tally_date < "20170701" and len(tally_date) >= 8)

    is_igst_transaction = any(t.get("tax_type") == "IGST" or 'igst' in str(t.get("tax_name", "")).lower() for t in taxes)

    # 1. Match an existing tax in Zoho Books
    if is_pre_gst:
        if total_tax_rate <= 0 and not taxes:
            return None
        # Pre-GST: Look ONLY for non-GST taxes (Excise, CST, VAT) with matching percentage
        for t_name, t_val in tax_map.items():
            t_perc = float(t_val.get("tax_percentage", 0))
            is_gst_rate = t_name.startswith("gst") or t_name.startswith("igst") or "gst" in t_name
            if not is_gst_rate and abs(t_perc - total_tax_rate) < 0.05:
                return t_val
    else:
        # Post-GST (from 01-07-2017 onwards): Strictly use standard Indian GST slabs (0%, 5%, 12%, 18%, 28%)
        if 0.0 < total_tax_rate <= 2.0:
            target_gst_rate = 0.0
        elif 2.0 < total_tax_rate <= 8.0:
            target_gst_rate = 5.0
        elif 8.0 < total_tax_rate <= 15.0:
            target_gst_rate = 12.0
        elif 15.0 < total_tax_rate <= 22.0:
            target_gst_rate = 18.0
        elif total_tax_rate > 22.0:
            target_gst_rate = 28.0
        else:
            target_gst_rate = 0.0

        if target_gst_rate > 0:
            # 1. Strictly filter for pure GST/IGST groups (MUST NOT contain vat, excise, or cst)
            candidates = []
            for t_name, t_val in tax_map.items():
                t_name_l = str(t_name).lower()
                # Exclude all Pre-GST taxes completely
                if any(x in t_name_l for x in ["vat", "excise", "cst", "entry"]):
                    continue
                t_perc = float(t_val.get("tax_percentage", 0))
                if abs(t_perc - target_gst_rate) < 0.05:
                    candidates.append((t_name_l, t_val))
            
            # Match interstate (IGST) vs intrastate (CGST + SGST)
            for t_name_l, t_val in candidates:
                if is_igst_transaction and ("igst" in t_name_l or "inter" in t_name_l):
                    return t_val
                elif not is_igst_transaction and "igst" not in t_name_l and ("cgst" in t_name_l or "sgst" in t_name_l or "gst" in t_name_l):
                    return t_val
            
            if candidates:
                return candidates[0][1]

            # Direct fallback by exact rate match if not found above
            for t_name, t_val in tax_map.items():
                t_name_l = str(t_name).lower()
                if not any(x in t_name_l for x in ["vat", "excise", "cst"]) and "gst" in t_name_l:
                    t_perc = float(t_val.get("tax_percentage", 0))
                    if abs(t_perc - target_gst_rate) < 0.05:
                        return t_val
        else:
            zero_tax_key = "igst0" if is_igst_transaction else "gst0"
            if zero_tax_key in tax_map:
                return tax_map[zero_tax_key]
            for t_name, t_val in tax_map.items():
                t_name_l = str(t_name).lower()
                if not any(x in t_name_l for x in ["vat", "excise", "cst"]):
                    if float(t_val.get("tax_percentage", 0)) == 0.0:
                        if (is_igst_transaction and "igst" in t_name_l) or (not is_igst_transaction and "igst" not in t_name_l):
                            return t_val

    # 2. If pre-GST tax with total_tax_rate > 0 is NOT found in Zoho Books, auto-create it via Zoho API!
    if is_pre_gst and total_tax_rate > 0:
        tax_names = [f"{t.get('tax_name', 'Tax')}" for t in taxes if t.get('tax_name')]
        tax_display_name = f"{' + '.join(tax_names)} [{total_tax_rate}%]" if tax_names else f"Pre-GST Tax {total_tax_rate}%"
        
        creds = _get_creds()
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        params = {"organization_id": creds["org_id"]}
        
        tax_payload = {
            "tax_name": tax_display_name,
            "tax_percentage": total_tax_rate,
            "tax_type": "tax"
        }
        print(f"  [AUTO-TAX] Creating Pre-GST Tax in Zoho Books: '{tax_display_name}' ({total_tax_rate}%)...")
        try:
            res = requests.post(f"{creds['base_url']}/settings/taxes", headers=headers, params=params, json=tax_payload)
            if res.status_code in [200, 201] and res.json().get("code") == 0:
                created_tax = res.json().get("tax", {})
                new_tax_info = {
                    "tax_id": created_tax.get("tax_id"),
                    "tax_name": created_tax.get("tax_name", tax_display_name),
                    "tax_percentage": float(created_tax.get("tax_percentage", total_tax_rate)),
                    "is_group": False
                }
                tax_map[tax_display_name.lower()] = new_tax_info
                if database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
                    database_manager.save_zoho_master_cache('taxes', tax_map, org_id=creds["org_id"])
                print(f"  [AUTO-TAX SUCCESS] Created Zoho Tax: '{tax_display_name}' [ID: {new_tax_info['tax_id']}]")
                return new_tax_info
            else:
                print(f"  [AUTO-TAX FAILED] {res.text}")
        except Exception as e:
            print(f"  [AUTO-TAX ERROR] {e}")

    # 3. Post-GST fallback (strictly GST 18% or GST 0%)
    if not is_pre_gst:
        target_default = 18.0 if (total_tax_rate > 0 or len(taxes) > 0) else 0.0
        for t_name, t_val in tax_map.items():
            t_name_l = str(t_name).lower()
            if not any(x in t_name_l for x in ["vat", "excise", "cst"]) and "gst" in t_name_l:
                if float(t_val.get("tax_percentage", 0)) == target_default:
                    if (is_igst_transaction and "igst" in t_name_l) or (not is_igst_transaction and "igst" not in t_name_l):
                        return t_val
        for t_name, t_val in tax_map.items():
            t_name_l = str(t_name).lower()
            if not any(x in t_name_l for x in ["vat", "excise", "cst"]):
                if float(t_val.get("tax_percentage", 0)) == target_default:
                    return t_val

    return None

def create_zoho_bill(token, bill_data, contact_map, account_map, payment_terms_map, tax_map, tag_map, item_map=None):
    """Create a bill in Zoho Books - returns success status and error details"""
    if item_map is None:
        item_map = get_zoho_items(token, use_cache=True)

    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {
        "organization_id": creds["org_id"],
        "ignore_auto_number_generation": "true"  #  Use Tally bill number
    }
    
    print(f"\n{'='*100}")
    print(f"[BILL] Processing Bill #{bill_data['bill_number']} - Date: {bill_data['date']}")
    print(f"{'='*100}")
    
    # Find or create vendor
    vendor_info = find_or_create_contact(token, contact_map, bill_data["vendor_name"])
    if not vendor_info:
        error_msg = f"Failed to find or create vendor: {bill_data['vendor_name']}"
        print(f"  [ERROR] {error_msg}")
        return {"success": False, "error": error_msg}
    
    vendor_name_disp = vendor_info.get("contact_name") or vendor_info.get("original_name") or bill_data.get("vendor_name", "")
    print(f"  [VENDOR] {vendor_name_disp} (ID: {vendor_info.get('contact_id')})")
    
    # Display additional info
    if bill_data.get('po_number'):
        print(f"  [PO] {bill_data['po_number']}")
    if bill_data.get('reference_number'):
        print(f"  [REF] {bill_data['reference_number']}")
    if bill_data.get('payment_terms'):
        print(f"  [TERMS] {bill_data['payment_terms']}")
    
    # Build line items
    zoho_line_items = []
    
    # Safely normalize tax_map
    if isinstance(tax_map, list):
        normalized_tax_map = {}
        for t in tax_map:
            if not isinstance(t, dict): continue
            tax_name = t.get("tax_name") or t.get("tax_group_name") or ""
            tax_id = t.get("tax_id") or t.get("tax_group_id")
            tax_percentage = t.get("tax_percentage") or t.get("tax_group_percentage") or 0.0
            if tax_name and tax_id:
                normalized_tax_map[tax_name.lower()] = {
                    "tax_id": tax_id,
                    "tax_name": tax_name,
                    "tax_percentage": float(tax_percentage or 0),
                    "is_group": t.get("is_group", False)
                }
        tax_map = normalized_tax_map
    elif not isinstance(tax_map, dict):
        tax_map = {}

    # Get dynamic tax info (handles pre-GST and post-GST)
    tax_info = find_or_create_bill_tax(token, bill_data, tax_map)
    if tax_info:
        print(f"  [TAX] Using Zoho tax: {tax_info['tax_name']} ({tax_info.get('tax_percentage', 0)}%) [ID: {tax_info.get('tax_id')}]")
    
    # Master Account IDs configured for Bills Module
    ACCOUNT_INVENTORY_ASSET_ID        = "3962933000000000626"  # Inventory Asset (All items / goods)
    ACCOUNT_TRANSPORTATION_CHARGES_ID = "3962933000000044007"  # Transportation / Transpotation Charges
    ACCOUNT_FREIGHT_CHARGES_ID        = "3962933000000059018"  # Freight / Fright Charges
    ACCOUNT_ROUND_OFF_ID              = "3962933000000053017"  # Round Off / Rounding Off

    # Pre-calculate goods total to know if tax was only on goods in Tally
    all_items = bill_data.get("line_items", [])
    if isinstance(all_items, str):
        try: all_items = json.loads(all_items)
        except: all_items = []
    
    goods_subtotal_calc = sum(
        float(it.get('amount') or 0) for it in all_items 
        if not (it.get("is_additional_charge") or any(k in str(it.get("item_name") or it.get("name") or "").lower() for k in ["freight", "fright", "transport", "transpot", "round"]))
    )
    all_taxes_list = bill_data.get("taxes", [])
    if isinstance(all_taxes_list, str):
        try: all_taxes_list = json.loads(all_taxes_list)
        except: all_taxes_list = []
    total_tax_amount_calc = sum(float(t.get("tax_amount", 0) or 0) for t in all_taxes_list)

    for item in all_items:
        item_name = str(item.get('item_name') or item.get('name') or 'Purchase Item').strip()
        item_name_lower = item_name.lower()

        is_round_off = any(k in item_name_lower for k in ["round off", "rounding off", "round-off", "rounding", "r/o"])
        is_transport = any(k in item_name_lower for k in ["transport", "transpotation", "transportation", "vehicle"])
        is_freight   = any(k in item_name_lower for k in ["freight", "fright", "cartage", "courier", "postage"])
        is_charge_item = is_round_off or is_transport or is_freight or item.get("is_additional_charge", False)

        matched_item_id = None
        product_type = "goods"

        if is_round_off:
            line_account_id = ACCOUNT_ROUND_OFF_ID
            line_account_name = "Round Off"
            product_type = "service"
        elif is_transport:
            line_account_id = ACCOUNT_TRANSPORTATION_CHARGES_ID
            line_account_name = "Transportation Charges"
            product_type = "service"
        elif is_freight or item.get("is_additional_charge"):
            line_account_id = ACCOUNT_FREIGHT_CHARGES_ID
            line_account_name = "Freight Charges"
            product_type = "service"
        else:
            # Regular Goods / Inventory Item
            line_account_id = ACCOUNT_INVENTORY_ASSET_ID
            line_account_name = "Inventory Asset"
            product_type = "goods"

            # Strict item lookup in Zoho Books (NO AUTO-CREATE)
            matched_item = find_item_in_zoho(item_name, item_map)
            if not matched_item:
                err_msg = f"Item '{item_name}' not found in Zoho Books! Please create this Goods item in Zoho Books first."
                print(f"  [ITEM ERROR] {err_msg}")
                return {"success": False, "error": err_msg}
            
            matched_item_id = matched_item["item_id"]
            print(f"  [ITEM MATCHED] '{item_name}' -> Zoho Item: '{matched_item['name']}' (ID: {matched_item_id})")

        print(f"  [ITEM] {item_name} - Qty: {item.get('quantity', 1)} @ Rs.{item.get('rate', 0)} -> Account: {line_account_name} ({line_account_id}) [Type: {product_type}]")
        if item.get('category') or item.get('cost_centre'):
            print(f"     [TAG] Category: {item.get('category', 'N/A')}, Cost Centre: {item.get('cost_centre', 'N/A')}")
        
        # Parse quantity to get numeric value
        qty_str = str(item['quantity']).split()[0] if item['quantity'] else "1"
        try:
            qty = float(qty_str)
        except:
            qty = 1.0
        
        # Parse discount
        try:
            discount = float(item['discount']) if item['discount'] and item['discount'] != '0' else 0
        except:
            discount = 0
        
        line_item = {
            "name": item_name,
            "description": item_name,
            "rate": item.get('rate', 0),
            "quantity": qty,
            "discount": discount,
            "account_id": line_account_id
        }
        if matched_item_id:
            line_item["item_id"] = matched_item_id
        
        # Add tax ID conditionally (only to goods, or charges if tax was explicitly on charges in Tally)
        if tax_info:
            if is_charge_item:
                expected_goods_tax = round(goods_subtotal_calc * (float(tax_info.get('tax_percentage', 0)) / 100.0), 2)
                if abs(total_tax_amount_calc - expected_goods_tax) <= 2.0 or is_round_off:
                    line_item["tax_id"] = ""
                    print(f"     [NO TAX ON CHARGE] '{item_name}' -> 0% tax (Tax in Tally applies to goods only)")
                else:
                    line_item["tax_id"] = tax_info["tax_id"]
            else:
                line_item["tax_id"] = tax_info["tax_id"]
        
        # Add reporting tags (Category and Cost Centre)
        tags = []
        if item.get('category'):
            category_tag = tag_map.get(str(item['category']).lower())
            if category_tag:
                tags.append({
                    "tag_id": category_tag["tag_id"],
                    "tag_option_id": category_tag["tag_option_id"]
                })
                print(f"     [TAG] Category: {item['category']}")
        
        if item.get('cost_centre'):
            cc_tag = tag_map.get(str(item['cost_centre']).lower())
            if cc_tag:
                tags.append({
                    "tag_id": cc_tag["tag_id"],
                    "tag_option_id": cc_tag["tag_option_id"]
                })
                print(f"     [TAG] Cost Centre: {item['cost_centre']}")
        
        if tags:
            line_item["tags"] = tags
        
        zoho_line_items.append(line_item)
    
    # Display taxes
    if all_taxes_list:
        print(f"\n  [TAX] Taxes:")
        for tax in all_taxes_list:
            if isinstance(tax, dict):
                print(f"     {tax.get('tax_type')} {tax.get('tax_rate')}%: Rs.{tax.get('tax_amount')}")
        total_disp_rate = tax_info.get("tax_percentage") if tax_info else calculate_total_tax_rate(all_taxes_list, bill_data.get("subtotal", 0))
        print(f"     Total Tax Rate: {total_disp_rate}%")
    
    # Display rounding off
    rounding_val = float(bill_data.get("rounding_off") or 0.0)
    if rounding_val != 0.0:
        print(f"  [ROUNDING] Rs.{abs(rounding_val)} -> Account ID: {ACCOUNT_ROUND_OFF_ID}")
    
    # Convert date format (YYYYMMDD -> YYYY-MM-DD)
    tally_date = bill_data["date"]
    zoho_date = f"{tally_date[:4]}-{tally_date[4:6]}-{tally_date[6:8]}"
    
    # Map payment terms
    payment_terms_id = map_payment_terms(bill_data.get("payment_terms", ""), payment_terms_map)
    
    # Build payload
    payload = {
        "vendor_id": vendor_info.get("contact_id"),
        "bill_number": bill_data["bill_number"],
        "reference_number": bill_data.get("reference_number", ""),
        "date": zoho_date,
        "line_items": zoho_line_items,
        "notes": str(bill_data.get("narration") or "")[:1000]
    }
    
    # Add payment terms if available
    if payment_terms_id:
        tally_terms = bill_data.get("payment_terms", "")
        numbers = re.findall(r'\d+', tally_terms)
        payload["payment_terms"] = int(numbers[0]) if numbers else 0
        print(f"  [PAYMENT TERMS APPLIED] Mapped '{tally_terms}' to ID: {payment_terms_id}")
    
    # Calculate exact total adjustment to match Tally total to the exact paisa
    zoho_calc_subtotal = sum(round(float(it.get("rate", 0)) * float(it.get("quantity", 1)) * (1.0 - float(it.get("discount", 0))/100.0), 2) for it in zoho_line_items)
    tax_perc_val = float(tax_info.get("tax_percentage", 0)) if tax_info else 0.0
    zoho_calc_tax = sum(round(float(it.get("rate", 0)) * float(it.get("quantity", 1)) * (1.0 - float(it.get("discount", 0))/100.0) * (tax_perc_val / 100.0), 2) for it in zoho_line_items if it.get("tax_id"))
    zoho_calc_total = round(zoho_calc_subtotal + zoho_calc_tax, 2)
    
    tally_target_total = float(bill_data.get("total_amount") or 0.0)
    if tally_target_total <= 0:
        tally_target_total = round(float(bill_data.get("subtotal", 0) or 0) + float(bill_data.get("tax_total", 0) or 0) + rounding_val, 2)
    
    calc_adjustment = round(tally_target_total - zoho_calc_total, 2)
    
    if abs(calc_adjustment) > 0.001 and abs(calc_adjustment) <= 50.0:
        payload["adjustment"] = calc_adjustment
        payload["adjustment_description"] = "Rounding Off / Tax Adjustment"
        print(f"  [AUTO-ADJUSTMENT] Applied Zoho Adjustment of Rs.{calc_adjustment} to match exact Tally total (Rs.{tally_target_total})")
    elif rounding_val != 0.0:
        payload["adjustment"] = round(rounding_val, 2)
        payload["adjustment_description"] = "Rounding Off"
    
    print(f"\n  [CREATE] Creating bill in Zoho Books...")
    print(f"  Payload: {json.dumps(payload, indent=2)}")
    
    try:
        res = requests.post(f"{creds['base_url']}/bills", headers=headers, params=params, json=payload)
        
        # Log full response for debugging
        with open("bill_response.log", "w") as f:
            f.write(f"Status Code: {res.status_code}\n")
            f.write(f"Response: {json.dumps(res.json(), indent=2)}\n")
        
        if res.status_code in [200, 201] and res.json().get("code") == 0:
            bill_id = res.json().get("bill", {}).get("bill_id", "N/A")
            print(f"  [SUCCESS] Bill created with ID: {bill_id}")
            return {"success": True, "bill_id": bill_id, "zoho_bill_id": bill_id}
        elif res.status_code == 400 and res.json().get("code") == 13011:
            print(f"  [EXISTS] Bill #{bill_data['bill_number']} already exists in Zoho Books for this vendor. Fetching ID and updating...")
            try:
                search_res = requests.get(f"{creds['base_url']}/bills", headers=headers, params={
                    "organization_id": creds["org_id"],
                    "bill_number": bill_data["bill_number"],
                    "vendor_id": vendor_info.get("contact_id")
                })
                if search_res.status_code == 200:
                    found_bills = search_res.json().get("bills", [])
                    if found_bills:
                        existing_bill_id = found_bills[0].get("bill_id")
                        print(f"  [UPDATING EXISTING] Updating Zoho Bill ID: {existing_bill_id}...")
                        update_res = requests.put(f"{creds['base_url']}/bills/{existing_bill_id}", headers=headers, params=params, json=payload)
                        if update_res.status_code in [200, 201] and update_res.json().get("code") == 0:
                            print(f"  [UPDATE SUCCESS] Updated Zoho Bill ID: {existing_bill_id}")
                            return {"success": True, "bill_id": existing_bill_id, "zoho_bill_id": existing_bill_id, "updated": True}
                        else:
                            return {"success": True, "bill_id": existing_bill_id, "zoho_bill_id": existing_bill_id, "already_exists": True}
            except Exception as ex:
                print(f"  [LOOKUP/UPDATE ERROR] {ex}")
            return {"success": True, "bill_id": "ALREADY_EXISTS", "zoho_bill_id": "ALREADY_EXISTS", "already_exists": True}
        else:
            error_data = res.json()
            error_msg = error_data.get("message", "Unknown error")
            print(f"  [FAILED] Status: {res.status_code}")
            print(f"  Response: {json.dumps(error_data, indent=2)}")
            print(f"  [INFO] Full response saved to bill_response.log")
            return {"success": False, "error": f"{error_msg} (Code: {error_data.get('code', 'N/A')})"}
    except Exception as e:
        print(f"  [ERROR] Error creating bill: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

# ----------------------------------------------------------
# API WRAPPER FOR FRONTEND
# ----------------------------------------------------------

def get_all_bills_data(from_date="20250401", to_date="20250430", limit=None, voucher_type="Purchase"):
    """
    Wrapper function for API to get bill data.
    Fetches from Tally, saves ALL fields to SQLite DB, returns formatted data
    for frontend display.  Mirrors get_all_receipts_data() pattern exactly.
    """
    # Ensure DB tables exist
    if database_manager:
        database_manager.init_db()

    try:
        bills = fetch_tally_bills_range(from_date, to_date, limit, voucher_type)

        if bills is None:
            return None
            
        if not bills:
            return {
                "bills": [],
                "stats": {
                    "total_bills": 0,
                    "total_amount": 0
                }
            }

        # ----------------------------------------------------------------
        # SAVE EVERY FIELD TO SQLITE
        # Complex list fields — vendor_address, line_items, taxes —
        # are JSON-serialised exactly like invoice_allocations in receipts.
        # NOT A SINGLE FIELD IS SKIPPED.
        # ----------------------------------------------------------------
        if database_manager and bills:
            now = datetime.now().isoformat()
            db_data_list = []

            for bill in bills:
                db_data_list.append({
                    # --- Voucher identity ---
                    "bill_number": bill.get("bill_number", ""),
                    "date":        bill.get("date", ""),
                    "vendor_name": bill.get("vendor_name", ""),

                    # --- Header fields ---
                    "po_number":        bill.get("po_number", ""),
                    "reference_number": bill.get("reference_number", ""),
                    # vendor_address is a list — JSON stringify it
                    "vendor_address":   json.dumps(bill.get("vendor_address", [])),
                    "payment_terms":    bill.get("payment_terms", ""),
                    "purchase_ledger":  bill.get("purchase_ledger", ""),
                    "narration":        bill.get("narration", ""),

                    # --- line_items: each element has
                    #     item_name, quantity, rate, discount,
                    #     amount, category, cost_centre
                    "line_items": json.dumps(bill.get("line_items", [])),

                    # --- taxes: each element has
                    #     tax_name, tax_type, tax_rate, tax_amount
                    "taxes": json.dumps(bill.get("taxes", [])),

                    # --- Totals ---
                    "rounding_off": bill.get("rounding_off", 0) or 0,
                    "subtotal":     bill.get("subtotal",     0) or 0,
                    "tax_total":    bill.get("tax_total",    0) or 0,
                    "total_amount": bill.get("total_amount", 0) or 0,

                    # --- Fetch range & timestamps ---
                    "from_date":  from_date,
                    "to_date":    to_date,
                    "created_at": now,
                    "updated_at": now,
                })

            # Bulk-save to prevent 'database is locked' errors
            database_manager.bulk_save_bills(db_data_list)
            print(f" Saved {len(bills)} bills to database")

            # Attach existing sync statuses from DB
            try:
                conn = database_manager.get_db_connection()
                rows = conn.execute("SELECT bill_number, date, zoho_bill_id, zoho_status, zoho_error FROM bills").fetchall()
                conn.close()
                status_map = {(str(r["bill_number"]).strip().lower(), str(r["date"]).strip()): dict(r) for r in rows}
                for bill in bills:
                    b_key = (str(bill.get("bill_number", "")).strip().lower(), str(bill.get("date", "")).strip())
                    b_info = status_map.get(b_key)
                    if b_info:
                        bill["zoho_bill_id"] = b_info.get("zoho_bill_id")
                        bill["zoho_status"] = b_info.get("zoho_status")
                        bill["zoho_error"] = b_info.get("zoho_error")
            except Exception as ex:
                print(f" Error loading bill sync statuses: {ex}")

        # ----------------------------------------------------------------
        # Build return stats
        # ----------------------------------------------------------------
        total_bills  = len(bills)
        total_amount = sum(bill.get("total_amount", 0) for bill in bills)

        return {
            "bills": bills,
            "stats": {
                "total_bills":  total_bills,
                "total_amount": round(total_amount, 2),
                "from_date":    from_date,
                "to_date":      to_date
            }
        }

    except Exception as e:
        print(f" Error in get_all_bills_data: {e}")
        import traceback
        traceback.print_exc()
        return None

def fetch_tally_bills_range(from_date="20250401", to_date="20250430", limit=None, voucher_type="Purchase"):
    """
    Fetch Purchase bills from Tally with ALL fields (matching invoice structure)
    
    Args:
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        limit: Maximum number of bills to fetch
        voucher_type: Tally Voucher Type Name to fetch (e.g. "Purchase", "GST PURCHASE")
    """
    ledger_map = get_ledger_map_from_tally()
    
    xml_request = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>{voucher_type}</VOUCHERTYPENAME>
    <SVFROMDATE>{from_date}</SVFROMDATE><SVTODATE>{to_date}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        print(f" Fetching bills ({voucher_type}) from Tally ({from_date} to {to_date})...")
        response = requests.post(TALLY_URL, data=xml_request, timeout=90)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        # Check for Tally errors
        line_error = soup.find('LINEERROR')
        if line_error:
            error_text = line_error.text.strip()
            print(f" Tally returned an error: {error_text}")
            raise Exception(f"Tally Error: {error_text}")
            
        vouchers = soup.find_all('VOUCHER')
        if limit:
            vouchers = vouchers[:limit]
        
        bill_data = []
        
        for v in vouchers:
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            vendor_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get Purchase Order Number
            po_number = v.find('BASICPURCHASEORDERNO').text if v.find('BASICPURCHASEORDERNO') else ""
            
            # Get Reference Number (Vendor Invoice Number)
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            
            # Get Vendor Address
            vendor_address = []
            vendor_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if vendor_addr_list:
                for addr in vendor_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        vendor_address.append(addr.text.strip())
            
            # Get Payment Terms
            payment_terms = get_payment_terms_hierarchical(v, vendor_name)
            
            # Get Purchase Ledger
            purchase_ledger = ""
            item_tax_rates = {}
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    purchase_ledger = item_ledger.text.strip()
                acc_alloc = item.find('ACCOUNTINGALLOCATIONS.LIST')
                if acc_alloc:
                    acc_ledger = acc_alloc.find('LEDGERNAME')
                    if acc_ledger and acc_ledger.text:
                        purchase_ledger = acc_ledger.text.strip()
                
                # Extract GST rate details from inventory items
                for rd in item.find_all('RATEDETAILS.LIST') or item.find_all('RATEDETAILS'):
                    head_tag = rd.find('GSTRATEDUTYHEAD')
                    rate_tag = rd.find('GSTRATE')
                    if head_tag and rate_tag:
                        head = head_tag.text.strip().upper()
                        r_nums = re.findall(r'[\d.]+', rate_tag.text)
                        if r_nums:
                            item_tax_rates[head] = float(r_nums[0])
            
            if not purchase_ledger:
                max_negative_amount = 0
                for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                    name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amount_tag = entry.find('AMOUNT')
                    if amount_tag and amount_tag.text:
                        numbers = re.findall(r'[-\d.]+', amount_tag.text)
                        amt = float(numbers[-1]) if numbers else 0.0
                    else:
                        amt = 0.0
                    
                    name_lower = name.lower()
                    if name == vendor_name or any(t in name_lower for t in ['cgst', 'sgst', 'igst', 'gst', 'vat', 'cst', 'tax', 'duty', 'excise', 'cess', 'round', 'r/o']):
                        continue
                    
                    if amt < max_negative_amount:
                        max_negative_amount = amt
                        purchase_ledger = name
            
            if not purchase_ledger:
                purchase_ledger = "Gst Purchase" if (v_date and v_date >= "20170701") else "Purchases"
            
            # Get line items
            line_items = []
            subtotal = 0
            
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_name = item.find('STOCKITEMNAME').text.strip() if item.find('STOCKITEMNAME') else ""
                
                qty_tag = item.find('ACTUALQTY') or item.find('BILLEDQTY')
                quantity = qty_tag.text.strip() if qty_tag else "0"
                
                rate_tag = item.find('RATE')
                if rate_tag and rate_tag.text:
                    rate_text = rate_tag.text.split('/')[0].strip()
                    numbers = re.findall(r'[-\d.]+', rate_text)
                    rate = float(numbers[-1]) if numbers else 0.0
                else:
                    rate = 0.0
                
                discount_tag = item.find('DISCOUNT')
                discount = discount_tag.text.strip() if discount_tag else "0"
                
                amount_tag = item.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    amount = float(numbers[-1]) if numbers else 0.0
                else:
                    amount = 0.0
                
                category = ""
                cost_centre = ""
                cat_alloc = item.find('CATEGORYALLOCATIONS.LIST')
                if cat_alloc:
                    category = cat_alloc.find('CATEGORY').text if cat_alloc.find('CATEGORY') else ""
                    cc_list = cat_alloc.find('COSTCENTREALLOCATIONS.LIST')
                    if cc_list:
                        cost_centre = cc_list.find('NAME').text if cc_list.find('NAME') else ""
                
                line_items.append({
                    "item_name": item_name,
                    "quantity": quantity,
                    "rate": rate,
                    "discount": discount,
                    "amount": abs(amount),
                    "category": category,
                    "cost_centre": cost_centre
                })
                
                subtotal += abs(amount)
            
            # Get party amount (true bill total in Tally)
            party_amount = 0.0
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                is_party_tag = entry.find('ISPARTYLEDGER')
                is_party = (is_party_tag.text.strip().lower() in ['yes', 'true', '1']) if is_party_tag else False
                if is_party or (vendor_name and name.lower() == vendor_name.lower()):
                    amt_tag = entry.find('AMOUNT')
                    if amt_tag and amt_tag.text:
                        nums = re.findall(r'[-\d.]+', amt_tag.text)
                        if nums:
                            party_amount = abs(float(nums[-1]))
                            if not vendor_name:
                                vendor_name = name
                            break

            # Get taxes, rounding off, and additional ledger charges
            taxes = []
            tax_total = 0.0
            rounding_off = 0.0
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                if not name:
                    continue
                
                is_party_tag = entry.find('ISPARTYLEDGER')
                is_party = (is_party_tag.text.strip().lower() in ['yes', 'true', '1']) if is_party_tag else False
                if is_party or (vendor_name and name.lower() == vendor_name.lower()):
                    continue
                
                amount_tag = entry.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    amt = float(numbers[-1]) if numbers else 0.0
                else:
                    amt = 0.0
                
                name_lower = name.lower()
                if any(t_kw in name_lower for t_kw in ['gst', 'cgst', 'sgst', 'igst', 'cst', 'vat', 'tax', 'duty', 'excise', 'cess']) and name_lower != purchase_ledger.lower():
                    tax_rate = ""
                    rate_match = re.search(r'([\d.]+)\s*%', name)
                    if rate_match:
                        tax_rate = rate_match.group(1)
                    
                    if not tax_rate:
                        rate_tag = entry.find('RATE') or entry.find('BASICRATEOFEXCISEDUTY')
                        if rate_tag and rate_tag.text:
                            r_match = re.search(r'([\d.]+)', rate_tag.text)
                            if r_match:
                                tax_rate = r_match.group(1)
                    
                    if not tax_rate:
                        if 'cgst' in name_lower:
                            tax_rate = str(item_tax_rates.get('CGST', '9'))
                        elif 'sgst' in name_lower or 'utgst' in name_lower:
                            tax_rate = str(item_tax_rates.get('SGST/UTGST', item_tax_rates.get('CGST', '9')))
                        elif 'igst' in name_lower:
                            tax_rate = str(item_tax_rates.get('IGST', '18'))
                    
                    if not tax_rate and abs(amt) > 0 and subtotal > 0:
                        calc_r = round((abs(amt) / subtotal) * 100, 2)
                        if calc_r > 0:
                            tax_rate = str(int(calc_r)) if abs(calc_r - round(calc_r)) < 1e-4 else str(calc_r)
                    
                    tax_type = "IGST" if 'igst' in name_lower else ("CGST" if 'cgst' in name_lower else ("SGST" if 'sgst' in name_lower else ("VAT" if 'vat' in name_lower else ("CST" if 'cst' in name_lower else ("Excise Duty" if ('excise' in name_lower or 'duty' in name_lower) else "Tax")))))
                    taxes.append({
                        "tax_name": name,
                        "tax_type": tax_type,
                        "tax_rate": str(tax_rate),
                        "tax_amount": abs(amt)
                    })
                    tax_total += abs(amt)
                elif ('round' in name_lower or 'r/o' in name_lower) and abs(amt) <= 50.0:
                    # Debit amt < 0 in Tally adds to payable, Credit amt > 0 subtracts from payable
                    rounding_off = -amt
                elif name_lower != purchase_ledger.lower() and abs(amt) > 0:
                    # Additional Ledger Charge (e.g. Freight Charges, Transportation, Packing, Insurance, Electricity, large adjustments)
                    charge_amt = abs(amt)
                    line_items.append({
                        "item_name": name,
                        "quantity": "1.0",
                        "rate": charge_amt,
                        "discount": "0",
                        "amount": charge_amt,
                        "category": "",
                        "cost_centre": "",
                        "is_additional_charge": True
                    })
                    subtotal += charge_amt
            
            # Final reconciliation with true party amount
            if party_amount > 0:
                total_amount = round(party_amount, 2)
                calculated_diff = round(party_amount - (subtotal + tax_total), 2)
                if abs(calculated_diff) <= 20.0 and rounding_off == 0.0:
                    rounding_off = calculated_diff
            else:
                total_amount = round(subtotal + tax_total + rounding_off, 2)
            
            bill_data.append({
                "date": v_date,
                "bill_number": v_no,
                "vendor_name": vendor_name,
                "po_number": po_number,
                "reference_number": reference_number,
                "vendor_address": vendor_address,
                "payment_terms": payment_terms,
                "purchase_ledger": purchase_ledger,
                "narration": narration,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": round(rounding_off, 2),
                "subtotal": round(subtotal, 2),
                "tax_total": round(tax_total, 2),
                "total_amount": round(total_amount, 2)
            })
        
        print(f" Fetched {len(bill_data)} bill(s)")
        return bill_data
    
    except Exception as e:
        print(f" Error fetching Tally bills: {e}")
        import traceback
        traceback.print_exc()
        return []

def sync_bills_to_zoho(selected_bills=None, from_date="20250401", to_date="20250430", limit=None, voucher_type="Purchase"):
    """
    Sync bills to Zoho Books.

    Priority order for data source:
      1. selected_bills    — passed directly from frontend (selected rows)
      2. DB (tally_data.db)— read previously-fetched data, NO second Tally port call
      3. Tally port         — fallback only if DB is also empty for the range

    This means 'Sync All' re-uses data saved in the DB by
    'Import Bills', avoiding a redundant Tally XML request.
    """
    try:
        print(" Starting Zoho Sync (Bills)...")

        # Get access token
        token = get_access_token()
        if not token:
            return {"status": "error", "message": "Failed to get access token"}

        # Get Zoho reference data (from SQLite DB Cache with 0 API calls)
        print("    Loading Zoho Masters from SQLite DB Cache...")
        contact_map      = get_zoho_contacts(token, use_cache=True, force_refresh=False)
        item_map         = get_zoho_items(token, use_cache=True, force_refresh=False)
        account_map      = get_zoho_accounts(token, use_cache=True, force_refresh=False)
        payment_terms_map= get_zoho_payment_terms_list(token, use_cache=True, force_refresh=False)
        tax_map          = get_zoho_taxes(token, use_cache=True, force_refresh=False)
        tag_map          = get_zoho_tags(token, use_cache=True, force_refresh=False)
        print(f"    Loaded {len(contact_map)} contacts, {len(item_map)} items, {len(account_map)} accounts, {len(payment_terms_map)} payment terms, {len(tax_map)} taxes from DB Cache.")

        # ----------------------------------------------------------------
        # DETERMINE WHAT TO SYNC
        # ----------------------------------------------------------------
        if selected_bills:
            # 1. Explicit selection from frontend — use as-is
            bills_to_sync = selected_bills
            if limit and len(bills_to_sync) > limit:
                bills_to_sync = bills_to_sync[:limit]
            print(f" Using {len(bills_to_sync)} selected bill(s) from frontend")

        else:
            # 2. Try DB first (avoids second Tally port call)
            bills_to_sync = []
            if database_manager:
                db_rows = database_manager.get_bills_by_date_range(from_date, to_date)
                if db_rows:
                    # Parse JSON fields back to Python objects
                    for row in db_rows:
                        for field in ('vendor_address', 'line_items', 'taxes'):
                            if row.get(field) and isinstance(row[field], str):
                                try:
                                    row[field] = json.loads(row[field])
                                except Exception:
                                    row[field] = []
                    if limit:
                        db_rows = db_rows[:limit]
                    bills_to_sync = db_rows
                    print(f" Loaded {len(bills_to_sync)} bill(s) from DB (no Tally call needed)")

            # 3. Fallback: hit Tally port if DB was empty
            if not bills_to_sync:
                print(" DB empty for range — fetching from Tally port as fallback...")
                bills_to_sync = fetch_tally_bills_range(from_date, to_date, limit, voucher_type)

        if not bills_to_sync:
            return {"status": "error", "message": "No bills to sync"}

        print(f" Syncing {len(bills_to_sync)} bill(s) to Zoho Books...")

        stats = {"created": 0, "failed": 0, "errors": []}

        for bill in bills_to_sync:
            result = create_zoho_bill(token, bill, contact_map, account_map,
                                      payment_terms_map, tax_map, tag_map, item_map=item_map)
            if result.get("success"):
                stats["created"] += 1
                zoho_id = result.get("zoho_bill_id") or result.get("bill_id") or "SYNCED"
                if database_manager:
                    try:
                        conn = database_manager.get_db_connection(write=True)
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE bills SET zoho_bill_id = ?, zoho_status = 'synced', zoho_error = NULL, updated_at = ? WHERE bill_number = ?",
                            (str(zoho_id), datetime.now().isoformat(), str(bill.get("bill_number")))
                        )
                    except Exception as e_db:
                        print(f"  [DB UPDATE ERROR] {e_db}")
                print(f" Synced Bill #{bill['bill_number']}")
            else:
                stats["failed"] += 1
                err_msg = result.get("error", "Unknown error")
                if database_manager:
                    try:
                        conn = database_manager.get_db_connection(write=True)
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE bills SET zoho_status = 'failed', zoho_error = ?, updated_at = ? WHERE bill_number = ?",
                            (str(err_msg), datetime.now().isoformat(), str(bill.get("bill_number")))
                        )
                    except Exception:
                        pass
                stats["errors"].append({
                    "bill_number": str(bill.get('bill_number') or ''),
                    "vendor":      str(bill.get('vendor_name') or ''),
                    "date":        str(bill.get('date') or ''),
                    "amount":      float(bill.get('total_amount') or 0.0),
                    "error":       err_msg
                })
                print(f" Failed Bill #{bill.get('bill_number')}: {err_msg}")

        return {"status": "success", "stats": stats}

    except Exception as e:
        print(f" Error in sync_bills_to_zoho: {e}")
        return {"status": "error", "message": str(e)}


def main():
    """Main function to migrate bill #11 from Tally to Zoho Books"""
    print("="*100)
    print("TALLY TO ZOHO BOOKS BILL MIGRATION - BILL #11 TEST")
    print("="*100)
    
    # Get access token
    print("\n[AUTH] Authenticating with Zoho Books...")
    token = get_access_token()
    if not token:
        print("[ERROR] Failed to get access token")
        return
    print("[SUCCESS] Authentication successful")
    
    # Fetch contacts, accounts, payment terms, taxes, and tags
    print("\n[FETCH] Fetching Zoho Books data...")
    contact_map = get_zoho_contacts(token)
    account_map = get_zoho_accounts(token)
    payment_terms_map = get_zoho_payment_terms_list(token)
    tax_map = get_zoho_taxes(token)
    tag_map = get_zoho_tags(token)
    print(f"[SUCCESS] Loaded {len(contact_map)} vendors, {len(account_map)} accounts, {len(payment_terms_map)} payment terms, {len([k for k in tax_map.keys() if isinstance(k, float)])} taxes, {len(tag_map)} tags")
    
    # Fetch bill #11 from Tally
    print("\n[FETCH] Fetching bill #11 from Tally...")
    bills = fetch_tally_bills(bill_number="11")
    
    if not bills:
        print("[ERROR] No bills found in Tally")
        return
    
    print(f"[SUCCESS] Found {len(bills)} bill(s)")
    
    # Process bill
    success_count = 0
    for bill in bills:
        if create_zoho_bill(token, bill, contact_map, account_map, payment_terms_map, tax_map, tag_map):
            success_count += 1
    
    print(f"\n{'='*100}")
    print(f"[COMPLETE] MIGRATION COMPLETE: {success_count}/{len(bills)} bill(s) created successfully")
    print(f"{'='*100}")

if __name__ == "__main__":
    main()


def parse_tally_json(json_path):
    import json, re
    try:
        with open(json_path, 'r', encoding='utf-16') as f: data = json.load(f)
    except:
        with open(json_path, 'r', encoding='utf-8') as f: data = json.load(f)
    if isinstance(data, list) and len(data) > 0 and 'date' in data[0]: return data
    vouchers = data.get('tallymessage', [])
    if isinstance(vouchers, dict): vouchers = [vouchers]
    
    bill_data = []
    for v in vouchers:
        if not isinstance(v, dict): continue
        if 'vouchernumber' not in v and 'vouchertypename' not in v: continue
        
        v_date = str(v.get('date', '')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        v_no = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or tally_guid or '').strip()
        if not v_no:
            import hashlib
            v_no = "AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
        if 'seen_v_no' not in locals(): seen_v_no = set()
        original_no = v_no
        counter = 1
        while v_no in seen_v_no:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            v_no = f"{original_no}_{suffix}"
            counter += 1
        seen_v_no.add(v_no)
        vendor_name = str(v.get('partyname', '')).strip()
        narration = str(v.get('narration', '')).strip()
        po_number = str(v.get('basicpurchaseorderno', '')).strip()
        reference_number = str(v.get('reference', '')).strip()
        
        vendor_address = []
        buyer_addr = v.get('basicbuyeraddress.list', v.get('basicbuyeraddress', []))
        if not isinstance(buyer_addr, list): buyer_addr = [buyer_addr]
        for addr in buyer_addr:
            if isinstance(addr, dict) and 'basicbuyeraddress' in addr:
                vendor_address.append(str(addr['basicbuyeraddress']).strip())
            elif isinstance(addr, str): vendor_address.append(addr.strip())
            
        payment_terms = str(v.get('basicduedateofpymt', '')).strip()
        if not payment_terms:
            bill_allocs = v.get('billallocations.list', v.get('billallocations', []))
            if not isinstance(bill_allocs, list): bill_allocs = [bill_allocs]
            for ba in bill_allocs:
                if isinstance(ba, dict) and ba.get('billcreditperiod'):
                    payment_terms = str(ba['billcreditperiod']).strip()
                    break

        purchase_ledger = ""
        item_tax_rates = {}
        inventory_entries = v.get('allinventoryentries', v.get('inventoryentries', v.get('allinventoryentries.list', v.get('inventoryentries.list', []))))
        if not isinstance(inventory_entries, list): inventory_entries = [inventory_entries]
        for item in inventory_entries:
            if isinstance(item, dict):
                if item.get('ledgername'):
                    purchase_ledger = str(item['ledgername']).strip()
                acc_alloc = item.get('accountingallocations', item.get('accountingallocations.list', []))
                if not isinstance(acc_alloc, list): acc_alloc = [acc_alloc]
                for acc in acc_alloc:
                    if isinstance(acc, dict) and acc.get('ledgername'):
                        purchase_ledger = str(acc['ledgername']).strip()
                
                # Extract GST rate details from inventory items
                rd = item.get('ratedetails', item.get('ratedetails.list', []))
                if not isinstance(rd, list): rd = [rd]
                for r in rd:
                    if isinstance(r, dict):
                        head = str(r.get('gstratedutyhead', '')).strip().upper()
                        rate_val_str = str(r.get('gstrate', '')).strip()
                        r_nums = re.findall(r'[\d.]+', rate_val_str)
                        if r_nums:
                            item_tax_rates[head] = float(r_nums[0])

        ledger_entries = v.get('allledgerentries', v.get('ledgerentries', v.get('allledgerentries.list', v.get('ledgerentries.list', []))))
        if not isinstance(ledger_entries, list): ledger_entries = [ledger_entries]
        
        # Find party amount (true bill total in Tally)
        party_amount = 0.0
        for entry in ledger_entries:
            if not isinstance(entry, dict): continue
            lname = str(entry.get('ledgername', '')).strip()
            is_party = entry.get('ispartyledger', False)
            if is_party is True or (vendor_name and lname.lower() == vendor_name.lower()):
                amt_str = str(entry.get('amount', '0'))
                nums = re.findall(r'[-\d.]+', amt_str)
                if nums:
                    party_amount = abs(float(nums[-1]))
                    if not vendor_name:
                        vendor_name = lname
                    break

        if not purchase_ledger:
            max_neg = 0
            for entry in ledger_entries:
                if not isinstance(entry, dict): continue
                lname = str(entry.get('ledgername', '')).strip()
                amt_str = str(entry.get('amount', '0'))
                nums = re.findall(r'[-\d.]+', amt_str)
                amt = float(nums[-1]) if nums else 0.0
                lname_lower = lname.lower()
                if lname.lower() == vendor_name.lower() or any(t in lname_lower for t in ['cgst', 'sgst', 'igst', 'gst', 'vat', 'cst', 'tax', 'duty', 'excise', 'cess', 'round', 'r/o']): continue
                if amt < max_neg: max_neg = amt; purchase_ledger = lname

        if not purchase_ledger:
            purchase_ledger = "Gst Purchase" if (v_date and v_date >= "20170701") else "Purchases"

        line_items = []
        for item in inventory_entries:
            if not isinstance(item, dict): continue
            item_name = str(item.get('stockitemname', '')).strip()
            if not item_name: continue
            quantity = str(item.get('actualqty', item.get('billedqty', '0'))).strip()
            rate_str = str(item.get('rate', '0')).split('/')[0].strip()
            nums = re.findall(r'[-\d.]+', rate_str)
            rate = float(nums[-1]) if nums else 0.0
            discount = str(item.get('discount', '0')).strip()
            amt_str = str(item.get('amount', '0')).strip()
            nums = re.findall(r'[-\d.]+', amt_str)
            amount = float(nums[-1]) if nums else 0.0
            
            cat_alloc = item.get('categoryallocations.list', item.get('categoryallocations', {}))
            if isinstance(cat_alloc, list) and len(cat_alloc) > 0: cat_alloc = cat_alloc[0]
            category = str(cat_alloc.get('category', '')).strip() if isinstance(cat_alloc, dict) else ""
            
            cost_centre = ""
            if isinstance(cat_alloc, dict):
                cc_alloc = cat_alloc.get('costcentreallocations.list', cat_alloc.get('costcentreallocations', {}))
                if isinstance(cc_alloc, list) and len(cc_alloc) > 0: cc_alloc = cc_alloc[0]
                cost_centre = str(cc_alloc.get('name', '')).strip() if isinstance(cc_alloc, dict) else ""

            line_items.append({
                "item_name": item_name,
                "quantity": quantity,
                "rate": rate,
                "discount": discount,
                "amount": abs(amount),
                "category": category,
                "cost_centre": cost_centre,
                "is_additional_charge": False
            })

        subtotal = sum(item.get("amount", 0) for item in line_items)
        taxes = []
        tax_total = 0.0
        rounding_off = 0.0
        
        for entry in ledger_entries:
            if not isinstance(entry, dict): continue
            lname = str(entry.get('ledgername', '')).strip()
            if not lname: continue
            if lname.lower() == vendor_name.lower() or entry.get('ispartyledger') is True:
                continue
                
            amt_str = str(entry.get('amount', '0'))
            nums = re.findall(r'[-\d.]+', amt_str)
            amt = float(nums[-1]) if nums else 0.0
            lname_lower = lname.lower()
            
            # Tax ledgers
            if any(t_kw in lname_lower for t_kw in ['gst', 'cgst', 'sgst', 'igst', 'cst', 'vat', 'tax', 'duty', 'excise', 'cess']) and lname_lower != purchase_ledger.lower():
                tax_rate = ""
                rate_match = re.search(r'([\d.]+)\s*%', lname)
                if rate_match:
                    tax_rate = rate_match.group(1)
                
                # Check rateofinvoicetax or rate field
                if not tax_rate and 'rateofinvoicetax' in entry:
                    tax_obj = entry['rateofinvoicetax']
                    if isinstance(tax_obj, list):
                        for r_it in tax_obj:
                            if isinstance(r_it, str) and r_it.strip():
                                tax_rate = r_it.strip()
                                break
                    elif isinstance(tax_obj, (str, int, float)):
                        tax_rate = str(tax_obj).strip()
                
                if not tax_rate and 'rate' in entry:
                    r_str = str(entry.get('rate', '')).split('/')[0].strip()
                    r_nums = re.findall(r'([\d.]+)', r_str)
                    if r_nums:
                        tax_rate = r_nums[0]
                
                if not tax_rate:
                    if 'cgst' in lname_lower:
                        tax_rate = str(item_tax_rates.get('CGST', '9'))
                    elif 'sgst' in lname_lower or 'utgst' in lname_lower:
                        tax_rate = str(item_tax_rates.get('SGST/UTGST', item_tax_rates.get('CGST', '9')))
                    elif 'igst' in lname_lower:
                        tax_rate = str(item_tax_rates.get('IGST', '18'))
                
                if not tax_rate and abs(amt) > 0 and subtotal > 0:
                    calc_r = round((abs(amt) / subtotal) * 100, 2)
                    if calc_r > 0:
                        tax_rate = str(int(calc_r)) if abs(calc_r - round(calc_r)) < 1e-4 else str(calc_r)
                
                tax_type = "IGST" if 'igst' in lname_lower else ("CGST" if 'cgst' in lname_lower else ("SGST" if 'sgst' in lname_lower else ("VAT" if 'vat' in lname_lower else ("CST" if 'cst' in lname_lower else ("Excise Duty" if ('excise' in lname_lower or 'duty' in lname_lower) else "Tax")))))
                taxes.append({
                    "tax_name": lname,
                    "tax_type": tax_type,
                    "tax_rate": str(tax_rate),
                    "tax_amount": abs(amt)
                })
                tax_total += abs(amt)
            elif ('round' in lname_lower or 'r/o' in lname_lower) and abs(amt) <= 50.0:
                # Debit amt < 0 in Tally adds to payable, Credit amt > 0 subtracts from payable
                rounding_off = -amt
            elif lname_lower != purchase_ledger.lower() and abs(amt) > 0:
                # Additional Ledger Charge (e.g. Freight Charges, Transport, Packing, Electricity, large adjustments)
                charge_amt = abs(amt)
                line_items.append({
                    "item_name": lname,
                    "quantity": "1.0",
                    "rate": charge_amt,
                    "discount": "0",
                    "amount": charge_amt,
                    "category": "",
                    "cost_centre": "",
                    "is_additional_charge": True
                })
                subtotal += charge_amt
                
        # Final reconciliation with true party amount
        if party_amount > 0:
            total_amount = round(party_amount, 2)
            calculated_diff = round(party_amount - (subtotal + tax_total), 2)
            if abs(calculated_diff) <= 20.0 and rounding_off == 0.0:
                rounding_off = calculated_diff
        else:
            total_amount = round(subtotal + tax_total + rounding_off, 2)

        bill_data.append({
            "date": v_date,
            "bill_number": v_no,
            "vendor_name": vendor_name,
            "po_number": po_number,
            "reference_number": reference_number,
            "vendor_address": vendor_address,
            "payment_terms": payment_terms,
            "purchase_ledger": purchase_ledger,
            "line_items": line_items,
            "taxes": taxes,
            "rounding_off": round(rounding_off, 2),
            "subtotal": round(subtotal, 2),
            "tax_total": round(tax_total, 2),
            "total_amount": round(total_amount, 2),
            "narration": narration
        })
    return bill_data


ZOHO_BILL_EXPORT_HEADERS = [
    'Bill Date', 'Due Date', 'Bill ID', 'Vendor Name', 'Entity Discount Percent', 
    'Payment Terms', 'Payment Terms Label', 'Bill Number', 'PurchaseOrder', 'Currency Code', 
    'Exchange Rate', 'SubTotal', 'Total', 'Balance', 'TotalRetentionAmountFCY', 
    'TotalRetentionAmountBCY', 'TCS Amount', 'Vendor Notes', 'Terms & Conditions', 
    'Adjustment', 'Adjustment Description', 'Branch ID', 'Branch Name', 'Location Name', 
    'Is Inclusive Tax', 'Submitted By', 'Approved By', 'Submitted Date', 'Approved Date', 
    'Bill Status', 'Created By', 'Product ID', 'Item Name', 'Account', 'Account Code', 
    'Description', 'Quantity', 'Usage unit', 'Tax Amount', 'Item Total', 'Is Billable', 
    'Reference Invoice Type', 'Source of Supply', 'Destination of Supply', 'GST Treatment', 
    'GST Identification Number (GSTIN)', 'TDS Calculation Type', 'TDS TaxID', 'TDS Name', 
    'TDS Percentage', 'TDS Section Code', 'TDS Section', 'TDS Amount', 'TCS Tax Name', 
    'TCS Percentage', 'Nature Of Collection', 'SKU', 'UPC', 'MPN', 'EAN', 'ISBN', 
    'Line Item Location Name', 'Rate', 'Discount Type', 'Is Discount Before Tax', 
    'Discount', 'Discount Amount', 'HSN/SAC', 'Purchase Receive Number', 'Purchase Order Number', 
    'Bill Receive Status', 'Manually Received Quantity', 'Tax ID', 'Tax Name', 'Tax Percentage', 
    'Tax Type', 'Item TDS Name', 'Item TDS Percentage', 'Item TDS Amount', 'Item TDS Section Code', 
    'Item TDS Section', 'Item Exemption Code', 'Item Type', 'Reverse Charge Tax Name', 
    'Reverse Charge Tax Rate', 'Reverse Charge Tax Type', 'Supply Type', 'ITC Eligibility', 
    'Entity Discount Amount', 'Discount Account', 'Discount Account Code', 'Item Discount Account', 
    'Item Discount Account Code', 'Is Landed Cost', 'Customer Name', 'Project Name', 
    'CGST Rate %', 'SGST Rate %', 'IGST Rate %', 'CESS Rate %', 'CGST(FCY)', 'SGST(FCY)', 
    'IGST(FCY)', 'CESS(FCY)', 'CGST', 'SGST', 'IGST', 'CESS'
]


def generate_zoho_formatted_excel(bills_list=None, output_path=None):
    """Generate professional styled Zoho Books formatted Bills Excel file (.xlsx) matching Zoho Books import format."""
    import io, openpyxl, json, re
    from datetime import datetime, timedelta
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    if isinstance(bills_list, str):
        output_path = bills_list
        bills_list = None

    if bills_list is None:
        try:
            import database_manager
            database_manager.init_db()
            raw = database_manager.get_all_bills()
            bills_list = [dict(r) for r in raw]
        except Exception:
            bills_list = []

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bills"
    ws.views.sheetView[0].showGridLines = True

    # Styling definitions
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")  # Dark Navy Blue
    header_font = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )

    data_font = Font(name="Segoe UI", size=9)

    ws.append(ZOHO_BILL_EXPORT_HEADERS)
    ws.row_dimensions[1].height = 28

    for col_num in range(1, len(ZOHO_BILL_EXPORT_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align

    # Fetch token and tax / item maps
    token = None
    tax_map = {}
    item_map = {}
    try:
        token = get_access_token()
        if token:
            tax_map = get_zoho_taxes(token, use_cache=True)
            item_map = get_zoho_items(token, use_cache=True)
    except Exception:
        pass

    # Contact lookup from zoho_contacts cache
    contact_lookup = {}
    try:
        import sqlite3, database_manager
        active_db = database_manager.get_active_db()
        conn_c = sqlite3.connect(active_db)
        cur_c = conn_c.cursor()
        cur_c.execute("SELECT contact_name_lower, place_of_contact, gst_no FROM zoho_contacts")
        contact_lookup = {r[0]: (r[1], r[2]) for r in cur_c.fetchall()}
        conn_c.close()
    except Exception:
        pass

    STATE_GST_MAP = {
        "01": "JK", "02": "HP", "03": "PB", "04": "CH", "05": "UK", "06": "HR",
        "07": "DL", "08": "RJ", "09": "UP", "10": "BR", "11": "SK", "12": "AR",
        "13": "NL", "14": "MN", "15": "MZ", "16": "TR", "17": "ML", "18": "AS",
        "19": "WB", "20": "JH", "21": "OR", "22": "CG", "23": "MP", "24": "GJ",
        "25": "DD", "26": "DN", "27": "MH", "28": "AD", "29": "KA", "30": "GA",
        "31": "LD", "32": "KL", "33": "TN", "34": "PY", "35": "AN", "36": "TS", "37": "AP"
    }

    def detect_vendor_pos(v_addr, v_name):
        c_info = contact_lookup.get(v_name.lower())
        if c_info and c_info[0]:
            return c_info[0].upper()
        gst_m = re.search(r'\b([0-3][0-9])[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b', v_addr.upper())
        if gst_m and gst_m.group(1) in STATE_GST_MAP:
            return STATE_GST_MAP[gst_m.group(1)]
        addr_low = v_addr.lower()
        if any(k in addr_low for k in ["chennai", "tamil nadu", "tamilnadu", "hosur", "coimbatore", "madurai"]): return "TN"
        if any(k in addr_low for k in ["mumbai", "pune", "maharashtra", "thane", "nagpur", "nashik"]): return "MH"
        if any(k in addr_low for k in ["delhi", "new delhi", "noida", "gurgaon"]): return "DL"
        if any(k in addr_low for k in ["hyderabad", "telangana", "secunderabad", "nalgonda", "maheshwaram"]): return "TS"
        if any(k in addr_low for k in ["kerala", "cochin", "kochi", "trivandrum"]): return "KL"
        if any(k in addr_low for k in ["andhra", "vijayawada", "visakhapatnam", "vizag"]): return "AP"
        if any(k in addr_low for k in ["gujarat", "ahmedabad", "surat", "vadodara"]): return "GJ"
        return "KA"

    current_row_idx = 2

    for bill in bills_list:
        raw_bill_no = str(bill.get("bill_number") or bill.get("reference_number") or '')
        raw_date = str(bill.get("date") or '').strip()
        
        if len(raw_date) == 8 and raw_date.isdigit():
            bill_date_str = f"{raw_date[6:8]}/{raw_date[4:6]}/{raw_date[0:4]}"
            bill_dt = datetime(int(raw_date[0:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        elif "-" in raw_date:
            parts = raw_date.split("T")[0].split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                bill_date_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
                bill_dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            elif len(parts) == 3 and len(parts[2]) == 4:
                bill_date_str = f"{parts[0]}/{parts[1]}/{parts[2]}"
                bill_dt = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
            else:
                bill_date_str = raw_date.replace("-", "/")
                bill_dt = datetime.now()
        elif "/" in raw_date:
            bill_date_str = raw_date
            parts = raw_date.split("/")
            if len(parts) == 3 and len(parts[2]) == 4:
                bill_dt = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
            else:
                bill_dt = datetime.now()
        else:
            bill_date_str = raw_date
            bill_dt = datetime.now()

        vendor_name = str(bill.get("vendor_name") or '')
        po_num = str(bill.get("po_number") or '')
        ref_num = str(bill.get("reference_number") or raw_bill_no)
        pay_terms = str(bill.get("payment_terms") or '')
        purch_ledger = str(bill.get("purchase_ledger") or 'Cost of Goods Sold')
        narration = str(bill.get("narration") or '')
        subtotal = float(bill.get("subtotal") or 0)
        tax_total = float(bill.get("tax_total") or 0)
        rounding = float(bill.get("rounding_off") or 0)
        total_amt = float(bill.get("total_amount") or 0)

        # Payment terms
        clean_terms = "30"
        if pay_terms:
            pt_nums = re.findall(r'\d+', str(pay_terms))
            if pt_nums:
                clean_terms = pt_nums[0]
            elif "due on receipt" in str(pay_terms).lower():
                clean_terms = "0"
        
        terms_label = "Due on Receipt" if clean_terms == "0" else f"Net {clean_terms}"
        try:
            due_dt = bill_dt + timedelta(days=int(clean_terms))
            due_date_str = due_dt.strftime("%d/%m/%Y")
        except:
            due_date_str = bill_date_str

        v_addr_raw = bill.get("vendor_address", "")
        if isinstance(v_addr_raw, list):
            v_addr = ", ".join(v_addr_raw)
        else:
            v_addr = str(v_addr_raw or '')

        pos_short = detect_vendor_pos(v_addr, vendor_name)
        is_interstate = (pos_short != "KA" and pos_short != "29")
        is_pre_gst = (raw_date < "20170701" and len(raw_date) >= 8)

        # Contact lookup info
        c_info = contact_lookup.get(vendor_name.lower())
        vendor_gstin = c_info[1] if (c_info and c_info[1]) else ""
        if not vendor_gstin:
            gst_m = re.search(r'\b([0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b', v_addr.upper())
            if gst_m:
                vendor_gstin = gst_m.group(1)

        line_items_raw = bill.get("line_items") or []
        if isinstance(line_items_raw, str):
            try: line_items = json.loads(line_items_raw)
            except: line_items = []
        else:
            line_items = line_items_raw or []

        taxes_raw = bill.get("taxes") or []
        if isinstance(taxes_raw, str):
            try: taxes = json.loads(taxes_raw)
            except: taxes = []
        else:
            taxes = taxes_raw or []

        # Resolve tax from Zoho active taxes master
        tax_info = None
        if tax_total > 0 or taxes:
            try:
                tax_info = find_or_create_bill_tax(token, bill, tax_map)
            except Exception:
                tax_info = None

        cur_tax_id = str(tax_info.get("tax_id", "")) if tax_info else ""
        cur_tax = str(tax_info.get("tax_name", "")) if tax_info else ""
        cur_tax_pct = f"{float(tax_info.get('tax_percentage', 0)):.2f}" if tax_info else "0.00"
        item_tax_type = "Tax Group" if (tax_info and tax_info.get("is_group")) else ("ItemAmount" if tax_info else "")
        tax_perc_val = float(tax_info.get("tax_percentage", 0)) if tax_info else 0.0

        if not line_items:
            line_items = [{
                "item_name": purch_ledger or "Purchase Item",
                "quantity": 1,
                "rate": subtotal or total_amt,
                "amount": subtotal or total_amt
            }]

        processed_items = []
        for it in line_items:
            it_name = str(it.get("item_name") or it.get("name") or "Purchase Item").strip()
            name_l = it_name.lower()

            is_round_off  = any(k in name_l for k in ["round off", "rounding off", "round-off", "rounding", "r/o"])
            is_transport  = any(k in name_l for k in ["transport", "transpotation", "transportation", "vehicle"])
            is_freight    = any(k in name_l for k in ["freight", "fright", "cartage", "courier", "postage"])
            is_charge_item = is_round_off or is_transport or is_freight or it.get("is_additional_charge", False)

            if is_round_off:
                it_account = "Round Off"
                item_type  = "service"
            elif is_transport:
                it_account = "Transportation Charges"
                item_type  = "service"
            elif is_freight or it.get("is_additional_charge"):
                it_account = "Freight Charges"
                item_type  = "service"
            else:
                it_account = "Inventory Asset"
                item_type  = "goods"

            qty_str = str(it.get('quantity') or "1").split()[0]
            try: it_qty = float(qty_str)
            except: it_qty = 1.0
            
            raw_rate = float(it.get('rate') or 0.0)
            raw_amt = float(it.get('amount') or (it_qty * raw_rate))
            
            if it_qty > 0 and raw_amt > 0:
                if raw_rate == 0.0 or abs(it_qty * raw_rate - raw_amt) > 0.05:
                    it_rate = round(raw_amt / it_qty, 4)
                    it_amt = round(it_qty * it_rate, 2)
                else:
                    it_rate = raw_rate
                    it_amt = round(it_qty * raw_rate, 2)
            else:
                it_rate = raw_rate
                it_amt = raw_amt

            # Tax application (charges have 0% tax if tax was on goods only)
            if is_charge_item or not tax_info:
                line_tax_id   = ""
                line_tax_name = ""
                line_tax_pct  = ""
                line_tax_type = ""
                line_tax_amt  = 0.0
            else:
                line_tax_id   = cur_tax_id
                line_tax_name = cur_tax
                line_tax_pct  = cur_tax_pct
                line_tax_type = item_tax_type
                line_tax_amt  = round(it_amt * (tax_perc_val / 100.0), 2)

            # GST breakdown
            if is_pre_gst or not tax_info or not line_tax_id:
                cgst_rate = 0.0; sgst_rate = 0.0; igst_rate = 0.0
                cgst_amt  = 0.0; sgst_amt  = 0.0; igst_amt  = 0.0
            elif is_interstate:
                cgst_rate = 0.0; sgst_rate = 0.0; igst_rate = float(line_tax_pct)
                cgst_amt  = 0.0; sgst_amt  = 0.0; igst_amt  = line_tax_amt
            else:
                half_pct = round(float(line_tax_pct) / 2.0, 2)
                half_amt = round(line_tax_amt / 2.0, 2)
                cgst_rate = half_pct; sgst_rate = half_pct; igst_rate = 0.0
                cgst_amt  = half_amt; sgst_amt  = half_amt; igst_amt  = 0.0

            # Match item_id if available
            matched_item_id = ""
            if not is_charge_item and item_map:
                matched_item = find_item_in_zoho(it_name, item_map)
                matched_item_id = matched_item.get("item_id", "") if matched_item else ""

            processed_items.append({
                "it_name": it_name,
                "it_account": it_account,
                "item_type": item_type,
                "is_charge_item": is_charge_item,
                "it_qty": it_qty,
                "it_rate": it_rate,
                "it_amt": it_amt,
                "line_tax_id": line_tax_id,
                "line_tax_name": line_tax_name,
                "line_tax_pct": line_tax_pct,
                "line_tax_type": line_tax_type,
                "line_tax_amt": line_tax_amt,
                "cgst_rate": cgst_rate,
                "sgst_rate": sgst_rate,
                "igst_rate": igst_rate,
                "cgst_amt": cgst_amt,
                "sgst_amt": sgst_amt,
                "igst_amt": igst_amt,
                "matched_item_id": matched_item_id
            })

        # Bill-level totals & adjustment calculation (exactly as API does)
        subtotal_calc = round(sum(x["it_amt"] for x in processed_items), 2)
        tax_calc = round(sum(x["line_tax_amt"] for x in processed_items if x["line_tax_id"]), 2)
        zoho_calc_total = round(subtotal_calc + tax_calc, 2)
        
        tally_target_total = float(bill.get("total_amount") or 0.0)
        if tally_target_total <= 0:
            tally_target_total = round(subtotal_calc + tax_calc + rounding, 2)

        calc_adjustment = round(tally_target_total - zoho_calc_total, 2)
        adj_val = calc_adjustment if abs(calc_adjustment) > 0.001 else 0.0
        adj_desc = "Rounding Off / Tax Adjustment" if abs(adj_val) > 0.001 else ""

        for pit in processed_items:
            row_dict = {h: "" for h in ZOHO_BILL_EXPORT_HEADERS}
            row_dict["Bill Date"] = bill_date_str
            row_dict["Due Date"] = due_date_str
            row_dict["Bill ID"] = bill.get("zoho_bill_id", "")
            row_dict["Vendor Name"] = vendor_name
            row_dict["Entity Discount Percent"] = "0.00"
            row_dict["Payment Terms"] = clean_terms
            row_dict["Payment Terms Label"] = terms_label
            row_dict["Bill Number"] = raw_bill_no
            row_dict["PurchaseOrder"] = ref_num
            row_dict["Currency Code"] = "INR"
            row_dict["Exchange Rate"] = 1.0
            row_dict["SubTotal"] = subtotal_calc
            row_dict["Total"] = tally_target_total
            row_dict["Balance"] = tally_target_total
            row_dict["TotalRetentionAmountFCY"] = 0.0
            row_dict["TotalRetentionAmountBCY"] = 0.0
            row_dict["Vendor Notes"] = narration
            row_dict["Adjustment"] = adj_val
            row_dict["Adjustment Description"] = adj_desc
            row_dict["Branch ID"] = "3962933000000032109"
            row_dict["Branch Name"] = "Bengaluru"
            row_dict["Location Name"] = "Bengaluru"
            row_dict["Is Inclusive Tax"] = "false"
            row_dict["Bill Status"] = "Overdue"
            row_dict["Created By"] = "info"
            row_dict["Product ID"] = pit["matched_item_id"] if not pit["is_charge_item"] else ""
            row_dict["Item Name"] = pit["it_name"] if not pit["is_charge_item"] else ""
            row_dict["Account"] = pit["it_account"]
            row_dict["Description"] = pit["it_name"]
            row_dict["Quantity"] = pit["it_qty"]
            row_dict["Usage unit"] = "nos"
            row_dict["Tax Amount"] = pit["line_tax_amt"]
            row_dict["Item Total"] = pit["it_amt"]
            row_dict["Is Billable"] = "false"
            row_dict["Source of Supply"] = pos_short
            row_dict["Destination of Supply"] = "KA"
            row_dict["GST Treatment"] = "business_gst"
            row_dict["GST Identification Number (GSTIN)"] = vendor_gstin
            row_dict["TDS Calculation Type"] = "item_level"
            row_dict["TDS Percentage"] = "0.00"
            row_dict["TDS Amount"] = "0.000"
            row_dict["Line Item Location Name"] = "Bengaluru"
            row_dict["Rate"] = pit["it_rate"]
            row_dict["Discount Type"] = "entity_level"
            row_dict["Is Discount Before Tax"] = "true"
            row_dict["Discount"] = "0.00"
            row_dict["Discount Amount"] = "0.0"
            row_dict["Tax ID"] = pit["line_tax_id"]
            row_dict["Tax Name"] = pit["line_tax_name"]
            row_dict["Tax Percentage"] = pit["line_tax_pct"]
            row_dict["Tax Type"] = pit["line_tax_type"]
            row_dict["Item TDS Amount"] = "0.000"
            row_dict["Item Type"] = pit["item_type"]
            row_dict["ITC Eligibility"] = "eligible"
            row_dict["Entity Discount Amount"] = "0.000"
            row_dict["Is Landed Cost"] = "false"
            row_dict["CGST Rate %"] = pit["cgst_rate"]
            row_dict["SGST Rate %"] = pit["sgst_rate"]
            row_dict["IGST Rate %"] = pit["igst_rate"]
            row_dict["CESS Rate %"] = 0.00
            row_dict["CGST(FCY)"] = pit["cgst_amt"]
            row_dict["SGST(FCY)"] = pit["sgst_amt"]
            row_dict["IGST(FCY)"] = pit["igst_amt"]
            row_dict["CESS(FCY)"] = 0.00
            row_dict["CGST"] = pit["cgst_amt"]
            row_dict["SGST"] = pit["sgst_amt"]
            row_dict["IGST"] = pit["igst_amt"]
            row_dict["CESS"] = 0.00

            row_vals = [row_dict[h] for h in ZOHO_BILL_EXPORT_HEADERS]
            ws.append(row_vals)
            row_fill = zebra_fill if (current_row_idx % 2 == 0) else white_fill
            ws.row_dimensions[current_row_idx].height = 20

            for col_idx in range(1, len(row_vals) + 1):
                c = ws.cell(row=current_row_idx, column=col_idx)
                c.font = data_font
                c.fill = row_fill
                c.border = thin_border
                h_name = ZOHO_BILL_EXPORT_HEADERS[col_idx - 1]
                if any(k in h_name for k in ["Total", "Amount", "SubTotal", "Balance", "Rate", "Adjustment"]) and not any(k in h_name for k in ["Date", "Description", "Account", "Type"]):
                    c.alignment = Alignment(horizontal="right", vertical="center")
                    if isinstance(c.value, (int, float)):
                        c.number_format = '#,##0.00'
                elif "Date" in h_name:
                    c.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    c.alignment = Alignment(horizontal="left", vertical="center")

            current_row_idx += 1

    # Adjust column widths instantly
    for col_num in range(1, len(ZOHO_BILL_EXPORT_HEADERS) + 1):
        col_letter = get_column_letter(col_num)
        h_len = len(ZOHO_BILL_EXPORT_HEADERS[col_num - 1])
        ws.column_dimensions[col_letter].width = max(h_len + 4, 14)

    if output_path:
        wb.save(output_path)
        return output_path

    out_stream = io.BytesIO()
    wb.save(out_stream)
    out_stream.seek(0)
    return out_stream.getvalue()


def generate_sync_errors_excel(errors_list, output_path=None):
    """Generates a professional Excel file (.xlsx) with full Zoho Books Bills Import Columns and an Error Summary sheet."""
    import io, openpyxl, json, re, sqlite3
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from datetime import datetime, timedelta

    # Load complete bill data from database for failed bills
    bills_db_map = {}
    try:
        import database_manager
        database_manager.init_db()
        all_db_bills = database_manager.get_all_bills()
        for b in all_db_bills:
            bd = dict(b)
            b_no = str(bd.get("bill_number") or "").strip().lower()
            b_dt = str(bd.get("date") or "").replace("-", "").strip()
            bills_db_map[(b_no, b_dt)] = bd
            if b_no:
                bills_db_map[b_no] = bd
    except Exception:
        pass

    wb = openpyxl.Workbook()

    # -------------------------------------------------------------
    # SHEET 1: Full Zoho Books Bills Import Format (Ready for Zoho)
    # -------------------------------------------------------------
    ws_bills = wb.active
    ws_bills.title = "Bills"
    ws_bills.views.sheetView[0].showGridLines = True

    # Zoho Import headers + error analysis columns
    headers_bills = list(ZOHO_BILL_EXPORT_HEADERS) + ["Sync Error Message", "Action Required / Fix"]
    ws_bills.append(headers_bills)
    ws_bills.row_dimensions[1].height = 28

    header_fill_blue = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")  # Dark Navy
    header_fill_red = PatternFill(start_color="991B1B", end_color="991B1B", fill_type="solid")   # Red for Error columns
    header_font = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_num in range(1, len(headers_bills) + 1):
        cell = ws_bills.cell(row=1, column=col_num)
        cell.fill = header_fill_red if col_num > len(ZOHO_BILL_EXPORT_HEADERS) else header_fill_blue
        cell.font = header_font
        cell.alignment = header_align

    # Caches
    token = None
    tax_map = {}
    item_map = {}
    try:
        token = get_access_token()
        if token:
            tax_map = get_zoho_taxes(token, use_cache=True)
            item_map = get_zoho_items(token, use_cache=True)
    except Exception:
        pass

    contact_lookup = {}
    try:
        active_db = database_manager.get_active_db()
        conn_c = sqlite3.connect(active_db)
        cur_c = conn_c.cursor()
        cur_c.execute("SELECT contact_name_lower, place_of_contact, gst_no FROM zoho_contacts")
        contact_lookup = {r[0]: (r[1], r[2]) for r in cur_c.fetchall()}
        conn_c.close()
    except Exception:
        pass

    STATE_GST_MAP = {
        "01": "JK", "02": "HP", "03": "PB", "04": "CH", "05": "UK", "06": "HR",
        "07": "DL", "08": "RJ", "09": "UP", "10": "BR", "11": "SK", "12": "AR",
        "13": "NL", "14": "MN", "15": "MZ", "16": "TR", "17": "ML", "18": "AS",
        "19": "WB", "20": "JH", "21": "OR", "22": "CG", "23": "MP", "24": "GJ",
        "25": "DD", "26": "DN", "27": "MH", "28": "AD", "29": "KA", "30": "GA",
        "31": "LD", "32": "KL", "33": "TN", "34": "PY", "35": "AN", "36": "TS", "37": "AP"
    }

    def detect_vendor_pos(v_addr, v_name):
        c_info = contact_lookup.get(v_name.lower())
        if c_info and c_info[0]:
            return c_info[0].upper()
        gst_m = re.search(r'\b([0-3][0-9])[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b', v_addr.upper())
        if gst_m and gst_m.group(1) in STATE_GST_MAP:
            return STATE_GST_MAP[gst_m.group(1)]
        addr_low = v_addr.lower()
        if any(k in addr_low for k in ["chennai", "tamil nadu", "tamilnadu", "hosur", "coimbatore", "madurai"]): return "TN"
        if any(k in addr_low for k in ["mumbai", "pune", "maharashtra", "thane", "nagpur", "nashik"]): return "MH"
        if any(k in addr_low for k in ["delhi", "new delhi", "noida", "gurgaon"]): return "DL"
        if any(k in addr_low for k in ["hyderabad", "telangana", "secunderabad", "nalgonda", "maheshwaram"]): return "TS"
        if any(k in addr_low for k in ["kerala", "cochin", "kochi", "trivandrum"]): return "KL"
        if any(k in addr_low for k in ["andhra", "vijayawada", "visakhapatnam", "vizag"]): return "AP"
        if any(k in addr_low for k in ["gujarat", "ahmedabad", "surat", "vadodara"]): return "GJ"
        return "KA"

    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )
    data_font = Font(name="Segoe UI", size=9)
    err_font = Font(name="Segoe UI", size=9, color="B91C1C", bold=True)

    current_row_idx = 2

    # Populate Sheet 1 with full Zoho Import format for each failed bill
    for err in errors_list:
        b_no_raw = str(err.get("bill_number") or err.get("invoice_number") or "").strip()
        b_dt_raw = str(err.get("date") or err.get("bill_date") or "").replace("-", "").strip()
        err_msg = str(err.get("error") or err.get("error_message") or "Sync Failed")

        msg_l = err_msg.lower()
        if "not found in zoho books" in msg_l or "create this goods item" in msg_l:
            action = "Create missing Goods/Service item in Zoho Books Items list"
        elif "vendor" in msg_l or "contact" in msg_l:
            action = "Verify Vendor contact name and GSTIN in Zoho Books Contacts"
        elif "tax" in msg_l or "gst" in msg_l:
            action = "Check tax rate and active tax mappings in Zoho Books Taxes"
        elif "account" in msg_l:
            action = "Check Chart of Accounts for required Purchase / Expense ledger"
        elif "already exists" in msg_l:
            action = "Bill already present in Zoho Books for this vendor"
        elif "token" in msg_l or "unauthorized" in msg_l:
            action = "Re-authenticate Zoho OAuth credentials"
        else:
            action = "Review bill line items and tax configuration in Zoho Books"

        # Lookup full bill from DB
        bill = bills_db_map.get((b_no_raw.lower(), b_dt_raw)) or bills_db_map.get(b_no_raw.lower()) or err

        raw_bill_no = str(bill.get("bill_number") or b_no_raw)
        raw_date = str(bill.get("date") or b_dt_raw)
        if len(raw_date) == 8 and raw_date.isdigit():
            bill_date_str = f"{raw_date[6:8]}/{raw_date[4:6]}/{raw_date[0:4]}"
            bill_dt = datetime(int(raw_date[0:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        elif "-" in raw_date:
            parts = raw_date.split("T")[0].split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                bill_date_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
                bill_dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            elif len(parts) == 3 and len(parts[2]) == 4:
                bill_date_str = f"{parts[0]}/{parts[1]}/{parts[2]}"
                bill_dt = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
            else:
                bill_date_str = raw_date.replace("-", "/")
                bill_dt = datetime.now()
        elif "/" in raw_date:
            bill_date_str = raw_date
            parts = raw_date.split("/")
            if len(parts) == 3 and len(parts[2]) == 4:
                bill_dt = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
            else:
                bill_dt = datetime.now()
        else:
            bill_date_str = raw_date
            bill_dt = datetime.now()

        vendor_name = str(bill.get("vendor_name") or err.get("vendor") or '')
        po_num = str(bill.get("po_number") or '')
        ref_num = str(bill.get("reference_number") or raw_bill_no)
        pay_terms = str(bill.get("payment_terms") or '')
        purch_ledger = str(bill.get("purchase_ledger") or 'Cost of Goods Sold')
        narration = str(bill.get("narration") or '')
        subtotal = float(bill.get("subtotal") or 0)
        tax_total = float(bill.get("tax_total") or 0)
        rounding = float(bill.get("rounding_off") or 0)
        total_amt = float(bill.get("total_amount") or err.get("amount") or 0)

        clean_terms = "30"
        if pay_terms:
            pt_nums = re.findall(r'\d+', str(pay_terms))
            if pt_nums: clean_terms = pt_nums[0]
            elif "due on receipt" in str(pay_terms).lower(): clean_terms = "0"
        terms_label = "Due on Receipt" if clean_terms == "0" else f"Net {clean_terms}"
        try:
            due_dt = bill_dt + timedelta(days=int(clean_terms))
            due_date_str = due_dt.strftime("%d/%m/%Y")
        except:
            due_date_str = bill_date_str

        v_addr_raw = bill.get("vendor_address", "")
        v_addr = ", ".join(v_addr_raw) if isinstance(v_addr_raw, list) else str(v_addr_raw or '')
        pos_short = detect_vendor_pos(v_addr, vendor_name)
        is_interstate = (pos_short != "KA" and pos_short != "29")
        is_pre_gst = (raw_date < "20170701" and len(raw_date) >= 8)

        c_info = contact_lookup.get(vendor_name.lower())
        vendor_gstin = c_info[1] if (c_info and c_info[1]) else ""
        if not vendor_gstin:
            gst_m = re.search(r'\b([0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b', v_addr.upper())
            if gst_m: vendor_gstin = gst_m.group(1)

        line_items_raw = bill.get("line_items") or []
        if isinstance(line_items_raw, str):
            try: line_items = json.loads(line_items_raw)
            except: line_items = []
        else:
            line_items = line_items_raw or []

        taxes_raw = bill.get("taxes") or []
        if isinstance(taxes_raw, str):
            try: taxes = json.loads(taxes_raw)
            except: taxes = []
        else:
            taxes = taxes_raw or []

        tax_info = None
        if tax_total > 0 or taxes:
            try: tax_info = find_or_create_bill_tax(token, bill, tax_map)
            except Exception: tax_info = None

        cur_tax_id = str(tax_info.get("tax_id", "")) if tax_info else ""
        cur_tax = str(tax_info.get("tax_name", "")) if tax_info else ""
        cur_tax_pct = f"{float(tax_info.get('tax_percentage', 0)):.2f}" if tax_info else "0.00"
        item_tax_type = "Tax Group" if (tax_info and tax_info.get("is_group")) else ("ItemAmount" if tax_info else "")
        tax_perc_val = float(tax_info.get("tax_percentage", 0)) if tax_info else 0.0

        if not line_items:
            line_items = [{
                "item_name": purch_ledger or "Purchase Item",
                "quantity": 1,
                "rate": subtotal or total_amt,
                "amount": subtotal or total_amt
            }]

        processed_items = []
        for it in line_items:
            it_name = str(it.get("item_name") or it.get("name") or "Purchase Item").strip()
            name_l = it_name.lower()
            is_round_off  = any(k in name_l for k in ["round off", "rounding off", "round-off", "rounding", "r/o"])
            is_transport  = any(k in name_l for k in ["transport", "transpotation", "transportation", "vehicle"])
            is_freight    = any(k in name_l for k in ["freight", "fright", "cartage", "courier", "postage"])
            is_charge_item = is_round_off or is_transport or is_freight or it.get("is_additional_charge", False)

            if is_round_off:
                it_account = "Round Off"
                item_type = "service"
            elif is_transport:
                it_account = "Transportation Charges"
                item_type = "service"
            elif is_freight or it.get("is_additional_charge"):
                it_account = "Freight Charges"
                item_type = "service"
            else:
                it_account = "Inventory Asset"
                item_type = "goods"

            qty_str = str(it.get('quantity') or "1").split()[0]
            try: it_qty = float(qty_str)
            except: it_qty = 1.0
            raw_rate = float(it.get('rate') or 0.0)
            raw_amt = float(it.get('amount') or (it_qty * raw_rate))
            if it_qty > 0 and raw_amt > 0 and (raw_rate == 0.0 or abs(it_qty * raw_rate - raw_amt) > 0.05):
                it_rate = round(raw_amt / it_qty, 4)
                it_amt = round(it_qty * it_rate, 2)
            else:
                it_rate = raw_rate
                it_amt = round(it_qty * raw_rate, 2) if it_qty > 0 and raw_rate > 0 else raw_amt

            if is_charge_item or not tax_info:
                line_tax_id = ""
                line_tax_name = ""
                line_tax_pct = ""
                line_tax_type = ""
                line_tax_amt = 0.0
            else:
                line_tax_id = cur_tax_id
                line_tax_name = cur_tax
                line_tax_pct = cur_tax_pct
                line_tax_type = item_tax_type
                line_tax_amt = round(it_amt * (tax_perc_val / 100.0), 2)

            if is_pre_gst or not tax_info or not line_tax_id:
                cgst_rate = 0.0; sgst_rate = 0.0; igst_rate = 0.0
                cgst_amt = 0.0; sgst_amt = 0.0; igst_amt = 0.0
            elif is_interstate:
                cgst_rate = 0.0; sgst_rate = 0.0; igst_rate = float(line_tax_pct or 0)
                cgst_amt = 0.0; sgst_amt = 0.0; igst_amt = line_tax_amt
            else:
                half_pct = round(float(line_tax_pct or 0) / 2.0, 2)
                half_amt = round(line_tax_amt / 2.0, 2)
                cgst_rate = half_pct; sgst_rate = half_pct; igst_rate = 0.0
                cgst_amt = half_amt; sgst_amt = half_amt; igst_amt = 0.0

            matched_item_id = ""
            if not is_charge_item and item_map:
                matched_item = find_item_in_zoho(it_name, item_map)
                matched_item_id = matched_item.get("item_id", "") if matched_item else ""

            processed_items.append({
                "it_name": it_name,
                "it_account": it_account,
                "item_type": item_type,
                "is_charge_item": is_charge_item,
                "it_qty": it_qty,
                "it_rate": it_rate,
                "it_amt": it_amt,
                "line_tax_id": line_tax_id,
                "line_tax_name": line_tax_name,
                "line_tax_pct": line_tax_pct,
                "line_tax_type": line_tax_type,
                "line_tax_amt": line_tax_amt,
                "cgst_rate": cgst_rate,
                "sgst_rate": sgst_rate,
                "igst_rate": igst_rate,
                "cgst_amt": cgst_amt,
                "sgst_amt": sgst_amt,
                "igst_amt": igst_amt,
                "matched_item_id": matched_item_id
            })

        subtotal_calc = round(sum(x["it_amt"] for x in processed_items), 2)
        tax_calc = round(sum(x["line_tax_amt"] for x in processed_items if x["line_tax_id"]), 2)
        zoho_calc_total = round(subtotal_calc + tax_calc, 2)

        tally_target_total = float(bill.get("total_amount") or err.get("amount") or 0.0)
        if tally_target_total <= 0:
            tally_target_total = round(subtotal_calc + tax_calc + rounding, 2)

        calc_adjustment = round(tally_target_total - zoho_calc_total, 2)
        adj_val = calc_adjustment if abs(calc_adjustment) > 0.001 else 0.0
        adj_desc = "Rounding Off / Tax Adjustment" if abs(adj_val) > 0.001 else ""

        for pit in processed_items:
            row_dict = {h: "" for h in ZOHO_BILL_EXPORT_HEADERS}
            row_dict["Bill Date"] = bill_date_str
            row_dict["Due Date"] = due_date_str
            row_dict["Bill ID"] = bill.get("zoho_bill_id", "")
            row_dict["Vendor Name"] = vendor_name
            row_dict["Entity Discount Percent"] = "0.00"
            row_dict["Payment Terms"] = clean_terms
            row_dict["Payment Terms Label"] = terms_label
            row_dict["Bill Number"] = raw_bill_no
            row_dict["PurchaseOrder"] = ref_num
            row_dict["Currency Code"] = "INR"
            row_dict["Exchange Rate"] = 1.0
            row_dict["SubTotal"] = subtotal_calc
            row_dict["Total"] = tally_target_total
            row_dict["Balance"] = tally_target_total
            row_dict["TotalRetentionAmountFCY"] = 0.0
            row_dict["TotalRetentionAmountBCY"] = 0.0
            row_dict["Vendor Notes"] = narration
            row_dict["Adjustment"] = adj_val
            row_dict["Adjustment Description"] = adj_desc
            row_dict["Branch ID"] = "3962933000000032109"
            row_dict["Branch Name"] = "Bengaluru"
            row_dict["Location Name"] = "Bengaluru"
            row_dict["Is Inclusive Tax"] = "false"
            row_dict["Bill Status"] = "Overdue"
            row_dict["Created By"] = "info"
            row_dict["Product ID"] = pit["matched_item_id"] if not pit["is_charge_item"] else ""
            row_dict["Item Name"] = pit["it_name"] if not pit["is_charge_item"] else ""
            row_dict["Account"] = pit["it_account"]
            row_dict["Description"] = pit["it_name"]
            row_dict["Quantity"] = pit["it_qty"]
            row_dict["Usage unit"] = "nos"
            row_dict["Tax Amount"] = pit["line_tax_amt"]
            row_dict["Item Total"] = pit["it_amt"]
            row_dict["Is Billable"] = "false"
            row_dict["Source of Supply"] = pos_short
            row_dict["Destination of Supply"] = "KA"
            row_dict["GST Treatment"] = "business_gst" if not is_pre_gst else ""
            row_dict["GST Identification Number (GSTIN)"] = vendor_gstin
            row_dict["TDS Calculation Type"] = "item_level"
            row_dict["TDS Percentage"] = "0.00"
            row_dict["TDS Amount"] = "0.000"
            row_dict["Line Item Location Name"] = "Bengaluru"
            row_dict["Rate"] = pit["it_rate"]
            row_dict["Discount Type"] = "entity_level"
            row_dict["Is Discount Before Tax"] = "true"
            row_dict["Discount"] = "0.00"
            row_dict["Discount Amount"] = "0.0"
            row_dict["Tax ID"] = pit["line_tax_id"]
            row_dict["Tax Name"] = pit["line_tax_name"]
            row_dict["Tax Percentage"] = pit["line_tax_pct"]
            row_dict["Tax Type"] = pit["line_tax_type"]
            row_dict["Item TDS Amount"] = "0.000"
            row_dict["Item Type"] = pit["item_type"]
            row_dict["ITC Eligibility"] = "eligible"
            row_dict["Entity Discount Amount"] = "0.000"
            row_dict["Is Landed Cost"] = "false"
            row_dict["CGST Rate %"] = pit["cgst_rate"]
            row_dict["SGST Rate %"] = pit["sgst_rate"]
            row_dict["IGST Rate %"] = pit["igst_rate"]
            row_dict["CESS Rate %"] = 0.00
            row_dict["CGST(FCY)"] = pit["cgst_amt"]
            row_dict["SGST(FCY)"] = pit["sgst_amt"]
            row_dict["IGST(FCY)"] = pit["igst_amt"]
            row_dict["CESS(FCY)"] = 0.00
            row_dict["CGST"] = pit["cgst_amt"]
            row_dict["SGST"] = pit["sgst_amt"]
            row_dict["IGST"] = pit["igst_amt"]
            row_dict["CESS"] = 0.00

            row_vals = [row_dict[h] for h in ZOHO_BILL_EXPORT_HEADERS]
            row_vals.append(err_msg)
            row_vals.append(action)

            ws_bills.append(row_vals)
            ws_bills.row_dimensions[current_row_idx].height = 20
            row_fill = zebra_fill if (current_row_idx % 2 == 0) else white_fill

            for c_idx in range(1, len(row_vals) + 1):
                c = ws_bills.cell(row=current_row_idx, column=c_idx)
                c.fill = row_fill
                c.border = thin_border
                c.font = err_font if c_idx > len(ZOHO_BILL_EXPORT_HEADERS) else data_font

            current_row_idx += 1

    # -------------------------------------------------------------
    # SHEET 2: Error Summary Dashboard
    # -------------------------------------------------------------
    ws_sum = wb.create_sheet(title="Error Summary")
    ws_sum.views.sheetView[0].showGridLines = True

    sum_headers = ["S.No", "Bill Number", "Bill Date", "Vendor Name", "Total Amount (₹)", "Sync Status", "Error Message / Reason", "Action Required / Fix", "Logged At"]
    ws_sum.append(sum_headers)
    ws_sum.row_dimensions[1].height = 28

    for col_num in range(1, len(sum_headers) + 1):
        cell = ws_sum.cell(row=1, column=col_num)
        cell.fill = header_fill_red
        cell.font = header_font
        cell.alignment = header_align

    s_row_idx = 2
    for s_idx, err in enumerate(errors_list, 1):
        b_no = str(err.get("bill_number") or err.get("invoice_number") or "-")
        raw_date = str(err.get("date") or err.get("bill_date") or "")
        b_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 and raw_date.isdigit() else raw_date
        vendor = str(err.get("vendor") or err.get("vendor_name") or "-")
        amount = float(err.get("amount") or err.get("total_amount") or 0.0)
        err_msg = str(err.get("error") or err.get("error_message") or "Unknown error")
        log_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg_l = err_msg.lower()
        if "not found in zoho books" in msg_l or "create this goods item" in msg_l:
            action = "Create missing Goods/Service item in Zoho Books Items list"
        elif "vendor" in msg_l or "contact" in msg_l:
            action = "Verify Vendor contact name and GSTIN in Zoho Books Contacts"
        elif "tax" in msg_l or "gst" in msg_l:
            action = "Check tax rate and active tax mappings in Zoho Books Taxes"
        elif "account" in msg_l:
            action = "Check Chart of Accounts for required Purchase / Expense ledger"
        elif "already exists" in msg_l:
            action = "Bill already present in Zoho Books for this vendor"
        elif "token" in msg_l or "unauthorized" in msg_l:
            action = "Re-authenticate Zoho OAuth credentials"
        else:
            action = "Review bill line items and tax configuration in Zoho Books"

        s_vals = [s_idx, b_no, b_date, vendor, amount, "FAILED", err_msg, action, log_time]
        ws_sum.append(s_vals)
        ws_sum.row_dimensions[s_row_idx].height = 22
        r_fill = zebra_fill if (s_row_idx % 2 == 0) else white_fill

        for c_idx in range(1, len(s_vals) + 1):
            c = ws_sum.cell(row=s_row_idx, column=c_idx)
            c.font = err_font if c_idx in [6, 7] else data_font
            c.fill = r_fill
            c.border = thin_border
            if c_idx == 5:
                c.alignment = Alignment(horizontal="right", vertical="center")
                c.number_format = '#,##0.00'
            elif c_idx in [1, 2, 6, 9]:
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")
        s_row_idx += 1

    # Auto column widths
    for ws_curr in [ws_bills, ws_sum]:
        for col in ws_curr.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max(len(str(cell.value or '')) for cell in col[:50])
            ws_curr.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

    if output_path:
        wb.save(output_path)
        return output_path

    out_stream = io.BytesIO()
    wb.save(out_stream)
    out_stream.seek(0)
    return out_stream.getvalue()



