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

from journel.journel_backend import _get_creds

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
    
    vendor_payment_terms_cache[vendor_name] = ""
    return ""

def get_payment_terms_hierarchical(voucher, party_name):
    """
    Extract payment terms using hierarchical method:
    1. Check BILLALLOCATIONS.LIST → BILLCREDITPERIOD
    2. Check BASICDUEDATEOFPYMT field
    3. Search for patterns like "30 days", "45 days" in purchase order text
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
    
    # Method 3: Search for payment term patterns in entire Purchase Order text
    voucher_text = str(voucher)
    patterns = [
        r'(\d+)\s*days?',
        r'net\s*(\d+)',
        r'(\d+)\s*days?\s*credit',
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

    def get_all_subgroups(group_name, visited=None):
        if visited is None: visited = set()
        if group_name in visited: return set()
        visited.add(group_name)
        results = {group_name}
        for child in children_map.get(group_name, []):
            results.update(get_all_subgroups(child, visited))
        return results

    creditor_groups = get_all_subgroups("Sundry Creditors")

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

def fetch_tally_purchase_orders(purchase_order_number="1"):
    """Fetch a specific Purchase Order by voucher number from Tally"""
    ledger_map = get_ledger_map_from_tally()
    
    print(f"[TALLY] Fetching Purchase Order with voucher number: {purchase_order_number}...")
    
    # Fetch all Purchase Orders without date restriction to find the specific PO
    xml_request = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Purchase Order</VOUCHERTYPENAME>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        print(f"[TALLY] Searching for Purchase Order '{purchase_order_number}' in all dates...")
        response = requests.post(TALLY_URL, data=xml_request, timeout=30)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        # Find the specific voucher by number
        all_vouchers = soup.find_all('VOUCHER')
        print(f"[TALLY] Total Purchase Order vouchers found: {len(all_vouchers)}")
        
        vouchers = []
        for v in all_vouchers:
            v_no = v.find('VOUCHERNUMBER')
            if v_no and v_no.text.strip() == purchase_order_number:
                vouchers.append(v)
                print(f"[TALLY]  Found Purchase Order #{purchase_order_number}")
                break
        
        if not vouchers:
            print(f"[ERROR] Purchase Order '{purchase_order_number}' not found!")
            print(f"[INFO] Total {len(all_vouchers)} purchase orders were searched.")
            print(f"[HINT] Please check the exact voucher number format in Tally.")
            print(f"[HINT] Example voucher numbers from search:")
            for i, v in enumerate(all_vouchers[:5]):
                v_no = v.find('VOUCHERNUMBER')
                if v_no:
                    print(f"  - {v_no.text.strip()}")
            return []

        purchase_order_data = []
        for idx, v in enumerate(vouchers, 1):
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get vendor from PARTYNAME field
            vendor_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            
            # Get Reference Number (vendor PO Number)
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            
            # Get vendor Address
            vendor_address = []
            buyer_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if buyer_addr_list:
                for addr in buyer_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        vendor_address.append(addr.text.strip())
            
            # Get Payment Terms using hierarchical method
            payment_terms = get_payment_terms_hierarchical(v, vendor_name)
            
            # Get Order Status
            order_status = v.find('ORDERSTATUS').text if v.find('ORDERSTATUS') else "Pending"
            
            # Get Purchase Ledger using HIERARCHY METHOD (same as tally_purchase_order.py)
            # Method 1: Try to get from stock item's ledger account
            purchase_ledger = ""
            purchase_ledger_from_item = ""
            
            # First, try to get purchase ledger from inventory entries
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    purchase_ledger_from_item = item_ledger.text.strip()
                    break
            
            # Method 2: If not found in items, find the ledger with LARGEST POSITIVE amount
            # (excluding vendor, taxes, and rounding) - for purchases, the ledger has positive amount
            if not purchase_ledger_from_item:
                max_positive_amount = 0
                for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                    name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amt_tag = entry.find('AMOUNT')
                    if amt_tag and amt_tag.text:
                        numbers = re.findall(r'[-\d.]+', amt_tag.text.strip())
                        if numbers:
                            amt = float(numbers[-1])
                        else:
                            amt = 0.0
                    else:
                        amt = 0.0
                    
                    name_lower = name.lower()
                    if name == vendor_name:  # Skip vendor
                        continue
                    if 'cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower:  # Skip taxes
                        continue
                    if 'rounding' in name_lower:  # Skip rounding
                        continue
                    
                    # Find the ledger with largest positive amount (this is the purchase ledger)
                    if amt > max_positive_amount:
                        max_positive_amount = amt
                        purchase_ledger = name
            else:
                purchase_ledger = purchase_ledger_from_item
            
            # Get Line Items
            line_items = []
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_name = item.find('STOCKITEMNAME').text.strip() if item.find('STOCKITEMNAME') else ""
                
                qty_tag = item.find('ACTUALQTY') or item.find('BILLEDQTY')
                quantity = qty_tag.text.strip() if qty_tag else "0"
                
                rate_tag = item.find('RATE')
                if rate_tag and rate_tag.text:
                    rate_text = rate_tag.text.split('/')[0].strip()
                    numbers = re.findall(r'[-\d.]+', rate_text)
                    if numbers:
                        rate = float(numbers[-1])
                    else:
                        rate = 0.0
                else:
                    rate = 0.0
                
                discount_tag = item.find('DISCOUNT')
                discount = discount_tag.text.strip() if discount_tag else "0"
                
                amount_tag = item.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    if numbers:
                        amount = float(numbers[-1])
                    else:
                        amount = 0.0
                else:
                    amount = 0.0
                
                # Get reporting tags (Category and Cost Centre) from Tally
                category = ""
                cost_centre = ""
                cat_alloc = item.find('CATEGORYALLOCATIONS.LIST')
                if cat_alloc:
                    category_tag = cat_alloc.find('CATEGORY')
                    if category_tag:
                        category = category_tag.text.strip()
                    cc_list = cat_alloc.find('COSTCENTREALLOCATIONS.LIST')
                    if cc_list:
                        cc_name = cc_list.find('NAME')
                        if cc_name:
                            cost_centre = cc_name.text.strip()
                
                line_items.append({
                    "item_name": item_name,
                    "quantity": quantity,
                    "rate": rate,
                    "discount": discount,
                    "amount": abs(amount),
                    "category": category,
                    "cost_centre": cost_centre
                })
            
            # Get Tax Details
            taxes = []
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
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
                
                name_lower = name.lower()
                if ('cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower):
                    tax_type = ""
                    if 'cgst' in name_lower:
                        tax_type = "CGST"
                    elif 'sgst' in name_lower:
                        tax_type = "SGST"
                    elif 'igst' in name_lower:
                        tax_type = "IGST"
                    
                    rate = ""
                    if '%' in name:
                        rate = name.split('%')[0].split()[-1]
                    
                    taxes.append({
                        "tax_type": tax_type,
                        "tax_name": name,
                        "tax_rate": rate,  # Changed from "rate"
                        "tax_amount": abs(amt)  # Changed from "amount"
                    })
            
            # Get Rounding Off
            rounding_off = 0.0
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                if 'rounding' in name.lower():
                    amount_tag = entry.find('AMOUNT')
                    if amount_tag and amount_tag.text:
                        amount_text = amount_tag.text.strip()
                        numbers = re.findall(r'[-\d.]+', amount_text)
                        if numbers:
                            rounding_off = float(numbers[-1])
                    break
            
            purchase_order_data.append({
                "purchase_order_number": v_no,
                "date": v_date,
                "vendor_name": vendor_name,
                "reference_number": reference_number,
                "vendor_address": vendor_address,
                "payment_terms": payment_terms,
                "order_status": order_status,
                "purchase_ledger": purchase_ledger,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": rounding_off,
                "narration": narration
            })
        
        return purchase_order_data
    except Exception as e:
        print(f"Error fetching Purchase Orders from Tally: {e}")
        import traceback
        traceback.print_exc()
        return []

def create_zoho_purchase_order(token, so_data, contact_map, account_map, payment_terms_map, tax_map, tag_map, item_map):
    """Create a Purchase Order in Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    # Convert Tally date format (YYYYMMDD) to Zoho format (YYYY-MM-DD)
    tally_date = so_data["date"]
    if len(tally_date) == 8:
        zoho_date = f"{tally_date[0:4]}-{tally_date[4:6]}-{tally_date[6:8]}"
    else:
        zoho_date = tally_date
    
    print(f"\n{'='*100}")
    print(f"[Purchase Order] Processing Purchase Order #{so_data['purchase_order_number']} - Date: {tally_date}")
    print(f"{'='*100}")
    
    # Find vendor in Zoho Books
    vendor_info, match_score = find_vendor_in_zoho(so_data["vendor_name"], contact_map)
    
    if not vendor_info:
        error_msg = f"Vendor '{so_data['vendor_name']}' not found in Zoho Books"
        print(f"  [ERROR] {error_msg}")
        print(f"  [ACTION REQUIRED] Please create this vendor in Zoho Books manually and run again.")
        print(f"  [SKIPPING] Skipping this Purchase Order...")
        return {"success": False, "error": error_msg}
    
    if match_score == 100:
        print(f"  [EXACT MATCH] Found vendor: {vendor_info['contact_name']}")
    else:
        if match_score < 80:
            error_msg = f"Low confidence match for vendor '{so_data['vendor_name']}' (Score: {match_score}%)"
            print(f"  [WARNING] {error_msg}")
            print(f"  [MATCH] Best match: '{vendor_info['contact_name']}' (Score: {match_score}%)")
            print(f"  [ACTION] Please verify this is correct before proceeding")
            return {"success": False, "error": error_msg}
        else:
            print(f"  [FUZZY MATCH] Matched '{so_data['vendor_name']}' → '{vendor_info['contact_name']}' (Score: {match_score}%)")
    
    print(f"  [vendor] {vendor_info['contact_name']} (ID: {vendor_info['contact_id']})")
    
    # Display additional info
    if so_data.get('reference_number'):
        print(f"  [REF] {so_data['reference_number']}")
    if so_data.get('payment_terms'):
        print(f"  [TERMS] {so_data['payment_terms']}")
    if so_data.get('order_status'):
        print(f"  [STATUS] {so_data['order_status']}")
    
    # Build line items
    zoho_line_items = []
    
    # Calculate total tax rate
    total_tax_rate = calculate_total_tax_rate(so_data["taxes"])
    
    # Detect tax type: Check if Tally has CGST+SGST (intrastate) or IGST (interstate)
    has_cgst = any('cgst' in tax.get('tax_type', '').lower() for tax in so_data["taxes"])
    has_sgst = any('sgst' in tax.get('tax_type', '').lower() for tax in so_data["taxes"])
    has_igst = any('igst' in tax.get('tax_type', '').lower() for tax in so_data["taxes"])
    
    is_intrastate = has_cgst and has_sgst
    is_interstate = has_igst
    
    # Get tax ID from Zoho based on total rate AND tax type
    tax_info = None
    
    # First, try to find exact match by rate
    if total_tax_rate > 0:
        # Search for appropriate tax based on transaction type
        for key, val in tax_map.items():
            if isinstance(key, float) and key == total_tax_rate:
                tax_name_lower = val.get('tax_name', '').lower()
                
                # For intrastate (CGST+SGST), avoid IGST
                if is_intrastate and 'igst' not in tax_name_lower:
                    tax_info = val
                    print(f"  [TAX MATCH] Intrastate transaction - Using {val['tax_name']} ({total_tax_rate}%)")
                    break
                # For interstate (IGST), prefer IGST
                elif is_interstate and 'igst' in tax_name_lower:
                    tax_info = val
                    print(f"  [TAX MATCH] Interstate transaction - Using {val['tax_name']} ({total_tax_rate}%)")
                    break
    
    # If exact tax not found OR tax rate is 0%, use default 18% GST (not IGST - for intrastate)
    if not tax_info:
        if total_tax_rate > 0:
            print(f"  [WARNING] No matching tax found for {total_tax_rate}% ({'Intrastate' if is_intrastate else 'Interstate'})")
            print(f"  [WARNING] Available taxes: {', '.join([str(k) for k in tax_map.keys() if isinstance(k, float)])}")
        else:
            print(f"  [INFO] Tax rate is 0% - using default 18% GST")
        
        # Try to use GST18 (not IGST18) as default for intrastate transactions
        # First check the _gst_taxes map which contains only GST (CGST+SGST) taxes
        default_tax = tax_map.get("_gst_taxes", {}).get(18.0) or tax_map.get("gst18")
        if not default_tax:
            # If GST18 not found, try to find any 18% tax that's not IGST
            for key, val in tax_map.items():
                if isinstance(key, str) and "18" in key and "igst" not in key.lower():
                    default_tax = val
                    break
        
        if default_tax:
            print(f"  [DEFAULT] Using default tax: {default_tax['tax_name']} (18%) instead of {total_tax_rate}%")
            tax_info = default_tax
        else:
            print(f"  [ERROR] No default 18% GST tax found!")
    
    for item in so_data["line_items"]:
        print(f"  [ITEM] {item['item_name']} - Qty: {item['quantity']} @ Rs.{item['rate']}")
        
        # Find purchase account
        purchase_account_id = None
        if so_data.get('purchase_ledger'):
            purchase_account = account_map.get(so_data['purchase_ledger'].lower())
            if purchase_account:
                purchase_account_id = purchase_account['account_id']
        
        # Parse quantity
        qty_str = item['quantity'].split()[0] if item['quantity'] else "1"
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
            "name": item['item_name'],
            "description": item['item_name'],
            "rate": item['rate'],
            "quantity": qty,
            "discount": discount,
        }

    # Construct Payload
    payload = {
        "vendor_id": vendor_info["contact_id"],
        "date": zoho_date,
        "line_items": zoho_line_items,
        "notes": so_data.get("narration", "")
    }

    try:
        res = requests.post(f"{creds['base_url']}/purchaseorders", headers=headers, params=params, json=payload)
        
        if res.status_code in [200, 201] and res.json().get("code") == 0:
            so_id = res.json().get("purchaseorder", {}).get("purchaseorder_id", "N/A")
            print(f"   SUCCESS! Purchase Order created with ID: {so_id}")
            
            # Optionally mark as open
            try:
                open_res = requests.post(
                    f"{creds['base_url']}/purchaseorders/{so_id}/status/open",
                    headers=headers,
                    params={"organization_id": creds["org_id"]}
                )
            except: pass
        else:
            print(f"   [ERROR] Failed to create PO: {res.text}")
    except Exception as e:
        print(f"   [ERROR] Exception creating PO: {e}")

