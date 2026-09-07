import os
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
from dotenv import load_dotenv
import json
import re
from fuzzywuzzy import fuzz

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
    
    # Cache empty result to avoid repeated queries
    customer_payment_terms_cache[customer_name] = ""
    return ""

def get_payment_terms_hierarchical(voucher, party_name):
    """
    Extract payment terms using hierarchical method:
    1. Check BILLALLOCATIONS.LIST → BILLCREDITPERIOD
    2. Check BASICDUEDATEOFPYMT field
    3. Search for patterns like "30 days", "45 days" in invoice text
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
    
    # Method 3: Search for payment term patterns in entire invoice text
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
    
    # Method 4: Fetch from customer ledger master
    customer_terms = fetch_customer_payment_terms(party_name)
    if customer_terms:
        return customer_terms
    
    return ""

def get_access_token():
    """Get Zoho Books access token"""
    payload = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    res = requests.post("https://accounts.zoho.com/oauth/v2/token", data=payload)
    return res.json().get("access_token")

def get_ledger_map_from_tally():
    """Builds a map that traces custom groups back to Sundry Debtors (Customers)."""
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

    # Get all customer groups (Sundry Debtors)
    debtor_groups = get_all_subgroups("Sundry Debtors")

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
            if parent in debtor_groups: l_map[name] = "(customers)"
            else: l_map[name] = "(others)"
    except: pass
    return l_map

def fetch_tally_invoices(limit=1):  # Changed to 1 for testing
    """Fetch Tax Invoice vouchers from Tally (April 2025 only) - FIRST INVOICE ONLY FOR TESTING"""
    ledger_map = get_ledger_map_from_tally()
    
    xml_request = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Tax Invoice</VOUCHERTYPENAME>
    <SVFROMDATE>20250401</SVFROMDATE><SVTODATE>20250430</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
        response = requests.post(TALLY_URL, data=xml_request, timeout=30)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        # Take only the first invoice for testing
        vouchers = soup.find_all('VOUCHER')[:limit]

        invoice_data = []
        for idx, v in enumerate(vouchers, 1):
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get customer from PARTYNAME field
            customer_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            
            # Get Purchase Order Number
            po_number = v.find('BASICPURCHASEORDERNO').text if v.find('BASICPURCHASEORDERNO') else ""
            
            # Get Buyer Address
            buyer_address = []
            buyer_addr_list = v.find('BASICBUYERADDRESS.LIST')
            if buyer_addr_list:
                for addr in buyer_addr_list.find_all('BASICBUYERADDRESS'):
                    if addr.text:
                        buyer_address.append(addr.text.strip())
            
            # Get Hidden Fields (IRN, Ack No, Ack Date)
            irn = v.find('IRN').text if v.find('IRN') else ""
            irn_ack_no = v.find('IRNACKNO').text if v.find('IRNACKNO') else ""
            irn_ack_date = v.find('IRNACKDATE').text if v.find('IRNACKDATE') else ""
            
            # Get Payment Terms using hierarchical method
            payment_terms = get_payment_terms_hierarchical(v, customer_name)
            
            # Get Sales Ledger using HIERARCHY METHOD
            sales_ledger = ""
            sales_ledger_from_item = ""
            
            # First, try to get sales ledger from inventory entries
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                # Check if there's a ledger associated with this item
                item_ledger = item.find('LEDGERNAME')
                if item_ledger and item_ledger.text:
                    sales_ledger_from_item = item_ledger.text.strip()
                    break
                     # Method 2: If not found in items, find the ledger with LARGEST NEGATIVE amount
            # (excluding customer, taxes, tds, and rounding)
            if not sales_ledger_from_item:
                max_negative_amount = 0
                for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                    name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amt = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0
                    
                    # Skip customer ledger, tax ledgers, TDS, and rounding off
                    name_lower = name.lower()
                    if name == customer_name:  # Skip customer
                        continue
                    if 'cgst' in name_lower or 'sgst' in name_lower or 'igst' in name_lower or 'cst' in name_lower or 'vat' in name_lower or 'tds' in name_lower or 't.d.s' in name_lower:  # Skip taxes/TDS
                        continue
                    if 'rounding' in name_lower:  # Skip rounding
                        continue
                    
                    # Find the ledger with largest negative amount
                    if amt < max_negative_amount:
                        max_negative_amount = amt
                        sales_ledger = name
            else:
                sales_ledger = sales_ledger_from_item
            
            # Get line items
            line_items = []
            subtotal = 0
            for item in v.find_all('INVENTORYENTRIES.LIST') or v.find_all('ALLINVENTORYENTRIES.LIST'):
                item_name = item.find('STOCKITEMNAME').text.strip() if item.find('STOCKITEMNAME') else ""
                
                # Get quantity
                qty_tag = item.find('ACTUALQTY') or item.find('BILLEDQTY')
                quantity = qty_tag.text.strip() if qty_tag else "0"
                
                # Get rate - handle currency conversion strings
                rate_tag = item.find('RATE')
                if rate_tag and rate_tag.text:
                    rate_text = rate_tag.text.split('/')[0].strip()
                    numbers = re.findall(r'[-\d.]+', rate_text)
                    rate = float(numbers[-1]) if numbers else 0.0
                else:
                    rate = 0.0
                
                # Get discount
                discount_tag = item.find('DISCOUNT')
                discount = discount_tag.text.strip() if discount_tag else "0"
                
                # Get amount - handle currency conversion strings
                amount_tag = item.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    amount = float(numbers[-1]) if numbers else 0.0
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
                subtotal += abs(amount)
            
            # Get tax details and additional charges (Freight, Transport, etc.) from LEDGERENTRIES.LIST
            taxes = []
            tax_total = 0.0
            rounding_off = 0.0
            freight_charges = 0.0
            tds_amount = 0.0
            tds_ledger = ""
            tds_rate = 0.0

            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                if not name: continue
                name_lower = name.lower()

                # Get amount - handle currency conversion strings
                amount_tag = entry.find('AMOUNT')
                if amount_tag and amount_tag.text:
                    amount_text = amount_tag.text.strip()
                    numbers = re.findall(r'[-\d.]+', amount_text)
                    amt = float(numbers[-1]) if numbers else 0.0
                else:
                    amt = 0.0
                
                # Check for TDS ledgers
                if ('tds' in name_lower or 't.d.s' in name_lower or 'tax deducted' in name_lower) and name_lower != customer_name.lower() and name_lower != sales_ledger.lower():
                    tds_ledger = name
                    tds_amount = abs(amt)
                    if subtotal > 0:
                        tds_rate = round((tds_amount / subtotal) * 100, 2)
                # Check for ANY tax ledger (CGST, SGST, IGST, etc.)
                elif any(t_kw in name_lower for t_kw in ['gst', 'cgst', 'sgst', 'igst', 'cst', 'vat', 'tax']) and name_lower != customer_name.lower() and name_lower != sales_ledger.lower():
                    rate = ""
                    if '%' in name:
                        rate = name.split('%')[0].split()[-1]
                    
                    tax_type = "CGST" if 'cgst' in name_lower else ("SGST" if 'sgst' in name_lower else ("IGST" if 'igst' in name_lower else ("CST" if 'cst' in name_lower else ("VAT" if 'vat' in name_lower else "GST"))))
                    taxes.append({
                        "tax_name": name,
                        "tax_type": tax_type,
                        "tax_rate": rate,
                        "tax_amount": abs(amt)
                    })
                    tax_total += abs(amt)
                elif 'rounding' in name_lower:
                    rounding_off = amt
                elif name_lower != customer_name.lower() and name_lower != sales_ledger.lower() and abs(amt) > 0:
                    # Additional Ledger Charge (e.g. Freight Charges, Transport, Packing)
                    charge_amt = abs(amt)
                    freight_charges += charge_amt
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
            
            total_amount = subtotal + tax_total + rounding_off - tds_amount

            invoice_data.append({
                "date": v_date,
                "invoice_number": v_no,
                "customer_name": customer_name,
                "po_number": po_number,
                "buyer_address": buyer_address,
                "payment_terms": payment_terms,
                "irn": irn,
                "irn_ack_no": irn_ack_no,
                "irn_ack_date": irn_ack_date,
                "sales_ledger": sales_ledger,
                "line_items": line_items,
                "taxes": taxes,
                "rounding_off": rounding_off,
                "tds_amount": round(tds_amount, 2),
                "tds_ledger": tds_ledger,
                "tds_rate": round(tds_rate, 2),
                "subtotal": round(subtotal, 2),
                "tax_total": round(tax_total, 2),
                "total_amount": round(total_amount, 2),
                "narration": narration if narration else ""
            })
        return invoice_data
    except Exception as e:
        print(f"Error fetching invoices from Tally: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_zoho_contacts(token):
    """Fetch all CUSTOMER contacts from Zoho Books"""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    creds = _get_creds()
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/contacts", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            all_contacts = res.json().get("contacts", [])
            # Filter to only customers
            customers = {c["contact_name"].lower(): c for c in all_contacts if c.get("contact_type") == "customer"}
            return customers
    except Exception as e:
        print(f"Error fetching contacts: {e}")
    return {}

def find_or_create_contact(token, contact_map, contact_name):
    """Find existing contact using FUZZY MATCHING - NO AUTO-CREATE"""
    contact_key = contact_name.lower().strip()
    
    # Exact match first
    if contact_key in contact_map:
        print(f"  [EXACT MATCH] Found customer: {contact_map[contact_key]['contact_name']}")
        return contact_map[contact_key]
    
    # Fuzzy matching to find similar names
    best_match = None
    best_score = 0
    best_name = ""
    
    for existing_name, contact_data in contact_map.items():
        # Calculate similarity score
        score = fuzz.ratio(contact_key, existing_name)
        
        if score > best_score:
            best_score = score
            best_match = contact_data
            best_name = existing_name
    
    # If similarity is >= 75%, use the match
    if best_match and best_score >= 75:
        print(f"  [FUZZY MATCH] Found similar customer: {best_match['contact_name']} (Score: {best_score}%)")
        # Add to map with the new key for faster future lookups
        contact_map[contact_key] = best_match
        return best_match
    
    # NO AUTO-CREATE - Require manual intervention
    print(f"\n  [ERROR] Customer '{contact_name}' not found in Zoho Books!")
    if best_match:
        print(f"  [SUGGESTION] Closest match: '{best_match['contact_name']}' (Score: {best_score}%)")
    print(f"  [ACTION REQUIRED] Please create this customer in Zoho Books manually and run again.")
    print(f"  [SKIPPING] Skipping this invoice...\n")
    return None

def get_zoho_accounts(token):
    """Fetch all accounts from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/chartofaccounts", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            return {a["account_name"].lower(): a["account_id"] for a in res.json().get("chartofaccounts", [])}
    except Exception as e:
        print(f"Error fetching accounts: {e}")
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
                term_days = term.get("payment_terms")
                if term_label and term_id:
                     # Map by label (e.g., "Net 30")
                    terms_map[term_label.lower()] = term_id
            return terms_map
    except Exception as e:
        print(f"  [WARNING] Error fetching payment terms: {e}")
    return {}

