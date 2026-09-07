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

# Cache for customer payment terms to avoid repeated queries
customer_payment_terms_cache = {}

def fetch_customer_payment_terms(customer_name):
    """Fetch payment terms from customer ledger master in Tally"""
    if not customer_name:
        return ""
    
    # Check cache first
    if customer_name in customer_payment_terms_cache:
        return customer_payment_terms_cache[customer_name]
    
    # XML request to fetch specific ledger details
    ledger_xml = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Ledgers</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    
    try:
        res = requests.post(TALLY_URL, data=ledger_xml, timeout=15)
        soup = BeautifulSoup(res.content, 'lxml-xml')
        
        # Find the specific customer ledger
        for ledger in soup.find_all('LEDGER'):
            name = ledger.get('NAME', '').strip()
            if name.lower() == customer_name.lower():
                # Check for CREDITPERIOD field
                credit_period = ledger.find('CREDITPERIOD')
                if credit_period and credit_period.text:
                    terms = credit_period.text.strip()
                    customer_payment_terms_cache[customer_name] = terms
                    return terms
                
                # Alternative: Check for BILLCREDITPERIOD in ledger
                bill_credit = ledger.find('BILLCREDITPERIOD')
                if bill_credit and bill_credit.text:
                    terms = bill_credit.text.strip()
                    customer_payment_terms_cache[customer_name] = terms
                    return terms
                
                break
    except:
        pass
    
    customer_payment_terms_cache[customer_name] = ""
    return ""