def get_zoho_contacts(token):
    """Fetch all vendor contacts from Zoho Books with pagination"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    
    all_vendors = {}
    page = 1
    per_page = 200
    
    try:
        while True:
            params = {
                "organization_id": creds["org_id"],
                "page": page,
                "per_page": per_page
            }
            
            res = requests.get(f"{creds['base_url']}/contacts", headers=headers, params=params)
            if res.status_code == 200 and res.json().get("code") == 0:
                contacts = res.json().get("contacts", [])
                
                if not contacts:
                    break
                
                # Filter to only vendors
                for c in contacts:
                    if c.get("contact_type") == "vendor":
                        all_vendors[c["contact_name"].lower()] = c
                
                page_context = res.json().get("page_context", {})
                has_more_page = page_context.get("has_more_page", False)
                
                if not has_more_page:
                    break
                
                page += 1
            else:
                print(f"Error fetching contacts on page {page}: {res.status_code}")
                break
        
        return all_vendors
    except Exception as e:
        print(f"Error fetching contacts: {e}")
    return {}

def get_zoho_accounts(token):
    """Fetch all accounts (chart of accounts) from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/chartofaccounts", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            all_accounts = res.json().get("chartofaccounts", [])
            account_map = {acc["account_name"].lower(): acc for acc in all_accounts}
            return account_map
    except Exception as e:
        print(f"Error fetching accounts: {e}")
    return {}