def map_payment_terms(tally_terms, zoho_terms_map):
    """Map Tally payment terms to Zoho Books payment terms ID"""
    if not tally_terms or not zoho_terms_map:
        return None
    
    tally_terms_lower = tally_terms.lower().strip()
    
    # Try exact match first
    if tally_terms_lower in zoho_terms_map:
        return zoho_terms_map[tally_terms_lower]
    
    # Tally sends "30 Days" - try to extract the number and match
    import re
    numbers = re.findall(r'\d+', tally_terms)
    if numbers:
        days = numbers[0]
        # Try variations
        variations = [
            f"net {days}",      # "net 30"
            f"{days} days",     # "30 days"
            f"net{days}",       # "net30"
        ]
        
        for variation in variations:
            if variation in zoho_terms_map:
                return zoho_terms_map[variation]
    
    # If no match found, use "Due on Receipt" as default
    if "due on receipt" in zoho_terms_map:
        return zoho_terms_map["due on receipt"]
    
    return None

def get_zoho_taxes(token):
    """Fetch all taxes AND tax groups from Zoho Books"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    

    tax_map = {}
    gst_taxes = {}  # Separate map for GST (CGST+SGST) taxes
    igst_taxes = {}  # Separate map for IGST taxes
    
    # Fetch individual taxes
    try:
        res = requests.get(f"{creds['base_url']}/settings/taxes", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            taxes = res.json().get("taxes", [])
            for tax in taxes:
                tax_name = tax.get("tax_name", "")
                tax_percentage = tax.get("tax_percentage", 0)
                tax_id = tax.get("tax_id")
                if tax_id:
                    tax_info = {
                        "tax_id": tax_id,
                        "tax_name": tax_name
                    }
                    
                    # Separate GST and IGST
                    if "IGST" in tax_name:
                        igst_taxes[float(tax_percentage)] = tax_info
                    elif "GST" in tax_name:
                        gst_taxes[float(tax_percentage)] = tax_info
                        # Prioritize GST over IGST for percentage mapping
                        tax_map[float(tax_percentage)] = tax_info
                    
                    # Also map by name pattern
                    if "GST" in tax_name or "IGST" in tax_name:
                        tax_map[tax_name.lower()] = tax_info
    except Exception as e:
        print(f"  [WARNING] Error fetching taxes: {e}")
    
    # Fetch tax groups (compound taxes like GST12 [12%])
    try:
        res = requests.get(f"{creds['base_url']}/settings/taxgroups", headers=headers, params=params)
        if res.status_code == 200 and res.json().get("code") == 0:
            tax_groups = res.json().get("tax_groups", [])
            for group in tax_groups:
                group_name = group.get("tax_group_name", "")
                group_percentage = group.get("tax_group_percentage", 0)
                group_id = group.get("tax_group_id")
                if group_id:
                    # Map by percentage (prioritize tax groups over individual taxes)
                    tax_map[float(group_percentage)] = {
                        "tax_id": group_id,
                        "tax_name": group_name
                    }
                    # Also map by name pattern (e.g., "GST12" -> 12%)
                    if "GST" in group_name or "IGST" in group_name:
                        # Extract number from name (e.g., "GST12" -> "gst12")
                        tax_map[group_name.lower().replace(" ", "").replace("[", "").replace("]", "").replace("%", "")] = {
                            "tax_id": group_id,
                            "tax_name": group_name
                        }
    except Exception as e:
        print(f"  [WARNING] Error fetching tax groups: {e}")
    
    # Store GST and IGST maps for later use
    tax_map["_gst_taxes"] = gst_taxes
    tax_map["_igst_taxes"] = igst_taxes
    
    return tax_map

def calculate_total_tax_rate(taxes):
    """Calculate total tax rate from CGST + SGST or IGST"""
    total_rate = 0
    for tax in taxes:
        if tax.get("tax_rate"):
            try:
                total_rate += float(tax["tax_rate"])
            except:
                pass
    return total_rate

def create_zoho_invoice(token, invoice_data, contact_map, account_map, payment_terms_map, tax_map, tag_map):
    """Create invoice in Zoho Books with FULL AUTOMATION"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {
        "organization_id": creds["org_id"],
        "ignore_auto_number_generation": "true"  # Use Tally invoice number
    }
    
    print(f"\n{'='*100}")
    print(f"[INVOICE] Processing Invoice #{invoice_data['invoice_number']} - Date: {invoice_data['date']}")
    print(f"{'='*100}")
    
    # Find or create customer
    customer_info = find_or_create_contact(token, contact_map, invoice_data["customer_name"])
    if not customer_info:
        print(f"  [ERROR] Failed to find or create customer: {invoice_data['customer_name']}")
        return False
    
    print(f"  [CUSTOMER] {customer_info['contact_name']} (ID: {customer_info['contact_id']})")
    
    # Display additional info
    if invoice_data.get('po_number'):
        print(f"  [PO] {invoice_data['po_number']}")
    if invoice_data.get('payment_terms'):
        print(f"  [TERMS] {invoice_data['payment_terms']}")
    if invoice_data.get('irn'):
        print(f"  [IRN] {invoice_data['irn'][:50]}...")
    
    # Build line items
    zoho_line_items = []
    
    # Calculate total tax rate (CGST + SGST = GST or IGST)
    total_tax_rate = calculate_total_tax_rate(invoice_data["taxes"])
    
    # Get tax ID from Zoho based on total rate
    tax_info = tax_map.get(total_tax_rate)
    
    # If exact tax not found, use default 18% GST (not IGST - for intrastate)
    if not tax_info and total_tax_rate > 0:
        print(f"  [WARNING] No tax found for {total_tax_rate}% in Zoho Books")
        print(f"  [WARNING] Available taxes: {', '.join([str(k) for k in tax_map.keys() if isinstance(k, float)])}")
        
        # Try to use GST18 (not IGST18) as default for intrastate transactions
        default_tax = tax_map.get(18.0) or tax_map.get("gst18")
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
    elif tax_info:
        print(f"  [TAX] Using Zoho tax: {tax_info['tax_name']} ({total_tax_rate}%)")
    
    for item in invoice_data["line_items"]:
        print(f"  [ITEM] {item['item_name']} - Qty: {item['quantity']} @ Rs.{item['rate']}")
        if item.get('category') or item.get('cost_centre'):
            print(f"     [TAG] Category: {item.get('category', 'N/A')}, Cost Centre: {item.get('cost_centre', 'N/A')}")
        
        # Get sales account ID
        sales_account_id = account_map.get(invoice_data["sales_ledger"].lower()) if invoice_data.get("sales_ledger") else None
        
        # Parse quantity to get numeric value
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
        
        # Add tax ID if available
        if tax_info:
            line_item["tax_id"] = tax_info["tax_id"]
        
        # Add account if found
        if sales_account_id:
            line_item["account_id"] = sales_account_id
            print(f"     [ACCOUNT] Using sales account: {invoice_data['sales_ledger']}")
        
        # Add reporting tags (Category and Cost Centre)
        tags = []
        if item.get('category'):
            category_tag = tag_map.get(item['category'].lower())
            if category_tag:
                tags.append({
                    "tag_id": category_tag["tag_id"],
                    "tag_option_id": category_tag["tag_option_id"]
                })
                print(f"     [TAG] Category: {item['category']}")
        
        if item.get('cost_centre'):
            cc_tag = tag_map.get(item['cost_centre'].lower())
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
    if invoice_data["taxes"]:
        print(f"\n  [TAX] Taxes:")
        for tax in invoice_data["taxes"]:
            print(f"     {tax['tax_type']} {tax['tax_rate']}%: Rs.{tax['tax_amount']}")
        print(f"     Total Tax Rate: {total_tax_rate}%")
    
    # Display rounding off
    if invoice_data.get("rounding_off"):
        print(f"  [ROUNDING] Rs.{abs(invoice_data['rounding_off'])}")
    
    # Convert date format (YYYYMMDD -> YYYY-MM-DD)
    tally_date = invoice_data["date"]
    zoho_date = f"{tally_date[:4]}-{tally_date[4:6]}-{tally_date[6:8]}"
    
    # Map payment terms
    payment_terms_id = map_payment_terms(invoice_data.get("payment_terms", ""), payment_terms_map)
    print(f"\n  [DEBUG] Payment Terms Mapping:")
    print(f"    Tally: '{invoice_data.get('payment_terms', '')}'")
    print(f"    Mapped ID: {payment_terms_id}")
    print(f"    Available terms: {list(payment_terms_map.keys())}")
    
    if payment_terms_id:
        print(f"  [PAYMENT TERMS] Mapped '{invoice_data.get('payment_terms')}' to ID: {payment_terms_id}")
    else:
        if invoice_data.get("payment_terms"):
            print(f"  [WARNING] Payment term '{invoice_data.get('payment_terms')}' not found in Zoho Books")
    
    # Build billing address from buyer address (keep it simple - Zoho has 100 char TOTAL limit)
    # TEMPORARILY DISABLED - Testing without billing address
    billing_address = {}
    # if invoice_data.get("buyer_address"):
    #     addr_lines = invoice_data["buyer_address"]
    #     # Combine all address lines into one short address field
    #     full_address = ", ".join([line.strip() for line in addr_lines if line.strip()])
    #     # Truncate to 90 chars to be safe
    #     billing_address["address"] = full_address[:90]
    #     billing_address["country"] = "India"
    
    # Build payload
    payload = {
        "customer_id": customer_info["contact_id"],
        "invoice_number": invoice_data["invoice_number"],
        "reference_number": invoice_data.get("po_number", ""),
        "date": zoho_date,
        "line_items": zoho_line_items,
        "notes": invoice_data["narration"][:1000] if invoice_data["narration"] else ""
    }
    
    # Add billing address if available
    if billing_address:
        payload["billing_address"] = billing_address
        print(f"  [ADDRESS] Billing address: {billing_address.get('attention', '')}, {billing_address.get('city', '')}")
    
    # Add payment terms if available
    if payment_terms_id:
        # Zoho Books expects days number (30), not ID - extract from Tally terms
        tally_terms = invoice_data.get("payment_terms", "")
        numbers = re.findall(r'\d+', tally_terms)
        payload["payment_terms"] = int(numbers[0]) if numbers else 0
        print(f"  [PAYMENT TERMS APPLIED] Payment Terms ID: {payment_terms_id}")
    else:
        print(f"  [WARNING] Payment terms not mapped - will use default")
    
    # Add adjustment for rounding off
    if invoice_data.get("rounding_off"):
        payload["adjustment"] = invoice_data["rounding_off"]
        payload["adjustment_description"] = "Rounding Off"
    
    print(f"\n  [CREATE] Creating invoice in Zoho Books...")
    print(f"  Payload: {json.dumps(payload, indent=2)}")
    
    try:
        res = requests.post(f"{creds['base_url']}/invoices", headers=headers, params=params, json=payload)
        
        # Log full response for debugging
        with open("invoice_response.log", "w") as f:
            f.write(f"Status Code: {res.status_code}\n")
            f.write(f"Response: {json.dumps(res.json(), indent=2)}\n")
        
        if res.status_code in [200, 201] and res.json().get("code") == 0:
            invoice_id = res.json().get("invoice", {}).get("invoice_id", "N/A")
            print(f"  [SUCCESS] Invoice created with ID: {invoice_id}")
            
            if invoice_id and invoice_id != "N/A":
                try:
                    requests.post(f"{creds['base_url']}/invoices/{invoice_id}/status/sent", headers=headers, params=params)
                    print(f"  [STATUS] Marked Invoice #{invoice_data['invoice_number']} as SENT / OVERDUE")
                except Exception as st_err:
                    print(f"  [STATUS ERROR] {st_err}")
            return True
        else:
            print(f"  [FAILED] Status: {res.status_code}")
            print(f"  Response: {json.dumps(res.json(), indent=2)}")
            print(f"  [INFO] Full response saved to invoice_response.log")
            return False
    except Exception as e:
        print(f"  [ERROR] Error creating invoice: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main function to migrate first invoice from Tally to Zoho Books"""
    print("="*100)
    print("TALLY TO ZOHO BOOKS INVOICE MIGRATION - FIRST INVOICE TEST")
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
    print(f"[SUCCESS] Loaded {len(contact_map)} contacts, {len(account_map)} accounts, {len(payment_terms_map)} payment terms, {len([k for k in tax_map.keys() if isinstance(k, float)])} taxes, {len(tag_map)} tags")
    
    # Fetch invoices from Tally
    print("\n[FETCH] Fetching first invoice from Tally...")
    invoices = fetch_tally_invoices(limit=1)
    
    if not invoices:
        print("[ERROR] No invoices found in Tally")
        return
    
    print(f"[SUCCESS] Found {len(invoices)} invoice(s)")
    
    # Process first invoice
    success_count = 0
    for invoice in invoices:
        if create_zoho_invoice(token, invoice, contact_map, account_map, payment_terms_map, tax_map, tag_map):
            success_count += 1
    
    print(f"\n{'='*100}")
    print(f"[COMPLETE] MIGRATION COMPLETE: {success_count}/{len(invoices)} invoice(s) created successfully")
    print(f"{'='*100}")

if __name__ == "__main__":
    main()