def get_payment_terms_hierarchical(voucher, party_name):
    """
    Extract payment terms using hierarchical method:
    1. Check BILLALLOCATIONS.LIST → BILLCREDITPERIOD
    2. Check BASICDUEDATEOFPYMT field
    3. Search for patterns like "30 days", "45 days" in sales order text
    4. Fetch from customer ledger master (CREDITPERIOD field)
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
    
    # Method 3: Search for payment term patterns in entire sales order text
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
    
    # Method 4: Fetch from customer ledger master
    customer_terms = fetch_customer_payment_terms(party_name)
    if customer_terms:
        return customer_terms
    
    return ""

def get_ledger_map_from_tally():
    """Builds a map that traces custom groups back to Sundry Debtors (Customers)."""
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

    debtor_groups = get_all_subgroups("Sundry Debtors")

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
            if parent in debtor_groups: l_map[name] = "(customers)"
            else: l_map[name] = "(others)"
    except: pass
    return l_map

def fetch_tally_sales_orders(sales_order_number="1"):
    """Fetch a specific sales order by voucher number from Tally"""
    ledger_map = get_ledger_map_from_tally()
    
    print(f"[TALLY] Fetching sales order with voucher number: {sales_order_number}...")
    
    # Use specific voucher type with narrow date range to avoid timeout
    # Fetch all Sales Orders in April 2025, then filter by number
    # Using just 1 week to reduce data volume
    xml_request = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
    <SVFROMDATE>20250401</SVFROMDATE><SVTODATE>20250407</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        print(f"[TALLY] Searching Sales Order vouchers in April 1-7, 2025...")
        response = requests.post(TALLY_URL, data=xml_request, timeout=30)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        # Find the specific voucher by number
        all_vouchers = soup.find_all('VOUCHER')
        print(f"[TALLY] Total Sales Order vouchers found: {len(all_vouchers)}")
        
        vouchers = []
        for v in all_vouchers:
            v_no = v.find('VOUCHERNUMBER')
            if v_no and v_no.text.strip() == sales_order_number:
                vouchers.append(v)
                print(f"[TALLY]  Found sales order #{sales_order_number}")
                break
        
        if not vouchers:
            print(f"[ERROR] Sales Order #{sales_order_number} not found in April 2025!")
            print(f"[ERROR] Please check the sales order number and try again.")
            return []

        sales_order_data = []
        for idx, v in enumerate(vouchers, 1):
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get customer from PARTYNAME field
            customer_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            
            # Get Reference Number (Customer PO Number)
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            
            # Get Customer Address
            customer_address = []
            buyer_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if buyer_addr_list:
                for addr in buyer_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        customer_address.append(addr.text.strip())
            
            # Get Payment Terms using hierarchical method
            payment_terms = get_payment_terms_hierarchical(v, customer_name)
            
            # Get Order Status
            order_status = v.find('ORDERSTATUS').text if v.find('ORDERSTATUS') else "Pending"
            
            # Get Sales Ledger using HIERARCHY METHOD
            sales_ledger = ""
            sales_ledger_from_item = ""
            
            # First, try to get sales ledger from inventory entries
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    sales_ledger_from_item = item_ledger.text.strip()
                    break
            
            # If not found in items, find the ledger with LARGEST NEGATIVE amount
            if not sales_ledger_from_item:
                max_negative_amount = 0
                for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                    name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amt = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0
                    
                    name_lower = name.lower()
                    if name == customer_name:
                        continue
                    if 'cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower:
                        continue
                    if 'rounding' in name_lower:
                        continue
                    
                    if amt < max_negative_amount:
                        max_negative_amount = amt
                        sales_ledger = name
            else:
                sales_ledger = sales_ledger_from_item
            
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
                
                # Note: Category and Cost Centre are fetched from Zoho Books item master
                # (not from Tally sales orders, as they don't include these at line item level)
                
                line_items.append({
                    "item_name": item_name,
                    "quantity": quantity,
                    "rate": rate,
                    "discount": discount,
                    "amount": abs(amount)
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
                if ('cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower) and 'output' in name_lower:
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
                        "rate": rate,
                        "amount": abs(amt)
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
            
            sales_order_data.append({
                "sales_order_number": v_no,
                "date": v_date,
                "customer_name": customer_name,
                "reference_number": reference_number,
                "customer_address": customer_address,
                "payment_terms": payment_terms,
                "order_status": order_status,
                "sales_ledger": sales_ledger,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": rounding_off,
                "narration": narration
            })
        
        return sales_order_data
    except Exception as e:
        print(f"Error fetching sales orders from Tally: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_access_token():
    """Get Zoho OAuth access token"""
    url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    
    try:
        response = requests.post(url, params=params)
        if response.status_code == 200:
            return response.json().get("access_token")
    except Exception as e:
        print(f"Error getting access token: {e}")
    return None

def get_zoho_contacts(token):
    """Fetch all CUSTOMER contacts from Zoho Books with pagination"""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    
    all_customers = {}
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
                
                # Filter to only customers
                for c in contacts:
                    if c.get("contact_type") == "customer":
                        all_customers[c["contact_name"].lower()] = c
                
                page_context = res.json().get("page_context", {})
                has_more_page = page_context.get("has_more_page", False)
                
                if not has_more_page:
                    break
                
                page += 1
            else:
                print(f"Error fetching contacts on page {page}: {res.status_code}")
                break
        
        return all_customers
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
    """Fetch payment terms from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
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
            return terms_map
    except Exception as e:
        print(f"Error fetching payment terms: {e}")
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
    """Fetch reporting tags from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/settings/tags", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            categories = res.json().get("reporting_tags", [])
            tag_map = {}
            
            for category in categories:
                tag_id = category.get("tag_id")
                tag_name = category.get("tag_name")
                
                # Get detailed options for this tag
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
            return tag_map
    except Exception as e:
        print(f"Error fetching tags: {e}")
    return {}

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
        if tax.get("tax_rate"):
            try:
                total_rate += float(tax["tax_rate"])
            except:
                pass
    return total_rate

def find_customer_in_zoho(customer_name, contact_map):
    """Find customer in Zoho Books using exact match or fuzzy matching"""
    customer_lower = customer_name.lower()
    
    # Try exact match first
    if customer_lower in contact_map:
        return contact_map[customer_lower], 100
    
    # Try fuzzy matching
    best_match = None
    best_score = 0
    
    for zoho_name, contact in contact_map.items():
        score = fuzz.ratio(customer_lower, zoho_name)
        if score > best_score:
            best_score = score
            best_match = contact
    
    return best_match, best_score

def create_zoho_sales_order(token, so_data, contact_map, account_map, payment_terms_map, tax_map, tag_map, item_map):
    """Create a sales order in Zoho Books"""
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
    print(f"[SALES ORDER] Processing Sales Order #{so_data['sales_order_number']} - Date: {tally_date}")
    print(f"{'='*100}")
    
    # Find customer in Zoho Books
    customer_info, match_score = find_customer_in_zoho(so_data["customer_name"], contact_map)
    
    if not customer_info:
        error_msg = f"Customer '{so_data['customer_name']}' not found in Zoho Books"
        print(f"  [ERROR] {error_msg}")
        print(f"  [ACTION REQUIRED] Please create this customer in Zoho Books manually and run again.")
        print(f"  [SKIPPING] Skipping this sales order...")
        return {"success": False, "error": error_msg}
    
    if match_score == 100:
        print(f"  [EXACT MATCH] Found customer: {customer_info['contact_name']}")
    else:
        if match_score < 80:
            error_msg = f"Low confidence match for customer '{so_data['customer_name']}' (Score: {match_score}%)"
            print(f"  [WARNING] {error_msg}")
            print(f"  [MATCH] Best match: '{customer_info['contact_name']}' (Score: {match_score}%)")
            print(f"  [ACTION] Please verify this is correct before proceeding")
            return {"success": False, "error": error_msg}
        else:
            print(f"  [FUZZY MATCH] Matched '{so_data['customer_name']}' → '{customer_info['contact_name']}' (Score: {match_score}%)")
    
    print(f"  [CUSTOMER] {customer_info['contact_name']} (ID: {customer_info['contact_id']})")
    
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
    
    # Get tax ID from Zoho based on total rate
    tax_info = tax_map.get(total_tax_rate)
    
    # If exact tax not found, use default 18% tax
    if not tax_info and total_tax_rate > 0:
        print(f"  [WARNING] No tax found for {total_tax_rate}% in Zoho Books")
        print(f"  [WARNING] Available taxes: {', '.join([str(k) for k in tax_map.keys() if isinstance(k, float)])}")
        
        # Check if this is an IGST transaction or GST transaction
        is_igst_transaction = False
        for tax in so_data["taxes"]:
            if tax.get("tax_type") == "IGST":
                is_igst_transaction = True
                break
        
        # Use appropriate default tax
        if is_igst_transaction:
            default_tax = tax_map.get("_igst_taxes", {}).get(18.0)
            if not default_tax:
                for key, val in tax_map.items():
                    if isinstance(key, str) and "igst" in key.lower() and "18" in key:
                        default_tax = val
                        break
            if default_tax:
                print(f"  [DEFAULT] Using IGST18 for interstate transaction instead of {total_tax_rate}%")
        else:
            default_tax = tax_map.get("_gst_taxes", {}).get(18.0) or tax_map.get(18.0) or tax_map.get("gst18")
            if not default_tax:
                for key, val in tax_map.items():
                    if isinstance(key, str) and "18" in key and "igst" not in key.lower():
                        default_tax = val
                        break
            if default_tax:
                print(f"  [DEFAULT] Using GST18 for intrastate transaction instead of {total_tax_rate}%")
        
        if default_tax:
            tax_info = default_tax
        else:
            print(f"  [ERROR] No default 18% tax found!")
    elif tax_info:
        print(f"  [TAX] Using Zoho tax: {tax_info['tax_name']} ({total_tax_rate}%)")
    
    for item in so_data["line_items"]:
        print(f"  [ITEM] {item['item_name']} - Qty: {item['quantity']} @ Rs.{item['rate']}")
        
        # Find sales account
        sales_account_id = None
        if so_data.get('sales_ledger'):
            sales_account = account_map.get(so_data['sales_ledger'].lower())
            if sales_account:
                sales_account_id = sales_account['account_id']
        
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
        
        # Add tax ID - REQUIRED for sales orders
        if tax_info:
            line_item["tax_id"] = tax_info["tax_id"]
        
        # Add account if found
        if sales_account_id:
            line_item["account_id"] = sales_account_id
            print(f"     [ACCOUNT] Using sales account: {so_data['sales_ledger']}")
        
        # Add reporting tags from Zoho Books item master
        # (Tally sales orders don't include category/cost centre at line item level)
        tags = []
        
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
            print(f"     {tax['tax_type']} {tax['tax_rate']}%: Rs.{tax['tax_amount']}")
        print(f"     Total Tax Rate: {total_tax_rate}%")
    
    # Map payment terms
    payment_terms_id = None
    if so_data.get("payment_terms"):
        tally_terms = so_data["payment_terms"].lower()
        
        # Try exact match
        if tally_terms in payment_terms_map:
            payment_terms_id = payment_terms_map[tally_terms]["payment_term_id"]
        else:
            # Try fuzzy matching
            for zoho_term_name, term_info in payment_terms_map.items():
                if fuzz.ratio(tally_terms, zoho_term_name) > 80:
                    payment_terms_id = term_info["payment_term_id"]
                    break
    
    print(f"\n  [DEBUG] Payment Terms Mapping:")
    print(f"    Tally: '{so_data.get('payment_terms', '')}'")
    print(f"    Mapped ID: {payment_terms_id}")
    print(f"    Available terms: {list(payment_terms_map.keys())}")
    
    if not payment_terms_id:
        if so_data.get("payment_terms"):
            print(f"  [WARNING] Payment term '{so_data.get('payment_terms')}' not found in Zoho Books")
    
    # Build payload
    payload = {
        "customer_id": customer_info["contact_id"],
        "salesorder_number": so_data["sales_order_number"],  # Insert Tally sales order number (e.g., "INFRA/PI-01/25-26")
        "reference_number": so_data.get("reference_number", ""),  # Customer PO number
        "date": zoho_date,
        "line_items": zoho_line_items,
        "notes": so_data["narration"][:1000] if so_data["narration"] else ""
    }
    
    # Add payment terms if available
    if payment_terms_id:
        tally_terms = so_data.get("payment_terms", "")
        numbers = re.findall(r'\d+', tally_terms)
        payload["payment_terms"] = int(numbers[0]) if numbers else 0
        print(f"  [PAYMENT TERMS APPLIED] Payment Terms ID: {payment_terms_id}")
    else:
        print(f"  [WARNING] Payment terms not mapped - will use default")
    
    # Add adjustment for rounding off
    if so_data.get("rounding_off"):
        payload["adjustment"] = so_data["rounding_off"]
        print(f"  [ROUNDING] Adjustment: Rs.{so_data['rounding_off']}")
    
    # Debug: Confirm sales order number is in payload
    print(f"\n  [DEBUG] Sales Order Number in Payload: '{payload.get('salesorder_number', 'NOT SET')}'")
    print(f"  [DEBUG] Reference Number in Payload: '{payload.get('reference_number', 'NOT SET')}'")
    
    # Create sales order
    print(f"\n  [CREATE] Creating sales order in Zoho Books...")
    print(f"  Payload: {json.dumps(payload, indent=2)}")
    
    try:
        res = requests.post(f"{creds['base_url']}/salesorders", headers=headers, params=params, json=payload)
        
        # Log response
        with open("salesorder_response.log", "w") as f:
            f.write(f"Status Code: {res.status_code}\n")
            f.write(f"Response: {json.dumps(res.json(), indent=2)}\n")
        
        if res.status_code == 201 and res.json().get("code") == 0:
            so_id = res.json()["salesorder"]["salesorder_id"]
            print(f"  [SUCCESS] Sales Order created successfully!")
            print(f"  [ID] Zoho Sales Order ID: {so_id}")
            return {"success": True, "salesorder_id": so_id}
        else:
            print(f"  [FAILED] Status: {res.status_code}")
            error_data = res.json()
            error_msg = error_data.get("message", "Unknown error")
            print(f"  Response: {json.dumps(error_data, indent=2)}")
            print(f"  [INFO] Full response saved to salesorder_response.log")
            return {"success": False, "error": f"{error_msg} (Code: {error_data.get('code', 'N/A')})"}
    except Exception as e:
        print(f"  [ERROR] Failed to create sales order: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

# ----------------------------------------------------------
# API WRAPPER FOR FRONTEND
# ----------------------------------------------------------

def get_all_sales_orders_data(from_date="20250401", to_date="20250430", limit=None):
    """
    Wrapper function for API to get sales order data
    Returns formatted data for frontend display
    """
    try:
        sales_orders = fetch_tally_sales_orders_range(from_date, to_date, limit)
        
        if not sales_orders:
            return None
        
        # Calculate stats
        total_orders = len(sales_orders)
        total_amount = sum(so.get("total_amount", 0) for so in sales_orders)
        
        return {
            "sales_orders": sales_orders,
            "stats": {
                "total_orders": total_orders,
                "total_amount": round(total_amount, 2),
                "from_date": from_date,
                "to_date": to_date
            }
        }
    except Exception as e:
        print(f" Error in get_all_sales_orders_data: {e}")
        import traceback
        traceback.print_exc()
        return None

def fetch_tally_sales_orders_range(from_date="20250401", to_date="20250430", limit=None):
    """
    Fetch Sales Orders from Tally with ALL fields
    
    Args:
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        limit: Maximum number of sales orders to fetch
    """
    ledger_map = get_ledger_map_from_tally()
    
    xml_request = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
    <SVFROMDATE>{from_date}</SVFROMDATE><SVTODATE>{to_date}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        print(f" Fetching sales orders from Tally ({from_date} to {to_date})...")
        response = requests.post(TALLY_URL, data=xml_request, timeout=90)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        vouchers = soup.find_all('VOUCHER')
        if limit:
            vouchers = vouchers[:limit]
        
        sales_order_data = []
        
        for v in vouchers:
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            customer_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get Reference Number (Customer PO Number)
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            
            # Get Customer Address
            customer_address = []
            buyer_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if buyer_addr_list:
                for addr in buyer_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        customer_address.append(addr.text.strip())
            
            # Get Payment Terms
            payment_terms = get_payment_terms_hierarchical(v, customer_name)
            
            # Get Order Status
            order_status = v.find('ORDERSTATUS').text if v.find('ORDERSTATUS') else "Pending"
            
            # Get Sales Ledger
            sales_ledger = ""
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    sales_ledger = item_ledger.text.strip()
                    break
            
            if not sales_ledger:
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
                    if name == customer_name or 'cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower or 'rounding' in name_lower:
                        continue
                    
                    if amt < max_negative_amount:
                        max_negative_amount = amt
                        sales_ledger = name
            
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
                
                line_items.append({
                    "item_name": item_name,
                    "quantity": quantity,
                    "rate": rate,
                    "discount": discount,
                    "amount": abs(amount)
                })
                
                subtotal += abs(amount)
            
            # Get tax details
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
                if ('cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower) and 'output' in name_lower:
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
            
            sales_order_data.append({
                "sales_order_number": v_no,
                "date": v_date,
                "customer_name": customer_name,
                "reference_number": reference_number,
                "customer_address": customer_address,
                "payment_terms": payment_terms,
                "order_status": order_status,
                "sales_ledger": sales_ledger,
                "narration": narration,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": rounding_off,
                "subtotal": round(subtotal, 2),
                "tax_total": round(tax_total, 2),
                "total_amount": round(total_amount, 2)
            })
        
        print(f" Fetched {len(sales_order_data)} sales order(s)")
        return sales_order_data
    
    except Exception as e:
        print(f" Error fetching Tally sales orders: {e}")
        import traceback
        traceback.print_exc()
        return []

def sync_sales_orders_to_zoho(selected_orders=None, from_date="20250401", to_date="20250430", limit=None):
    """
    Sync sales orders to Zoho Books.

    Priority order for data source:
      1. selected_orders     — passed directly from frontend (selected rows)
      2. DB (tally_data.db)  — read previously-fetched data, NO second Tally port call
      3. Tally port          — fallback only if DB is also empty for the range
    """
    try:
        print(" Starting Zoho Sync (Sales Orders)...")

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
            print(f" Using {len(orders_to_sync)} selected sales order(s) from frontend")

        else:
            orders_to_sync = []
            if database_manager:
                db_rows = database_manager.get_sales_orders_by_date_range(from_date, to_date)
                if db_rows:
                    for row in db_rows:
                        for field in ('customer_address', 'line_items', 'taxes'):
                            if row.get(field) and isinstance(row[field], str):
                                try:
                                    row[field] = json.loads(row[field])
                                except Exception:
                                    row[field] = []
                    if limit:
                        db_rows = db_rows[:limit]
                    orders_to_sync = db_rows
                    print(f" Loaded {len(orders_to_sync)} sales order(s) from DB (no Tally call needed)")

            if not orders_to_sync:
                print(" DB empty for range — fetching from Tally port as fallback...")
                orders_to_sync = fetch_tally_sales_orders_range(from_date, to_date, limit)

        if not orders_to_sync:
            return {"status": "error", "message": "No sales orders to sync"}

        print(f" Syncing {len(orders_to_sync)} sales order(s) to Zoho Books...")

        stats = {"created": 0, "failed": 0, "errors": []}

        for so in orders_to_sync:
            result = create_zoho_sales_order(token, so, contact_map, account_map,
                                             payment_terms_map, tax_map, tag_map, item_map)
            if result.get("success"):
                stats["created"] += 1
                print(f" Synced Sales Order #{so['sales_order_number']}")
            else:
                stats["failed"] += 1
                stats["errors"].append({
                    "sales_order_number": so['sales_order_number'],
                    "customer":          so['customer_name'],
                    "error":             result.get("error", "Unknown error")
                })
                print(f" Failed Sales Order #{so['sales_order_number']}: {result.get('error')}")

        return {"status": "success", "stats": stats}

    except Exception as e:
        print(f" Error in sync_sales_orders_to_zoho: {e}")
        return {"status": "error", "message": str(e)}


def get_all_sales_orders_data(from_date="20250401", to_date="20250430", limit=None):
    """
    Wrapper function for API to get sales order data.
    Fetches from Tally, saves ALL fields to SQLite DB, returns formatted data
    for frontend display.  Mirrors get_all_receipts_data() pattern exactly.
    NOT A SINGLE FIELD IS SKIPPED.
    """
    if database_manager:
        database_manager.init_db()

    try:
        orders = fetch_tally_sales_orders_range(from_date, to_date, limit)

        if not orders:
            return None

        # ----------------------------------------------------------------
        # SAVE EVERY FIELD TO SQLITE
        # ----------------------------------------------------------------
        if database_manager and orders:
            now = datetime.now().isoformat()
            db_data_list = []

            for so in orders:
                db_data_list.append({
                    # --- Voucher identity ---
                    "sales_order_number": so.get("sales_order_number", ""),
                    "date":               so.get("date", ""),
                    "customer_name":      so.get("customer_name", ""),

                    # --- Header fields ---
                    "reference_number":  so.get("reference_number", ""),
                    # customer_address is a list — JSON stringify it
                    "customer_address":  json.dumps(so.get("customer_address", [])),
                    "payment_terms":     so.get("payment_terms", ""),
                    "order_status":      so.get("order_status", ""),
                    "sales_ledger":      so.get("sales_ledger", ""),
                    "narration":         so.get("narration", ""),

                    # --- line_items: each element has
                    #     item_name, quantity, rate, discount, amount
                    "line_items": json.dumps(so.get("line_items", [])),

                    # --- taxes: each element has
                    #     tax_name, tax_type, tax_rate, tax_amount
                    "taxes": json.dumps(so.get("taxes", [])),

                    # --- Totals ---
                    "rounding_off": so.get("rounding_off", 0) or 0,
                    "subtotal":     so.get("subtotal",     0) or 0,
                    "tax_total":    so.get("tax_total",    0) or 0,
                    "total_amount": so.get("total_amount", 0) or 0,

                    # --- Fetch range & timestamps ---
                    "from_date":  from_date,
                    "to_date":    to_date,
                    "created_at": now,
                    "updated_at": now,
                })

            database_manager.bulk_save_sales_orders(db_data_list)
            print(f" Saved {len(orders)} sales orders to database")

        # ----------------------------------------------------------------
        # Build return stats
        # ----------------------------------------------------------------
        total_orders = len(orders)
        total_amount = sum(so.get("total_amount", 0) for so in orders)

        return {
            "sales_orders": orders,
            "stats": {
                "total_orders": total_orders,
                "total_amount": round(total_amount, 2),
                "from_date":    from_date,
                "to_date":      to_date
            }
        }

    except Exception as e:
        print(f" Error in get_all_sales_orders_data: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Main function to migrate sales order from Tally to Zoho Books"""
    print("="*100)
    print("TALLY TO ZOHO BOOKS SALES ORDER MIGRATION")
    print("="*100)
    
    # Get sales order number from user
    print("\n[INPUT] Enter the Sales Order Number from Tally")
    print("        (e.g., 'INFRA/PI-01/25-26' or '1')")
    sales_order_number = input("Sales Order Number: ").strip()
    
    if not sales_order_number:
        print("[ERROR] Sales order number cannot be empty!")
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
    print(f"[SUCCESS] Loaded {len(contact_map)} customers, {len(account_map)} accounts, {len(payment_terms_map)} payment terms, {len([k for k in tax_map.keys() if isinstance(k, float)])} taxes, {len(tag_map)} tags, {len(item_map)} items")
    
    # Fetch sales order from Tally
    print(f"\n[FETCH] Fetching sales order '{sales_order_number}' from Tally...")
    sales_orders = fetch_tally_sales_orders(sales_order_number=sales_order_number)
    
    if not sales_orders:
        print("[ERROR] No sales orders found in Tally")
        return
    
    print(f"[SUCCESS] Found {len(sales_orders)} sales order(s)")
    
    # Process sales order
    success_count = 0
    for so in sales_orders:
        if create_zoho_sales_order(token, so, contact_map, account_map, payment_terms_map, tax_map, tag_map, item_map):
            success_count += 1
    
    print(f"\n{'='*100}")
    print(f"[COMPLETE] MIGRATION COMPLETE: {success_count}/{len(sales_orders)} sales order(s) created successfully")
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
    
    so_data = []
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
        customer_name = str(v.get('partyname', '')).strip()
        narration = str(v.get('narration', '')).strip()
        reference_number = str(v.get('reference', '')).strip()
        
        customer_address = []
        buyer_addr = v.get('basicbuyeraddress.list', v.get('basicbuyeraddress', []))
        if not isinstance(buyer_addr, list): buyer_addr = [buyer_addr]
        for addr in buyer_addr:
            if isinstance(addr, dict) and 'basicbuyeraddress' in addr:
                customer_address.append(str(addr['basicbuyeraddress']).strip())
            elif isinstance(addr, str): customer_address.append(addr.strip())
            
        payment_terms = str(v.get('basicduedateofpymt', '')).strip()
        order_status = str(v.get('orderstatus', 'Pending')).strip()
        sales_ledger = ""
        
        inventory_entries = v.get('inventoryentries.list', v.get('inventoryentries', v.get('allinventoryentries.list', v.get('allinventoryentries', []))))
        if not isinstance(inventory_entries, list): inventory_entries = [inventory_entries]
        for item in inventory_entries:
            if isinstance(item, dict) and item.get('ledgername'):
                sales_ledger = str(item['ledgername']).strip()
                break

        ledger_entries = v.get('ledgerentries.list', v.get('ledgerentries', v.get('allledgerentries.list', v.get('allledgerentries', []))))
        if not isinstance(ledger_entries, list): ledger_entries = [ledger_entries]
        
        if not sales_ledger:
            max_neg = 0
            for entry in ledger_entries:
                if not isinstance(entry, dict): continue
                lname = str(entry.get('ledgername', '')).strip()
                amt_str = str(entry.get('amount', '0'))
                nums = re.findall(r'[-\d.]+', amt_str)
                amt = float(nums[-1]) if nums else 0.0
                lname_lower = lname.lower()
                if lname == customer_name or 'cgst' in lname_lower or 'sgst' in lname_lower or 'igst' in lname_lower or 'rounding' in lname_lower: continue
                if amt < max_neg: max_neg = amt; sales_ledger = lname

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
            if ('cgst' in lname_lower or 'sgst' in lname_lower or 'igst' in lname_lower) and 'output' in lname_lower:
                tax_rate = ""
                if '%' in lname: tax_rate = lname.split('%')[0].split()[-1]
                tax_type = "CGST" if 'cgst' in lname_lower else ("SGST" if 'sgst' in lname_lower else "IGST")
                taxes.append({"tax_name": lname, "tax_type": tax_type, "rate": tax_rate, "amount": abs(amt)})
            elif 'rounding' in lname_lower: rounding_off = float(nums[-1]) if nums else 0.0
                
        so_data.append({"sales_order_number": v_no, "date": v_date, "customer_name": customer_name, "reference_number": reference_number, "customer_address": customer_address, "payment_terms": payment_terms, "order_status": order_status, "sales_ledger": sales_ledger, "line_items": line_items, "taxes": taxes, "rounding_off": rounding_off, "narration": narration})
    return so_data