def get_zoho_payment_terms_list(token):
    """Fetch all payment terms from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/settings/paymentterms", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            terms_data = res.json().get("data", {})
            terms_list = terms_data.get("payment_terms", [])
            # Create mapping: "net 30" -> payment_terms_id
            terms_map = {}
            for term in terms_list:
                term_label = term.get("payment_terms_label", "")
                term_id = term.get("payment_terms_id")
                if term_label and term_id:
                    # Map by label (e.g., "Net 30")
                    terms_map[term_label.lower()] = term_id
            return terms_map
    except Exception as e:
        print(f"  [WARNING] Error fetching payment terms: {e}")
    return {}

def get_zoho_taxes(token):
    """Fetch tax rates from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/settings/taxes", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            all_taxes = res.json().get("taxes", [])
            
            tax_map = {}
            for tax in all_taxes:
                tax_name = tax.get("tax_name", "").lower()
                tax_rate = float(tax.get("tax_percentage", 0))
                
                # Store by rate
                tax_map[tax_rate] = {
                    "tax_id": tax["tax_id"],
                    "tax_name": tax["tax_name"],
                    "tax_percentage": tax_rate
                }
                
                # Also store by name for lookup
                tax_map[tax_name] = {
                    "tax_id": tax["tax_id"],
                    "tax_name": tax["tax_name"],
                    "tax_percentage": tax_rate
                }
            
            return tax_map
    except Exception as e:
        print(f"Error fetching taxes: {e}")
    return {}

def get_zoho_tags(token):
    """Fetch all tags from Zoho Books using reporting_tags API"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    tag_map = {}
    try:
        # Get list of all tag categories
        res = requests.get(f"{creds['base_url']}/settings/tags", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            # Use 'reporting_tags' key instead of 'tags'
            categories = res.json().get("reporting_tags", [])
            
            # Loop through each category to get detailed options
            for category in categories:
                tag_id = category.get("tag_id")
                tag_name = category.get("tag_name")
                
                # Get detailed options for this tag
                detail_res = requests.get(f"{creds['base_url']}/settings/tags/{tag_id}", headers=headers, params=params)
                if detail_res.status_code == 200:
                    detail_data = detail_res.json()
                    tag_obj = detail_data.get("tag", detail_data.get("reporting_tag", {}))
                    # Use 'tag_options' instead of 'tag_option'
                    options = tag_obj.get("tag_options", [])
                    
                    for option in options:
                        option_name = option.get("tag_option_name", "")
                        option_id = option.get("tag_option_id")
                        if option_name and option_id:
                            # Map by option name for easier lookup
                            tag_map[option_name.lower()] = {
                                "tag_id": tag_id,
                                "tag_option_id": option_id,
                                "tag_name": tag_name,
                                "tag_option_name": option_name
                            }
    except Exception as e:
        print(f"  [WARNING] Error fetching tags: {e}")
    return tag_map

def get_zoho_items(token):
    """Fetch all items from Zoho Books with their reporting tags"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    
    all_items = {}
    page = 1
    per_page = 200
    
    try:
        while True:
            params = {
                "organization_id": creds["org_id"],
                "page": page,
                "per_page": per_page
            }
            
            res = requests.get(f"{creds['base_url']}/items", headers=headers, params=params)
            if res.status_code == 200 and res.json().get("code") == 0:
                items = res.json().get("items", [])
                
                if not items:
                    break
                
                # Store items with their tags
                for item in items:
                    item_name = item.get("name", "").lower()
                    all_items[item_name] = {
                        "item_id": item.get("item_id"),
                        "name": item.get("name"),
                        "tags": item.get("tags", [])  # This contains the reporting tags
                    }
                
                page_context = res.json().get("page_context", {})
                has_more_page = page_context.get("has_more_page", False)
                
                if not has_more_page:
                    break
                
                page += 1
            else:
                print(f"Error fetching items on page {page}: {res.status_code}")
                break
        
        return all_items
    except Exception as e:
        print(f"Error fetching items: {e}")
    return {}


def calculate_total_tax_rate(taxes):
    """Calculate total tax rate from CGST + SGST or IGST"""
    total_rate = 0.0
    for tax in taxes:
        if tax.get("tax_rate"):  # Changed from "rate" to "tax_rate"
            try:
                total_rate += float(tax["tax_rate"])
            except:
                pass
    return total_rate

def find_vendor_in_zoho(vendor_name, contact_map):
    """Find vendor in Zoho Books using exact match or fuzzy matching"""
    vendor_lower = vendor_name.lower()
    
    # Try exact match first
    if vendor_lower in contact_map:
        return contact_map[vendor_lower], 100
    
    # Try fuzzy matching
    best_match = None
    best_score = 0
    
    for zoho_name, contact in contact_map.items():
        score = fuzz.ratio(vendor_lower, zoho_name)
        if score > best_score:
            best_score = score
            best_match = contact
    
    return best_match, best_score

def create_zoho_purchase_order(token, so_data, contact_map, account_map, payment_terms_map, tax_map, tag_map, item_map):
    """Create a Purchase Order in Zoho Books"""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": ORGANIZATION_ID}
    
    # Convert Tally date format (YYYYMMDD) to Zoho format (YYYY-MM-DD)
    tally_date = so_data["date"]
    if len(tally_date) == 8:
        zoho_date = f"{tally_date[0:4]}-{tally_date[4:6]}-{tally_date[6:8]}"
    else:
        zoho_date = tally_date
    
    print(f"\n{'='*100}")
    print(f"[Purchase Order] Processing Purchase Order #{so_data['purchase_order_number']} - Date: {tally_date}")
    print(f"{'='*100}")
    
    # Find vendor in Zoho Books
    vendor_info, match_score = find_vendor_in_zoho(so_data["vendor_name"], contact_map)
    
    if not vendor_info:
        error_msg = f"Vendor '{so_data['vendor_name']}' not found in Zoho Books"
        print(f"  [ERROR] {error_msg}")
        print(f"  [ACTION REQUIRED] Please create this vendor in Zoho Books manually and run again.")
        print(f"  [SKIPPING] Skipping this Purchase Order...")
        return {"success": False, "error": error_msg}
    
    if match_score == 100:
        print(f"  [EXACT MATCH] Found vendor: {vendor_info['contact_name']}")
    else:
        if match_score < 80:
            error_msg = f"Low confidence match for vendor '{so_data['vendor_name']}' (Score: {match_score}%)"
            print(f"  [WARNING] {error_msg}")
            print(f"  [MATCH] Best match: '{vendor_info['contact_name']}' (Score: {match_score}%)")
            print(f"  [ACTION] Please verify this is correct before proceeding")
            return {"success": False, "error": error_msg}
        else:
            print(f"  [FUZZY MATCH] Matched '{so_data['vendor_name']}' → '{vendor_info['contact_name']}' (Score: {match_score}%)")
    
    print(f"  [vendor] {vendor_info['contact_name']} (ID: {vendor_info['contact_id']})")
    
    # Display additional info
    if so_data.get('reference_number'):
        print(f"  [REF] {so_data['reference_number']}")
    if so_data.get('payment_terms'):
        print(f"  [TERMS] {so_data['payment_terms']}")
    if so_data.get('order_status'):
        print(f"  [STATUS] {so_data['order_status']}")
    
    # Build line items
    zoho_line_items = []
    
    # Calculate total tax rate
    total_tax_rate = calculate_total_tax_rate(so_data["taxes"])
    
    # Detect tax type: Check if Tally has CGST+SGST (intrastate) or IGST (interstate)
    has_cgst = any('cgst' in tax.get('tax_type', '').lower() for tax in so_data["taxes"])
    has_sgst = any('sgst' in tax.get('tax_type', '').lower() for tax in so_data["taxes"])
    has_igst = any('igst' in tax.get('tax_type', '').lower() for tax in so_data["taxes"])
    
    is_intrastate = has_cgst and has_sgst
    is_interstate = has_igst
    
    # Get tax ID from Zoho based on total rate AND tax type
    tax_info = None
    
    # First, try to find exact match by rate
    if total_tax_rate > 0:
        # Search for appropriate tax based on transaction type
        for key, val in tax_map.items():
            if isinstance(key, float) and key == total_tax_rate:
                tax_name_lower = val.get('tax_name', '').lower()
                
                # For intrastate (CGST+SGST), avoid IGST
                if is_intrastate and 'igst' not in tax_name_lower:
                    tax_info = val
                    print(f"  [TAX MATCH] Intrastate transaction - Using {val['tax_name']} ({total_tax_rate}%)")
                    break
                # For interstate (IGST), prefer IGST
                elif is_interstate and 'igst' in tax_name_lower:
                    tax_info = val
                    print(f"  [TAX MATCH] Interstate transaction - Using {val['tax_name']} ({total_tax_rate}%)")
                    break
    
    # If exact tax not found OR tax rate is 0%, use default 18% GST (not IGST - for intrastate)
    if not tax_info:
        if total_tax_rate > 0:
            print(f"  [WARNING] No matching tax found for {total_tax_rate}% ({'Intrastate' if is_intrastate else 'Interstate'})")
            print(f"  [WARNING] Available taxes: {', '.join([str(k) for k in tax_map.keys() if isinstance(k, float)])}")
        else:
            print(f"  [INFO] Tax rate is 0% - using default 18% GST")
        
        # Try to use GST18 (not IGST18) as default for intrastate transactions
        # First check the _gst_taxes map which contains only GST (CGST+SGST) taxes
        default_tax = tax_map.get("_gst_taxes", {}).get(18.0) or tax_map.get("gst18")
        if not default_tax:
            # If GST18 not found, try to find any 18% tax that's not IGST
            for key, val in tax_map.items():
                if isinstance(key, str) and "18" in key and "igst" not in key.lower():
                    default_tax = val
                    break
        
        if default_tax:
            print(f"  [DEFAULT] Using default tax: {default_tax['tax_name']} (18%) instead of {total_tax_rate}%")
            tax_info = default_tax
        else:
            print(f"  [ERROR] No default 18% GST tax found!")
    
    for item in so_data["line_items"]:
        print(f"  [ITEM] {item['item_name']} - Qty: {item['quantity']} @ Rs.{item['rate']}")
        
        # Find purchase account
        purchase_account_id = None
        if so_data.get('purchase_ledger'):
            purchase_account = account_map.get(so_data['purchase_ledger'].lower())
            if purchase_account:
                purchase_account_id = purchase_account['account_id']
        
        # Parse quantity
        qty_str = item['quantity'].split()[0] if item['quantity'] else "1"
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
            "name": item['item_name'],
            "description": item['item_name'],
            "rate": item['rate'],
            "quantity": qty,
            "discount": discount,
        }
        
        # Add tax ID - REQUIRED for Purchase Orders
        if tax_info:
            line_item["tax_id"] = tax_info["tax_id"]
        
        # Add account if found
        if purchase_account_id:
            line_item["account_id"] = purchase_account_id
            print(f"     [ACCOUNT] Using purchase account: {so_data['purchase_ledger']}")
        
        # Add reporting tags - prioritize Tally data, then fall back to Zoho Books item master
        tags = []
        
        # First, try to use category/cost centre from Tally
        if item.get('category') or item.get('cost_centre'):
            print(f"     [TAGS FROM TALLY] Category: {item.get('category', 'N/A')}, Cost Centre: {item.get('cost_centre', 'N/A')}")
            
            # Map Tally category to Zoho Books tags (direct lookup by option name)
            if item.get('category'):
                category_tag = tag_map.get(item['category'].lower())
                if category_tag:
                    tags.append({
                        "tag_id": category_tag["tag_id"],
                        "tag_option_id": category_tag["tag_option_id"]
                    })
                    print(f"       - Mapped Category: {item['category']}")
            
            # Map Tally cost centre to Zoho Books tags (direct lookup by option name)
            if item.get('cost_centre'):
                cc_tag = tag_map.get(item['cost_centre'].lower())
                if cc_tag:
                    tags.append({
                        "tag_id": cc_tag["tag_id"],
                        "tag_option_id": cc_tag["tag_option_id"]
                    })
                    print(f"       - Mapped Cost Centre: {item['cost_centre']}")
        
        # If no tags from Tally, fall back to Zoho Books item master
        if not tags:
            # Look up item in Zoho Books to get its reporting tags
            item_key = item['item_name'].lower()
            zoho_item = item_map.get(item_key)
            
            if zoho_item and zoho_item.get('tags'):
                print(f"     [TAGS FROM ZOHO] Found {len(zoho_item['tags'])} tag(s) for item '{item['item_name']}'")
                tags = zoho_item['tags']
                
                # Display the tags
                for tag in tags:
                    tag_name = tag.get('tag_name', 'N/A')
                    tag_option = tag.get('tag_option_name', 'N/A')
                    print(f"       - {tag_name}: {tag_option}")
            else:
                print(f"     [INFO] No reporting tags found in Zoho for item '{item['item_name']}'")

        
        if tags:
            line_item["tags"] = tags
        
        zoho_line_items.append(line_item)
    
    # Display taxes
    if so_data["taxes"]:
        print(f"\n  [TAX] Taxes:")
        for tax in so_data["taxes"]:
            print(f"     {tax['tax_type']} {tax.get('tax_rate', 'N/A')}%: Rs.{tax.get('tax_amount', 0)}")
        print(f"     Total Tax Rate: {total_tax_rate}%")
    
    # Map payment terms
    payment_terms_id = None
    payment_terms_days = None
    if so_data.get("payment_terms"):
        tally_terms = so_data["payment_terms"].lower().strip()
        
        # Try exact match first
        if tally_terms in payment_terms_map:
            payment_terms_id = payment_terms_map[tally_terms]
        else:
            # Extract number from Tally terms (e.g., "30 Days" -> "30")
            numbers = re.findall(r'\d+', so_data["payment_terms"])
            if numbers:
                days = numbers[0]
                # Try variations
                variations = [
                    f"net {days}",      # "net 30"
                    f"{days} days",     # "30 days"
                    f"net{days}",       # "net30"
                ]
                
                for variation in variations:
                    if variation in payment_terms_map:
                        payment_terms_id = payment_terms_map[variation]
                        payment_terms_days = int(days)
                        break
    
    print(f"\n  [DEBUG] Payment Terms Mapping:")
    print(f"    Tally: '{so_data.get('payment_terms', '')}'")
    print(f"    Mapped ID: {payment_terms_id}")
    print(f"    Days: {payment_terms_days}")
    print(f"    Available terms: {list(payment_terms_map.keys())}")
    
    if not payment_terms_id:
        if so_data.get("payment_terms"):
            print(f"  [WARNING] Payment term '{so_data.get('payment_terms')}' not found in Zoho Books")
    
    # Build payload
    payload = {
        "vendor_id": vendor_info["contact_id"],
        "purchaseorder_number": so_data["purchase_order_number"],  # Insert Tally Purchase Order number (e.g., "INFRA/PI-01/25-26")
        "reference_number": so_data.get("reference_number", ""),  # vendor PO number
        "date": zoho_date,
        "line_items": zoho_line_items,
        "notes": so_data["narration"][:1000] if so_data["narration"] else ""
    }
    
    # Add payment terms if available
    if payment_terms_id and payment_terms_days:
        payload["payment_terms"] = payment_terms_days
        print(f"  [PAYMENT TERMS APPLIED] {payment_terms_days} days (ID: {payment_terms_id})")
    else:
        print(f"  [WARNING] Payment terms not mapped - will use default")
    
    # Add adjustment for rounding off
    if so_data.get("rounding_off"):
        payload["adjustment"] = so_data["rounding_off"]
        print(f"  [ROUNDING] Adjustment: Rs.{so_data['rounding_off']}")
    
    # Debug: Confirm Purchase Order number is in payload
    print(f"\n  [DEBUG] Purchase Order Number in Payload: '{payload.get('purchaseorder_number', 'NOT SET')}'")
    print(f"  [DEBUG] Reference Number in Payload: '{payload.get('reference_number', 'NOT SET')}'")
    
    # Create Purchase Order
    print(f"\n  [CREATE] Creating Purchase Order in Zoho Books...")
    print(f"  Payload: {json.dumps(payload, indent=2)}")
    
    try:
        res = requests.post(f"{BASE_URL}/purchaseorders", headers=headers, params=params, json=payload)
        
        # Log response
        with open("purchaseorder_response.log", "w") as f:
            f.write(f"Status Code: {res.status_code}\n")
            f.write(f"Response: {json.dumps(res.json(), indent=2)}\n")
        
        if res.status_code == 201 and res.json().get("code") == 0:
            so_id = res.json()["purchaseorder"]["purchaseorder_id"]
            print(f"  [SUCCESS] Purchase Order created successfully!")
            print(f"  [ID] Zoho Purchase Order ID: {so_id}")
            
            # Update status if needed (Zoho creates POs as "draft" by default)
            # If Tally status is "Open" or anything other than "Pending"/"Draft", mark as open
            tally_status = so_data.get("order_status", "Pending").lower()
            if tally_status not in ["pending", "draft", ""]:
                print(f"  [STATUS] Tally status is '{so_data.get('order_status')}' - marking PO as open in Zoho...")
                try:
                    # Mark PO as open (issued) in Zoho Books
                    status_res = requests.post(
                        f"{BASE_URL}/purchaseorders/{so_id}/status/open",
                        headers=headers,
                        params=params
                    )
                    if status_res.status_code == 200 and status_res.json().get("code") == 0:
                        print(f"  [SUCCESS] Purchase Order marked as 'open' in Zoho Books")
                    else:
                        print(f"  [WARNING] Could not update status: {status_res.json().get('message', 'Unknown error')}")
                except Exception as e:
                    print(f"  [WARNING] Could not update PO status: {e}")
            else:
                print(f"  [INFO] Purchase Order created as 'draft' (Tally status: {so_data.get('order_status', 'Pending')})")
            
            return {"success": True, "purchaseorder_id": so_id}
        else:
            print(f"  [FAILED] Status: {res.status_code}")
            error_data = res.json()
            error_msg = error_data.get("message", "Unknown error")
            print(f"  Response: {json.dumps(error_data, indent=2)}")
            print(f"  [INFO] Full response saved to purchaseorder_response.log")
            return {"success": False, "error": f"{error_msg} (Code: {error_data.get('code', 'N/A')})"}
    except Exception as e:
        print(f"  [ERROR] Failed to create Purchase Order: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

# ----------------------------------------------------------
# API WRAPPER FOR FRONTEND
# ----------------------------------------------------------

def get_all_purchase_orders_data(from_date="20250401", to_date="20250430", limit=None):
    """
    Wrapper function for API to get purchase order data
    Returns formatted data for frontend display
    """
    try:
        purchase_orders = fetch_tally_purchase_orders_range(from_date, to_date, limit)
        
        if not purchase_orders:
            return None
        
        # Calculate stats
        total_orders = len(purchase_orders)
        total_amount = sum(po.get("total_amount", 0) for po in purchase_orders)
        
        return {
            "purchase_orders": purchase_orders,
            "stats": {
                "total_orders": total_orders,
                "total_amount": round(total_amount, 2),
                "from_date": from_date,
                "to_date": to_date
            }
        }
    except Exception as e:
        print(f" Error in get_all_purchase_orders_data: {e}")
        import traceback
        traceback.print_exc()
        return None

def fetch_tally_purchase_orders_range(from_date="20250401", to_date="20250430", limit=None):
    """
    Fetch Purchase Orders from Tally with ALL fields
    
    Args:
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        limit: Maximum number of purchase orders to fetch
    """
    ledger_map = get_ledger_map_from_tally()
    
    xml_request = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Purchase Order</VOUCHERTYPENAME>
    <SVFROMDATE>{from_date}</SVFROMDATE><SVTODATE>{to_date}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        print(f" Fetching purchase orders from Tally ({from_date} to {to_date})...")
        response = requests.post(TALLY_URL, data=xml_request, timeout=90)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        vouchers = soup.find_all('VOUCHER')
        if limit:
            vouchers = vouchers[:limit]
        
        purchase_order_data = []
        
        for v in vouchers:
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            vendor_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get Reference Number
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            
            # Get Vendor Address
            vendor_address = []
            buyer_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if buyer_addr_list:
                for addr in buyer_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        vendor_address.append(addr.text.strip())
            
            # Get Payment Terms
            payment_terms = get_payment_terms_hierarchical(v, vendor_name)
            
            # Get Order Status
            order_status = v.find('ORDERSTATUS').text if v.find('ORDERSTATUS') else "Pending"
            
            # Get Purchase Ledger
            purchase_ledger = ""
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    purchase_ledger = item_ledger.text.strip()
                    break
            
            if not purchase_ledger:
                max_positive_amount = 0
                for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                    name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amount_tag = entry.find('AMOUNT')
                    if amount_tag and amount_tag.text:
                        numbers = re.findall(r'[-\d.]+', amount_tag.text)
                        amt = float(numbers[-1]) if numbers else 0.0
                    else:
                        amt = 0.0
                    
                    name_lower = name.lower()
                    if name == vendor_name or 'cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower or 'rounding' in name_lower:
                        continue
                    
                    if amt > max_positive_amount:
                        max_positive_amount = amt
                        purchase_ledger = name
            
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
                
                # Get reporting tags
                category = ""
                cost_centre = ""
                cat_alloc = item.find('CATEGORYALLOCATIONS.LIST')
                if cat_alloc:
                    category_tag = cat_alloc.find('CATEGORY')
                    if category_tag:
                        category = category_tag.text.strip()
                    cc_list = cat_alloc.find('COSTCENTREALLOCATIONS.LIST')
                    if cc_list:
                        cc_name = cc_list.find('NAME')
                        if cc_name:
                            cost_centre = cc_name.text.strip()
                
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
            
            # Get tax details - For PURCHASE orders, look for INPUT taxes (not output)
            taxes = []
            tax_total = 0
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                
                amount_tag = entry.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    amt = float(numbers[-1]) if numbers else 0.0
                else:
                    amt = 0.0
                
                name_lower = name.lower()
                # For purchase orders, check for tax ledgers (CGST/SGST/IGST) - no need to filter for "output"
                if ('cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower):
                    tax_rate = ""
                    if '%' in name:
                        tax_rate = name.split('%')[0].split()[-1]
                    
                    tax_type = "CGST" if 'cgst' in name_lower else ("SGST" if 'sgst' in name_lower else "IGST")
                    taxes.append({
                        "tax_name": name,
                        "tax_type": tax_type,
                        "tax_rate": tax_rate,
                        "tax_amount": abs(amt)
                    })
                    tax_total += abs(amt)
            
            # Get rounding off
            rounding_off = 0.0
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                if 'rounding' in name.lower():
                    amount_tag = entry.find('AMOUNT')
                    if amount_tag and amount_tag.text:
                        numbers = re.findall(r'[-\d.]+', amount_tag.text)
                        rounding_off = float(numbers[-1]) if numbers else 0.0
                    break
            
            total_amount = subtotal + tax_total + rounding_off
            
            purchase_order_data.append({
                "purchase_order_number": v_no,
                "date": v_date,
                "vendor_name": vendor_name,
                "reference_number": reference_number,
                "vendor_address": vendor_address,
                "payment_terms": payment_terms,
                "order_status": order_status,
                "purchase_ledger": purchase_ledger,
                "narration": narration,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": rounding_off,
                "subtotal": round(subtotal, 2),
                "tax_total": round(tax_total, 2),
                "total_amount": round(total_amount, 2)
            })
        
        print(f" Fetched {len(purchase_order_data)} purchase order(s)")
        return purchase_order_data
    
    except Exception as e:
        print(f" Error fetching Tally purchase orders: {e}")
        import traceback
        traceback.print_exc()
        return []

def sync_purchase_orders_to_zoho(selected_orders=None, from_date="20250401", to_date="20250430", limit=None):
    """
    Sync purchase orders to Zoho Books.

    Priority order for data source:
      1. selected_orders     — passed directly from frontend (selected rows)
      2. DB (tally_data.db)  — read previously-fetched data, NO second Tally port call
      3. Tally port          — fallback only if DB is also empty for the range
    """
    try:
        print(" Starting Zoho Sync (Purchase Orders)...")

        token = get_access_token()
        if not token:
            return {"status": "error", "message": "Failed to get access token"}

        contact_map      = get_zoho_contacts(token)
        account_map      = get_zoho_accounts(token)
        payment_terms_map= get_zoho_payment_terms_list(token)
        tax_map          = get_zoho_taxes(token)
        tag_map          = get_zoho_tags(token)
        item_map         = get_zoho_items(token)

        # ----------------------------------------------------------------
        # DETERMINE WHAT TO SYNC
        # ----------------------------------------------------------------
        if selected_orders:
            orders_to_sync = selected_orders
            if limit and len(orders_to_sync) > limit:
                orders_to_sync = orders_to_sync[:limit]
            print(f" Using {len(orders_to_sync)} selected purchase order(s) from frontend")

        else:
            orders_to_sync = []
            if database_manager:
                db_rows = database_manager.get_purchase_orders_by_date_range(from_date, to_date)
                if db_rows:
                    for row in db_rows:
                        for field in ('vendor_address', 'line_items', 'taxes'):
                            if row.get(field) and isinstance(row[field], str):
                                try:
                                    row[field] = json.loads(row[field])
                                except Exception:
                                    row[field] = []
                    if limit:
                        db_rows = db_rows[:limit]
                    orders_to_sync = db_rows
                    print(f" Loaded {len(orders_to_sync)} purchase order(s) from DB (no Tally call needed)")

            if not orders_to_sync:
                print(" DB empty for range — fetching from Tally port as fallback...")
                orders_to_sync = fetch_tally_purchase_orders_range(from_date, to_date, limit)

        if not orders_to_sync:
            return {"status": "error", "message": "No purchase orders to sync"}

        print(f" Syncing {len(orders_to_sync)} purchase order(s) to Zoho Books...")

        stats = {"created": 0, "failed": 0, "errors": []}

        for po in orders_to_sync:
            result = create_zoho_purchase_order(token, po, contact_map, account_map,
                                                payment_terms_map, tax_map, tag_map, item_map)
            if result.get("success"):
                stats["created"] += 1
                print(f" Synced Purchase Order #{po['purchase_order_number']}")
            else:
                stats["failed"] += 1
                stats["errors"].append({
                    "purchase_order_number": po['purchase_order_number'],
                    "vendor":               po['vendor_name'],
                    "error":                result.get("error", "Unknown error")
                })
                print(f" Failed Purchase Order #{po['purchase_order_number']}: {result.get('error')}")

        return {"status": "success", "stats": stats}

    except Exception as e:
        print(f" Error in sync_purchase_orders_to_zoho: {e}")
        return {"status": "error", "message": str(e)}


def get_all_purchase_orders_data(from_date="20250401", to_date="20250430", limit=None):
    """
    Wrapper function for API to get purchase order data.
    Fetches from Tally, saves ALL fields to SQLite DB, returns formatted data
    for frontend display.  Mirrors get_all_receipts_data() pattern exactly.
    NOT A SINGLE FIELD IS SKIPPED.
    """
    if database_manager:
        database_manager.init_db()

    try:
        orders = fetch_tally_purchase_orders_range(from_date, to_date, limit)

        if not orders:
            return None

        # ----------------------------------------------------------------
        # SAVE EVERY FIELD TO SQLITE
        # ----------------------------------------------------------------
        if database_manager and orders:
            now = datetime.now().isoformat()
            db_data_list = []

            for po in orders:
                db_data_list.append({
                    # --- Voucher identity ---
                    "purchase_order_number": po.get("purchase_order_number", ""),
                    "date":                  po.get("date", ""),
                    "vendor_name":           po.get("vendor_name", ""),

                    # --- Header fields ---
                    "reference_number": po.get("reference_number", ""),
                    # vendor_address is a list — JSON stringify it
                    "vendor_address":   json.dumps(po.get("vendor_address", [])),
                    "payment_terms":    po.get("payment_terms", ""),
                    "order_status":     po.get("order_status", ""),
                    "purchase_ledger":  po.get("purchase_ledger", ""),
                    "narration":        po.get("narration", ""),

                    # --- line_items: each element has
                    #     item_name, quantity, rate, discount,
                    #     amount, category, cost_centre
                    "line_items": json.dumps(po.get("line_items", [])),

                    # --- taxes: each element has
                    #     tax_name, tax_type, tax_rate, tax_amount
                    "taxes": json.dumps(po.get("taxes", [])),

                    # --- Totals ---
                    "rounding_off": po.get("rounding_off", 0) or 0,
                    "subtotal":     po.get("subtotal",     0) or 0,
                    "tax_total":    po.get("tax_total",    0) or 0,
                    "total_amount": po.get("total_amount", 0) or 0,

                    # --- Fetch range & timestamps ---
                    "from_date":  from_date,
                    "to_date":    to_date,
                    "created_at": now,
                    "updated_at": now,
                })

            database_manager.bulk_save_purchase_orders(db_data_list)
            print(f" Saved {len(orders)} purchase orders to database")

        # ----------------------------------------------------------------
        # Build return stats
        # ----------------------------------------------------------------
        total_orders = len(orders)
        total_amount = sum(po.get("total_amount", 0) for po in orders)

        return {
            "purchase_orders": orders,
            "stats": {
                "total_orders": total_orders,
                "total_amount": round(total_amount, 2),
                "from_date":    from_date,
                "to_date":      to_date
            }
        }

    except Exception as e:
        print(f" Error in get_all_purchase_orders_data: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Main function to migrate Purchase Order from Tally to Zoho Books"""
    print("="*100)
    print("TALLY TO ZOHO BOOKS Purchase Order MIGRATION")
    print("="*100)
    
    # Get Purchase Order number from user
    print("\n[INPUT] Enter the Purchase Order Number from Tally")
    print("        (e.g., 'INFRA/P0-01/25-26' or '1')")
    purchase_order_number = input("Purchase Order Number: ").strip()
    
    if not purchase_order_number:
        print("[ERROR] Purchase Order number cannot be empty!")
        return
    
    # Get access token
    print("\n[AUTH] Authenticating with Zoho Books...")
    token = get_access_token()
    if not token:
        print("[ERROR] Failed to get access token")
        return
    print("[SUCCESS] Authentication successful")
    
    # Fetch contacts, accounts, payment terms, taxes, tags, and items
    print("\n[FETCH] Fetching Zoho Books data...")
    contact_map = get_zoho_contacts(token)
    account_map = get_zoho_accounts(token)
    payment_terms_map = get_zoho_payment_terms_list(token)
    tax_map = get_zoho_taxes(token)
    tag_map = get_zoho_tags(token)
    item_map = get_zoho_items(token)
    print(f"[SUCCESS] Loaded {len(contact_map)} vendors, {len(account_map)} accounts, {len(payment_terms_map)} payment terms, {len([k for k in tax_map.keys() if isinstance(k, float)])} taxes, {len(tag_map)} tags, {len(item_map)} items")
    
    # Fetch Purchase Order from Tally
    print(f"\n[FETCH] Fetching Purchase Order '{purchase_order_number}' from Tally...")
    purchase_orders = fetch_tally_purchase_orders(purchase_order_number=purchase_order_number)
    
    if not purchase_orders:
        print("[ERROR] No Purchase Orders found in Tally")
        return
    
    print(f"[SUCCESS] Found {len(purchase_orders)} Purchase Order(s)")
    
    # Process Purchase Order
    success_count = 0
    for so in purchase_orders:
        if create_zoho_purchase_order(token, so, contact_map, account_map, payment_terms_map, tax_map, tag_map, item_map):
            success_count += 1
    
    print(f"\n{'='*100}")
    print(f"[COMPLETE] MIGRATION COMPLETE: {success_count}/{len(purchase_orders)} Purchase Order(s) created successfully")
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
    
    po_data = []
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
        reference_number = str(v.get('reference', '')).strip()
        
        vendor_address = []
        buyer_addr = v.get('basicbuyeraddress.list', v.get('basicbuyeraddress', []))
        if not isinstance(buyer_addr, list): buyer_addr = [buyer_addr]
        for addr in buyer_addr:
            if isinstance(addr, dict) and 'basicbuyeraddress' in addr:
                vendor_address.append(str(addr['basicbuyeraddress']).strip())
            elif isinstance(addr, str): vendor_address.append(addr.strip())
            
        payment_terms = str(v.get('basicduedateofpymt', '')).strip()
        order_status = str(v.get('orderstatus', 'Pending')).strip()
        purchase_ledger = ""
        
        inventory_entries = v.get('inventoryentries.list', v.get('inventoryentries', v.get('allinventoryentries.list', v.get('allinventoryentries', []))))
        if not isinstance(inventory_entries, list): inventory_entries = [inventory_entries]
        for item in inventory_entries:
            if isinstance(item, dict) and item.get('ledgername'):
                purchase_ledger = str(item['ledgername']).strip()
                break

        ledger_entries = v.get('ledgerentries.list', v.get('ledgerentries', v.get('allledgerentries.list', v.get('allledgerentries', []))))
        if not isinstance(ledger_entries, list): ledger_entries = [ledger_entries]
        
        if not purchase_ledger:
            max_neg = 0
            for entry in ledger_entries:
                if not isinstance(entry, dict): continue
                lname = str(entry.get('ledgername', '')).strip()
                amt_str = str(entry.get('amount', '0'))
                nums = re.findall(r'[-\d.]+', amt_str)
                amt = float(nums[-1]) if nums else 0.0
                lname_lower = lname.lower()
                if lname == vendor_name or 'cgst' in lname_lower or 'sgst' in lname_lower or 'igst' in lname_lower or 'rounding' in lname_lower: continue
                if amt < max_neg: max_neg = amt; purchase_ledger = lname

        line_items = []
        for item in inventory_entries:
            if not isinstance(item, dict): continue
            item_name = str(item.get('stockitemname', '')).strip()
            quantity = str(item.get('actualqty', item.get('billedqty', '0'))).strip()
            rate_str = str(item.get('rate', '0')).split('/')[0].strip()
            nums = re.findall(r'[-\d.]+', rate_str)
            rate = float(nums[-1]) if nums else 0.0
            discount = str(item.get('discount', '0')).strip()
            amt_str = str(item.get('amount', '0')).strip()
            nums = re.findall(r'[-\d.]+', amt_str)
            amount = float(nums[-1]) if nums else 0.0
            line_items.append({"item_name": item_name, "quantity": quantity, "rate": rate, "discount": discount, "amount": abs(amount)})

        taxes = []; rounding_off = 0.0
        for entry in ledger_entries:
            if not isinstance(entry, dict): continue
            lname = str(entry.get('ledgername', '')).strip()
            amt_str = str(entry.get('amount', '0'))
            nums = re.findall(r'[-\d.]+', amt_str)
            amt = float(nums[-1]) if nums else 0.0
            lname_lower = lname.lower()
            if ('cgst' in lname_lower or 'sgst' in lname_lower or 'igst' in lname_lower) and 'input' in lname_lower:
                tax_rate = ""
                if '%' in lname: tax_rate = lname.split('%')[0].split()[-1]
                tax_type = "CGST" if 'cgst' in lname_lower else ("SGST" if 'sgst' in lname_lower else "IGST")
                taxes.append({"tax_name": lname, "tax_type": tax_type, "rate": tax_rate, "amount": abs(amt)})
            elif 'rounding' in lname_lower: rounding_off = float(nums[-1]) if nums else 0.0
                
        po_data.append({"purchase_order_number": v_no, "date": v_date, "vendor_name": vendor_name, "reference_number": reference_number, "vendor_address": vendor_address, "payment_terms": payment_terms, "order_status": order_status, "purchase_ledger": purchase_ledger, "line_items": line_items, "taxes": taxes, "rounding_off": rounding_off, "narration": narration})
    return po_data

