import requests
import os
from datetime import datetime
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import json
import re
import sys
from pathlib import Path

# Add parent directory to path to access shared cache
parent_dir = Path(__file__).parent.parent
sys.path.append(str(parent_dir))

# Import database manager
try:
    import database_manager
except ImportError:
    print("️ Warning: Could not import database_manager. SQLite sync will be skipped.")
    database_manager = None

from journel.journel_backend import _get_creds

TALLY_URL = "http://localhost:9000"

# ----------------------------------------------------------
# JOB HELPERS (SSE-friendly logging + stop)
# ----------------------------------------------------------

def _make_emitter(log=None):
    def _emit(msg: str):
        try:
            if callable(log):
                log(msg)
            else:
                print(msg)
        except Exception:
            pass
    return _emit

def _should_stop(stop_event) -> bool:
    try:
        return bool(stop_event and getattr(stop_event, "is_set", None) and stop_event.is_set())
    except Exception:
        return False

def _iter_days(from_yyyymmdd: str, to_yyyymmdd: str):
    start = datetime.strptime(from_yyyymmdd, "%Y%m%d")
    end = datetime.strptime(to_yyyymmdd, "%Y%m%d")
    cur = start
    from datetime import timedelta
    while cur <= end:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=1)

def fetch_tally_receipts_day_by_day(from_date="20250401", to_date="20250430", limit=None, company_name=None, *, log=None, stop_event=None):
    """
    Day-by-day fetch to avoid large payload/timeouts.
    Ensures each request uses the current loop date.
    """
    _emit = _make_emitter(log)
    receipts = []
    total_days = 0

    _emit(f"Fetching receipts: {from_date} -> {to_date} (day by day)...")

    for day in _iter_days(from_date, to_date):
        if _should_stop(stop_event):
            _emit("Stopped by user.")
            break

        total_days += 1
        day_receipts = []  # IMPORTANT: clear per-day results

        xml_request = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
        <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
        <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
        <SVFROMDATE>{day}</SVFROMDATE><SVTODATE>{day}</SVTODATE>
        </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

        try:
            response = requests.post(TALLY_URL, data=xml_request, timeout=45)
            soup = BeautifulSoup(response.content, 'lxml-xml')
            vouchers = soup.find_all('VOUCHER')
            if limit:
                vouchers = vouchers[:limit]

            for v in vouchers:
                receipt_date = v.find('DATE').text.strip() if v.find('DATE') else day
                receipt_number = v.find('VOUCHERNUMBER').text.strip() if v.find('VOUCHERNUMBER') else ""
                voucher_type = v.find('VOUCHERTYPENAME').text.strip() if v.find('VOUCHERTYPENAME') else "Receipt"
                tally_guid = v.find('GUID').text.strip() if v.find('GUID') else ""

                customer_name = v.find('PARTYNAME').text.strip() if v.find('PARTYNAME') else ""
                customer_ledger_amount = 0.0

                ledger_entries = []
                payment_mode = ""
                bank_account = ""
                account_current_balance = 0.0
                rounding_amount = 0.0
                rounding_ledger = ""
                against_reference = ""

                raw_entries = v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST')
                for entry in raw_entries:
                    ledger_name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                    amount = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0.0

                    current_balance = 0.0
                    cb_tag = entry.find('CURRENTBALANCE')
                    if cb_tag:
                        cb_text = cb_tag.text.strip()
                        m = re.search(r'([\\d,]+\\.?\\d*)', cb_text)
                        if m:
                            current_balance = float(m.group(1).replace(',', ''))
                            if 'Dr' in cb_text:
                                current_balance = -current_balance

                    ledger_entries.append({
                        "ledger_name": ledger_name,
                        "amount": amount,
                        "current_balance": current_balance
                    })

                    lname = ledger_name.lower()
                    if 'rounding' in lname:
                        rounding_amount = amount
                        rounding_ledger = ledger_name
                    elif any(k in lname for k in ['bank', 'cash', 'sbi', 'hdfc', 'icici', 'axis', 'kotak', 'idfc']):
                        bank_account = ledger_name
                        payment_mode = "Cash" if 'cash' in lname else "Bank Transfer"
                        account_current_balance = current_balance
                    elif amount > 0 and not any(k in lname for k in ['cash', 'bank', 'cgst', 'sgst', 'igst', 'rounding']):
                        if not customer_name:
                            customer_name = ledger_name
                        customer_ledger_amount = abs(amount)

                total_amount = customer_ledger_amount
                reference_number = v.find('REFERENCE').text.strip() if v.find('REFERENCE') else ""
                if not reference_number:
                    reference_number = v.find('CHEQUENUMBER').text.strip() if v.find('CHEQUENUMBER') else ""
                narration = v.find('NARRATION').text.strip() if v.find('NARRATION') else ""

                invoice_allocations = []
                bill_allocs_found = v.find_all('BILLALLOCATIONS.LIST')
                for bill_alloc in bill_allocs_found:
                    inv_name = bill_alloc.find('NAME').text.strip() if bill_alloc.find('NAME') else ""
                    raw_amt = float(bill_alloc.find('AMOUNT').text or 0) if bill_alloc.find('AMOUNT') else 0.0
                    bill_type = bill_alloc.find('BILLTYPE').text.strip() if bill_alloc.find('BILLTYPE') else "Agst Ref"

                    if not inv_name and bill_type == "On Account":
                        inv_name = "On Account"

                    if not inv_name:
                        continue

                    if not against_reference:
                        against_reference = inv_name

                    dr_cr = "Cr" if raw_amt < 0 else "Dr"
                    final_amount = abs(raw_amt) if raw_amt != 0 else customer_ledger_amount
                    invoice_allocations.append({
                        "invoice_number": inv_name,
                        "bill_type": bill_type,
                        "amount": float(final_amount or 0),
                        "dr_cr": dr_cr,
                    })

                day_receipts.append({
                    "date": receipt_date,
                    "receipt_number": receipt_number,
                    "voucher_type": voucher_type,
                    "customer_name": customer_name,
                    "customer_ledger_amount": customer_ledger_amount,
                    "payment_mode": payment_mode,
                    "bank_account": bank_account,
                    "account_current_balance": account_current_balance,
                    "amount": total_amount,
                    "reference_number": reference_number,
                    "against_reference": against_reference,
                    "narration": narration,
                    "invoice_allocations": invoice_allocations,
                    "ledger_entries": ledger_entries,
                    "cost_center_allocations": [],
                    "rounding_amount": rounding_amount,
                    "rounding_ledger": rounding_ledger,
                    "tally_guid": tally_guid,
                })

            receipts.extend(day_receipts)
            day_iso = datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d")
            _emit(f"[{day_iso}] Fetched {len(day_receipts)} records")

        except requests.exceptions.ConnectionError as e:
            raise Exception(f"ConnectionError: Failed to connect to Tally on {TALLY_URL}. Is Tally running and configured for XML export? Error: {str(e)}")
        except Exception as e:
            day_iso = datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d")
            _emit(f"[{day_iso}] Error: {e}")

    _emit(f"Done. Total fetched: {len(receipts)}")
    return receipts

# ----------------------------------------------------------
# TALLY RECEIPT FETCHING
# ----------------------------------------------------------

def fetch_tally_receipts(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    """
    Fetch Receipt vouchers from Tally with ALL fields
    
    Args:
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        limit: Maximum number of receipts to fetch
        company_name: Specific company name to filter (if None, uses current company)
    
    Returns:
        List of receipt dictionaries
    """
    
    # Build XML request for Receipt vouchers
    xml_request = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
    <SVFROMDATE>{from_date}</SVFROMDATE><SVTODATE>{to_date}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    
    try:
        response = requests.post(TALLY_URL, data=xml_request, timeout=30)
        soup = BeautifulSoup(response.content, 'lxml-xml')
        
        vouchers = soup.find_all('VOUCHER')
        if limit:
            vouchers = vouchers[:limit]
        
        receipts = []
        
        for v in vouchers:
            # Basic fields
            receipt_date = v.find('DATE').text if v.find('DATE') else ""
            receipt_number = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            voucher_type = v.find('VOUCHERTYPENAME').text if v.find('VOUCHERTYPENAME') else "Receipt"
            tally_guid = v.find('GUID').text if v.find('GUID') else ""
            
            # Get customer name from PARTYNAME or from ledger entries
            customer_name = v.find('PARTYNAME').text if v.find('PARTYNAME') else ""
            customer_ledger_amount = 0.0
            
            # Extract ALL ledger entries
            ledger_entries = []
            payment_mode = ""
            bank_account = ""
            account_current_balance = 0.0
            rounding_amount = 0.0
            rounding_ledger = ""
            against_reference = ""
            
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                ledger_name = entry.find('LEDGERNAME').text.strip() if entry.find('LEDGERNAME') else ""
                amount = float(entry.find('AMOUNT').text or 0) if entry.find('AMOUNT') else 0
                
                # Get current balance if available
                current_balance_tag = entry.find('CURRENTBALANCE')
                current_balance = 0.0
                if current_balance_tag:
                    current_balance_text = current_balance_tag.text.strip()
                    # Extract numeric value from "4,11,07,348.31 Cr" format
                    import re
                    balance_match = re.search(r'([\d,]+\.?\d*)', current_balance_text)
                    if balance_match:
                        current_balance = float(balance_match.group(1).replace(',', ''))
                        if 'Dr' in current_balance_text:
                            current_balance = -current_balance
                
                # Store ledger entry
                ledger_entry = {
                    "ledger_name": ledger_name,
                    "amount": amount,
                    "current_balance": current_balance
                }
                ledger_entries.append(ledger_entry)
                
                # Identify customer ledger (positive amount, not bank/cash/tax)
                if amount > 0 and not any(keyword in ledger_name.lower() for keyword in ['cash', 'bank', 'cgst', 'sgst', 'igst', 'rounding']):
                    if not customer_name:
                        customer_name = ledger_name
                    customer_ledger_amount = abs(amount)
                
                # Identify bank/cash account (negative amount)
                if amount < 0 and not any(keyword in ledger_name.lower() for keyword in ['rounding']):
                    if 'cash' in ledger_name.lower():
                        payment_mode = "Cash"
                        bank_account = ledger_name
                        account_current_balance = current_balance
                    elif 'bank' in ledger_name.lower():
                        payment_mode = "Bank Transfer"
                        bank_account = ledger_name
                        account_current_balance = current_balance
                    else:
                        if not payment_mode:  # Only set if not already set
                            payment_mode = "Other"
                            bank_account = ledger_name
                            account_current_balance = current_balance
                
                # Identify rounding entries
                if 'rounding' in ledger_name.lower():
                    rounding_amount = amount
                    rounding_ledger = ledger_name
            
            # Extract cost center allocations
            cost_center_allocations = []
            category_allocs_found = v.find_all('CATEGORYALLOCATIONS.LIST')
            
            # Debug logging
            if receipt_number == "1":  # Log for first receipt
                print(f"\n DEBUG Cost Centers for Receipt #{receipt_number}:")
                print(f"   Found {len(category_allocs_found)} CATEGORYALLOCATIONS.LIST elements")
            
            for category_alloc in category_allocs_found:
                category_name = category_alloc.find('CATEGORY').text if category_alloc.find('CATEGORY') else ""
                
                # Find nested cost centers
                cost_centre_allocs = category_alloc.find_all('COSTCENTREALLOCATIONS.LIST')
                
                if cost_centre_allocs:
                    for cc_alloc in cost_centre_allocs:
                        cc_name = cc_alloc.find('NAME').text if cc_alloc.find('NAME') else ""
                        cc_amount = float(cc_alloc.find('AMOUNT').text or 0) if cc_alloc.find('AMOUNT') else 0
                        
                        # Combine Category and Cost Center Name to show BOTH
                        # Format: "Carpets - Distribution Model"
                        full_name = f"{category_name} - {cc_name}" if category_name and cc_name else (cc_name or category_name)
                        
                        # Debug logging
                        if receipt_number == "1":
                            print(f"   - Found Cost Center: '{full_name}' | Amount: {cc_amount}")
                        
                        if full_name:
                            cost_center_allocations.append({
                                "category": full_name,  # Shows "Category - CostCenter"
                                "amount": abs(cc_amount)
                            })
                else:
                    # Fallback: if no nested cost centers, check for direct amount
                    direct_amount_tag = category_alloc.find('AMOUNT', recursive=False)
                    if direct_amount_tag:
                        amount = float(direct_amount_tag.text or 0)
                        if amount != 0:
                            if receipt_number == "1":
                                print(f"   - Found Direct Category: '{category_name}' | Amount: {amount}")
                                
                            cost_center_allocations.append({
                                "category": category_name,
                                "amount": abs(amount)
                            })
            
            if receipt_number == "1":
                print(f"   Total cost_center_allocations: {len(cost_center_allocations)}")
            
            # Get total amount (from customer ledger - positive amount)
            total_amount = customer_ledger_amount
            
            # Get reference/cheque number
            reference_number = v.find('REFERENCE').text if v.find('REFERENCE') else ""
            if not reference_number:
                reference_number = v.find('CHEQUENUMBER').text if v.find('CHEQUENUMBER') else ""
            
            # Get narration
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            # Get invoice allocations (which invoices this payment is applied to)
            invoice_allocations = []
            bill_allocs_found = v.find_all('BILLALLOCATIONS.LIST')
            
            # Debug logging
            if receipt_number in ["1", "323", "152"]:  # Log for specific receipts
                print(f"\n DEBUG Receipt #{receipt_number}:")
                print(f"   Found {len(bill_allocs_found)} BILLALLOCATIONS.LIST elements")
            
            for bill_alloc in bill_allocs_found:
                invoice_name = bill_alloc.find('NAME').text if bill_alloc.find('NAME') else ""
                invoice_amount = float(bill_alloc.find('AMOUNT').text or 0) if bill_alloc.find('AMOUNT') else 0
                bill_type = bill_alloc.find('BILLTYPE').text if bill_alloc.find('BILLTYPE') else "Agst Ref"
                
                # Handle On Account entries which have no name but need to be captured
                if not invoice_name and bill_type == "On Account":
                    invoice_name = "On Account"
                
                # Debug logging
                if receipt_number in ["1", "323", "152"]:
                    print(f"   - Invoice Name: {invoice_name}")
                    print(f"   - Bill Type: {bill_type}")
                    print(f"   - Invoice Amount: {invoice_amount}")
                
                if invoice_name:  # Only require invoice name, not amount
                    # Store the invoice reference
                    if not against_reference:
                        against_reference = invoice_name
                    
                    # Use customer_ledger_amount if invoice_amount is 0
                    final_amount = abs(invoice_amount) if invoice_amount != 0 else customer_ledger_amount
                    
                    invoice_allocations.append({
                        "invoice_number": invoice_name,
                        "bill_type": bill_type, # Added dynamic bill type
                        "amount": final_amount
                    })
                    
                    if receipt_number in ["1", "323", "152"]:
                        print(f"    Added to invoice_allocations: {invoice_name} - {final_amount}")
            
            if receipt_number in ["1", "323", "152"]:
                print(f"   Total invoice_allocations: {len(invoice_allocations)}")
                print(f"   against_reference: {against_reference}")
            
            receipt = {
                "date": receipt_date,
                "receipt_number": receipt_number,
                "voucher_type": voucher_type,
                "customer_name": customer_name,
                "customer_ledger_amount": customer_ledger_amount,
                "payment_mode": payment_mode,
                "bank_account": bank_account,
                "account_current_balance": account_current_balance,
                "amount": total_amount,
                "reference_number": reference_number,
                "against_reference": against_reference,
                "narration": narration,
                "invoice_allocations": invoice_allocations,
                "ledger_entries": ledger_entries,
                "cost_center_allocations": cost_center_allocations,
                "rounding_amount": rounding_amount,
                "rounding_ledger": rounding_ledger,
                "tally_guid": tally_guid
            }
            
            receipts.append(receipt)
        
        print(f" Fetched {len(receipts)} receipts from Tally")
        
        return receipts
        
    except requests.exceptions.RequestException as e:
        print(f" Error connecting to Tally on {TALLY_URL}: {e}")
        raise Exception(f"Failed to connect to Tally on port 9000. Is Tally running and configured for XML export? Error: {str(e)}")
    except Exception as e:
        print(f" Error parsing receipts from Tally: {e}")
        import traceback
        traceback.print_exc()
        raise Exception(f"Error parsing Tally data: {str(e)}")

# ----------------------------------------------------------
# ZOHO BOOKS INTEGRATION
# ----------------------------------------------------------

def get_access_token():
    """Get Zoho access token using refresh token"""
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
        else:
            print(f" Failed to get access token: {response.text}")
            return None
    except Exception as e:
        print(f" Error getting access token: {e}")
        return None

def get_zoho_customers(token):
    """Fetch all customers from Zoho Books"""
    creds = _get_creds()
    url = f"{creds['base_url']}/contacts"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            contacts = response.json().get("contacts", [])
            # Create a map of customer name to customer ID
            customer_map = {}
            for contact in contacts:
                customer_map[contact["contact_name"]] = {
                    "customer_id": contact["contact_id"],
                    "email": contact.get("email", "")
                }
            return customer_map
        else:
            print(f" Failed to fetch customers: {response.text}")
            return {}
    except Exception as e:
        print(f" Error fetching customers: {e}")
        return {}

def get_zoho_invoices(token, customer_id=None):
    """Fetch invoices from Zoho Books for a specific customer"""
    creds = _get_creds()
    url = f"{creds['base_url']}/invoices"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    if customer_id:
        params["customer_id"] = customer_id
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            invoices = response.json().get("invoices", [])
            # Create a map of invoice number to invoice ID and balance
            invoice_map = {}
            for invoice in invoices:
                invoice_map[invoice["invoice_number"]] = {
                    "invoice_id": invoice["invoice_id"],
                    "balance": float(invoice.get("balance", 0)),
                    "total": float(invoice.get("total", 0))
                }
            return invoice_map
        else:
            print(f" Failed to fetch invoices: {response.text}")
            return {}
    except Exception as e:
        print(f" Error fetching invoices: {e}")
        return {}

def get_zoho_bank_accounts(token):
    """Fetch all bank accounts from Zoho Books"""
    creds = _get_creds()
    url = f"{creds['base_url']}/bankaccounts"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            accounts = response.json().get("bankaccounts", [])
            # Create a map of account name to account ID
            account_map = {}
            for account in accounts:
                account_map[account["account_name"]] = account["account_id"]
            return account_map
        else:
            print(f" Failed to fetch bank accounts: {response.text}")
            return {}
    except Exception as e:
        print(f" Error fetching bank accounts: {e}")
        return {}

def create_zoho_payment_received(token, receipt_data, customer_map, invoice_map, bank_account_map):
    """
    Create a payment received in Zoho Books
    """
    creds = _get_creds()
    
    # Get customer ID
    customer_name = receipt_data.get("customer_name", "")
    if customer_name not in customer_map:
        return False, f"Customer '{customer_name}' not found in Zoho Books"
    
    customer_id = customer_map[customer_name]["customer_id"]
    
    # Get bank account ID
    bank_account_name = receipt_data.get("bank_account", "")
    account_id = None
    
    # Try to match bank account
    for acc_name, acc_id in bank_account_map.items():
        if bank_account_name.lower() in acc_name.lower() or acc_name.lower() in bank_account_name.lower():
            account_id = acc_id
            break
    
    if not account_id:
        # Use first available bank account as default
        if bank_account_map:
            account_id = list(bank_account_map.values())[0]
        else:
            return False, "No bank accounts found in Zoho Books"
    
    # Convert date format from YYYYMMDD to YYYY-MM-DD
    date_str = receipt_data.get("date", "")
    if len(date_str) == 8:
        formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    else:
        formatted_date = datetime.now().strftime("%Y-%m-%d")
    
    # Build payment data
    payment_data = {
        "customer_id": customer_id,
        "payment_mode": receipt_data.get("payment_mode", "cash"),
        "amount": receipt_data.get("amount", 0),
        "date": formatted_date,
        "reference_number": receipt_data.get("reference_number", ""),
        "description": receipt_data.get("narration", ""),
        "account_id": account_id,
        "invoices": []
    }
    
    # Add invoice allocations (supports direct match, formatted numbers like TTMH/282/21-22, or prefix like GST-282)
    for allocation in receipt_data.get("invoice_allocations", []):
        inv_ref = allocation.get("invoice_number", "")
        alloc_amt = allocation.get("amount", 0)
        matched_id = None
        
        if inv_ref in invoice_map:
            matched_id = invoice_map[inv_ref]["invoice_id"]
        else:
            ref_digits = [int(d) for d in re.findall(r'\d+', str(inv_ref)) if d.isdigit()]
            if ref_digits:
                target_num = ref_digits[0]
                for mapped_inv_no, inv_info in invoice_map.items():
                    inv_digits = [int(d) for d in re.findall(r'\d+', str(mapped_inv_no)) if d.isdigit()]
                    if target_num in inv_digits:
                        matched_id = inv_info["invoice_id"]
                        break

        if matched_id:
            payment_data["invoices"].append({
                "invoice_id": matched_id,
                "amount_applied": alloc_amt
            })
    
    url = f"{creds['base_url']}/customerpayments"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }
    params = {"organization_id": creds["org_id"]}
    
    try:
        response = requests.post(
            url,
            headers=headers,
            params=params,
            json={"JSONString": json.dumps(payment_data)}
        )
        
        if response.status_code in [200, 201]:
            print(f" Created payment received for {customer_name}: ₹{receipt_data.get('amount', 0)}")
            return True, None
        else:
            error_msg = response.json().get("message", response.text)
            print(f" Failed to create payment: {error_msg}")
            return False, error_msg
            
    except Exception as e:
        error_msg = str(e)
        print(f" Error creating payment: {error_msg}")
        return False, error_msg

# ----------------------------------------------------------
# SYNC FUNCTION
# ----------------------------------------------------------

def sync_receipts_to_zoho(selected_receipts=None, from_date="20250401", to_date="20250430", limit=None, company_name=None):
    """
    Sync receipts to Zoho Books
    
    Args:
        selected_receipts: List of receipt objects to sync (if None, fetches from Tally)
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        limit: Maximum number of receipts to sync
        company_name: Specific company name to filter
    
    Returns:
        Dictionary with sync results
    """
    
    # Get access token
    token = get_access_token()
    if not token:
        return {"status": "error", "message": "Failed to get Zoho access token"}
    
    # Fetch receipts if not provided
    if selected_receipts is None:
        receipts = fetch_tally_receipts(from_date, to_date, limit, company_name)
    else:
        receipts = selected_receipts
    
    if not receipts:
        return {"status": "error", "message": "No receipts to sync"}
    
    # Get Zoho data
    print(" Fetching Zoho Books data...")
    customer_map = get_zoho_customers(token)
    bank_account_map = get_zoho_bank_accounts(token)
    
    # Sync each receipt
    results = {
        "total": len(receipts),
        "success": 0,
        "failed": 0,
        "errors": []
    }
    
    for receipt in receipts:
        # Get invoices for this customer
        customer_name = receipt.get("customer_name", "")
        customer_id = customer_map.get(customer_name, {}).get("customer_id")
        
        invoice_map = {}
        if customer_id:
            invoice_map = get_zoho_invoices(token, customer_id)
        
        success, error = create_zoho_payment_received(
            token, receipt, customer_map, invoice_map, bank_account_map
        )
        
        if success:
            results["success"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({
                "receipt_number": receipt.get("receipt_number", ""),
                "customer": customer_name,
                "error": error
            })
    
    results["status"] = "success"
    results["message"] = f"Synced {results['success']} out of {results['total']} receipts"
    
    return results

# ----------------------------------------------------------
# API WRAPPER FOR FRONTEND
# ----------------------------------------------------------

def get_all_receipts_data(from_date="20250401", to_date="20250430", limit=None, company_name=None):
    """
    Wrapper function for API to get receipt data
    Returns formatted data for frontend display
    Saves data to SQLite database
    """
    # Initialize DB if possible
    if database_manager:
        database_manager.init_db()
    
    receipts = fetch_tally_receipts(from_date, to_date, limit, company_name)
    
    # Save each receipt to database
    if database_manager and receipts:
        from datetime import datetime
        db_data_list = []
        
        for receipt in receipts:
            db_data = {
                "receipt_number": receipt.get("receipt_number", ""),
                "voucher_type": receipt.get("voucher_type", ""),
                "date": receipt.get("date", ""),
                "customer_name": receipt.get("customer_name", ""),
                "customer_ledger_amount": receipt.get("customer_ledger_amount", 0) or 0,
                "payment_mode": receipt.get("payment_mode", ""),
                "bank_account": receipt.get("bank_account", ""),
                "account_current_balance": receipt.get("account_current_balance", 0) or 0,
                "amount": receipt.get("amount", 0) or 0,
                "reference_number": receipt.get("reference_number", ""),
                "against_reference": receipt.get("against_reference", ""),
                "narration": receipt.get("narration", ""),
                "invoice_allocations": json.dumps(receipt.get("invoice_allocations", [])),
                "ledger_entries": json.dumps(receipt.get("ledger_entries", [])),
                "cost_center_allocations": json.dumps(receipt.get("cost_center_allocations", [])),
                "rounding_amount": receipt.get("rounding_amount", 0) or 0,
                "rounding_ledger": receipt.get("rounding_ledger", ""),
                "tally_guid": receipt.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            db_data_list.append(db_data)
            
        # Bulk save to prevent database lock issues
        database_manager.bulk_save_receipts(db_data_list)
        print(f" Saved {len(receipts)} receipts to database")
    
    # Calculate totals
    total_amount = sum(r.get("amount", 0) for r in receipts)
    
    return {
        "receipts": receipts,
        "count": len(receipts),
        "total_amount": total_amount,
        "from_date": from_date,
        "to_date": to_date
    }


def get_all_receipts_data_day_by_day(from_date="20250401", to_date="20250430", limit=None, company_name=None, *, log=None, stop_event=None):
    """
    Job-friendly wrapper:
    - Fetches receipts day-by-day (prevents repeated counts / stale dates)
    - Upserts into SQLite preferring tally_guid
    """
    _emit = _make_emitter(log)

    if database_manager:
        try:
            database_manager.init_db()
        except Exception:
            pass

    receipts = fetch_tally_receipts_day_by_day(from_date, to_date, limit, company_name, log=log, stop_event=stop_event)

    if database_manager and receipts:
        from datetime import datetime as _dt
        db_data_list = []
        for receipt in receipts:
            db_data_list.append({
                "receipt_number": receipt.get("receipt_number", ""),
                "voucher_type": receipt.get("voucher_type", ""),
                "date": receipt.get("date", ""),
                "customer_name": receipt.get("customer_name", ""),
                "customer_ledger_amount": receipt.get("customer_ledger_amount", 0) or 0,
                "payment_mode": receipt.get("payment_mode", ""),
                "bank_account": receipt.get("bank_account", ""),
                "account_current_balance": receipt.get("account_current_balance", 0) or 0,
                "amount": receipt.get("amount", 0) or 0,
                "reference_number": receipt.get("reference_number", ""),
                "against_reference": receipt.get("against_reference", ""),
                "narration": receipt.get("narration", ""),
                "invoice_allocations": json.dumps(receipt.get("invoice_allocations", []), ensure_ascii=False),
                "ledger_entries": json.dumps(receipt.get("ledger_entries", []), ensure_ascii=False),
                "cost_center_allocations": json.dumps(receipt.get("cost_center_allocations", []), ensure_ascii=False),
                "rounding_amount": receipt.get("rounding_amount", 0) or 0,
                "rounding_ledger": receipt.get("rounding_ledger", ""),
                "tally_guid": receipt.get("tally_guid", ""),
                "company_name": company_name or "",
                "created_at": _dt.now().isoformat(),
                "updated_at": _dt.now().isoformat(),
            })

        save_fn = getattr(database_manager, "bulk_save_receipts_by_guid", None) or getattr(database_manager, "bulk_save_receipts", None)
        if callable(save_fn):
            save_fn(db_data_list)
            _emit(f"Saved {len(db_data_list)} receipts to SQLite")

    total_amount = sum(float(r.get("amount", 0) or 0) for r in receipts)
    return {
        "status": "success",
        "receipts": receipts,
        "count": len(receipts),
        "total_amount": total_amount,
        "from_date": from_date,
        "to_date": to_date,
    }


def parse_tally_json(json_path):
    import json, re
    try:
        with open(json_path, 'r', encoding='utf-16') as f: data = json.load(f)
    except:
        with open(json_path, 'r', encoding='utf-8') as f: data = json.load(f)
    if isinstance(data, list) and len(data) > 0 and 'date' in data[0]: return data
    vouchers = data.get('tallymessage', [])
    if isinstance(vouchers, dict): vouchers = [vouchers]
    
    receipts = []
    for v in vouchers:
        if not isinstance(v, dict): continue
        if 'vouchernumber' not in v and 'vouchertypename' not in v: continue
        
        r_date = str(v.get('date', '')).strip()
        tally_guid = str(v.get('guid', '')).strip()
        r_no = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or tally_guid or '').strip()
        if not r_no:
            import hashlib
            r_no = "RCT-AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
            
        if 'seen_r_no' not in locals(): seen_r_no = set()
        original_r_no = r_no
        counter = 1
        while r_no in seen_r_no:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            r_no = f"{original_r_no}_{suffix}"
            counter += 1
        seen_r_no.add(r_no)
        
        voucher_type = str(v.get('vouchertypename', 'Receipt')).strip()
        customer_name = str(v.get('partyname', '')).strip()
        narration = str(v.get('narration', '')).strip()
        ref_no = str(v.get('reference', v.get('chequenumber', ''))).strip()
        
        ledger_entries = []; cost_centers_dict = {}
        bank_account = ""; payment_mode = ""; account_current_balance = 0.0
        rounding_amount = 0.0; rounding_ledger = ""
        customer_ledger_amount = 0.0
        against_reference = ""
        
        all_entries = v.get('allledgerentries.list', v.get('allledgerentries', []))
        if not isinstance(all_entries, list): all_entries = [all_entries]
        
        for entry in all_entries:
            if not isinstance(entry, dict): continue
            lname = str(entry.get('ledgername', '')).strip()
            amt_str = str(entry.get('amount', '0'))
            nums = re.findall(r'[-\d.]+', amt_str)
            amt = float(nums[-1]) if nums else 0.0
            
            ledger_entries.append({"ledger_name": lname, "amount": amt, "current_balance": 0.0})
            
            lname_lower = lname.lower()
            if amt > 0 and not any(k in lname_lower for k in ['cash', 'bank', 'cgst', 'sgst', 'igst', 'rounding']):
                if not customer_name: customer_name = lname
                customer_ledger_amount = abs(amt)
            if amt < 0 and 'rounding' not in lname_lower:
                if 'cash' in lname_lower: payment_mode = "Cash"; bank_account = lname
                elif 'bank' in lname_lower: payment_mode = "Bank Transfer"; bank_account = lname
                elif not payment_mode: payment_mode = "Other"; bank_account = lname
            if 'rounding' in lname_lower:
                rounding_amount = amt; rounding_ledger = lname
                
            cats = entry.get('categoryallocations.list', entry.get('categoryallocations', []))
            if not isinstance(cats, list): cats = [cats]
            for ca in cats:
                if not isinstance(ca, dict): continue
                cat_name = str(ca.get('category', '')).strip()
                ccs = ca.get('costcentreallocations.list', ca.get('costcentreallocations', []))
                if not isinstance(ccs, list): ccs = [ccs]
                for cc in ccs:
                    if not isinstance(cc, dict): continue
                    cc_name = str(cc.get('name', '')).strip()
                    cc_amt_str = str(cc.get('amount', '0'))
                    ccnums = re.findall(r'[-\d.]+', cc_amt_str)
                    cc_amt = float(ccnums[-1]) if ccnums else 0.0
                    full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full: cost_centers_dict[full] = abs(cc_amt)
                    
        invoice_allocations = []
        for entry in all_entries:
            bills = entry.get('billallocations.list', entry.get('billallocations', []))
            if not isinstance(bills, list): bills = [bills]
            for ba in bills:
                if not isinstance(ba, dict): continue
                bname = str(ba.get('name', '')).strip()
                bamt_str = str(ba.get('amount', '0'))
                bnums = re.findall(r'[-\d.]+', bamt_str)
                bamt = float(bnums[-1]) if bnums else 0.0
                btype = str(ba.get('billtype', 'Agst Ref')).strip()
                if not bname and btype == "On Account": bname = "On Account"
                if bname:
                    if not against_reference: against_reference = bname
                    invoice_allocations.append({"invoice_number": bname, "bill_type": btype, "amount": abs(bamt) if bamt != 0 else customer_ledger_amount})

        cost_center_allocations = [{"category": k, "amount": v} for k, v in cost_centers_dict.items()]
        receipts.append({"date": r_date, "receipt_number": r_no, "voucher_type": voucher_type, "customer_name": customer_name, "customer_ledger_amount": customer_ledger_amount, "payment_mode": payment_mode, "bank_account": bank_account, "account_current_balance": account_current_balance, "amount": customer_ledger_amount, "reference_number": ref_no, "against_reference": against_reference, "narration": narration, "invoice_allocations": invoice_allocations, "ledger_entries": ledger_entries, "cost_center_allocations": cost_center_allocations, "rounding_amount": rounding_amount, "rounding_ledger": rounding_ledger, "tally_guid": tally_guid})
    return receipts


# ----------------------------------------------------------
# ZOHO SYNC (JOB MODE) — RECEIPTS ROUTING LOGIC
# ----------------------------------------------------------

def generate_sync_errors_excel(errors):
    import io
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Border, Side, Alignment
    from datetime import datetime

    wb = Workbook()
    
    # --- Sheet 1: Receipts ---
    ws1 = wb.active
    ws1.title = "Receipts"
    
    headers1 = ["Receipt Number", "Date", "Customer Name", "Payment Mode", "Amount", "Bank Account", "Reference Number", "Description", "Sync Error Message", "Action Required / Fix"]
    ws1.append(headers1)
    
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    
    for col_idx, _ in enumerate(headers1, 1):
        cell = ws1.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center")
    
    for err in errors:
        error_msg = (
            err.get("error")
            or err.get("Error Message")
            or err.get("zoho_error")
            or err.get("Sync Error Message")
            or "Unknown Error"
        )
        fix = ""
        if "customer" in error_msg.lower():
            fix = "Create customer in Zoho Books first"
        elif "bank" in error_msg.lower() or "account" in error_msg.lower():
            fix = "Verify bank account mapping"
        elif "amount mismatch" in error_msg.lower() or "invoice not found" in error_msg.lower():
            fix = "Sync/create missing invoice in Zoho Books or check invoice amount"
        else:
            fix = "Review error message and update Tally data"
            
        r_no = err.get("receipt_number") or err.get("Receipt Number") or err.get("voucher_number") or ""
        r_date = err.get("date") or err.get("Date") or err.get("receipt_date") or ""
        r_cust = err.get("customer") or err.get("Customer") or err.get("customer_name") or err.get("party_name") or ""
        r_mode = err.get("payment_mode") or err.get("Payment Mode") or "Bank Transfer"
        r_amt = err.get("amount") or err.get("Amount") or err.get("total_amount") or ""
        r_bank = err.get("bank_account") or err.get("Bank Account") or err.get("bank_name") or ""
        r_ref = err.get("reference_number") or err.get("Reference Number") or err.get("Invoice Numbers") or err.get("ref") or ""
        r_desc = err.get("description") or err.get("Description") or err.get("narration") or ""
            
        row = [
            r_no,
            r_date,
            r_cust,
            r_mode,
            r_amt,
            r_bank,
            r_ref,
            r_desc,
            error_msg,
            fix
        ]
        ws1.append(row)
        for col_idx in range(1, len(row) + 1):
            ws1.cell(row=ws1.max_row, column=col_idx).border = thin_border
            
    for col in ws1.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws1.column_dimensions[column].width = min((max_length + 2) * 1.2, 50)
        
    # --- Sheet 2: Error Summary ---
    ws2 = wb.create_sheet(title="Error Summary")
    headers2 = ["S.No", "Receipt Number", "Date", "Customer Name", "Total Amount (₹)", "Sync Status", "Error Message / Reason", "Action Required / Fix", "Logged At"]
    ws2.append(headers2)
    
    for col_idx, _ in enumerate(headers2, 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center")
        
    logged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for idx, err in enumerate(errors, 1):
        error_msg = (
            err.get("error")
            or err.get("Error Message")
            or err.get("zoho_error")
            or err.get("Sync Error Message")
            or "Unknown Error"
        )
        fix = ""
        if "customer" in error_msg.lower():
            fix = "Create customer in Zoho Books first"
        elif "bank" in error_msg.lower() or "account" in error_msg.lower():
            fix = "Verify bank account mapping"
        elif "amount mismatch" in error_msg.lower() or "invoice not found" in error_msg.lower():
            fix = "Sync/create missing invoice in Zoho Books or check invoice amount"
        else:
            fix = "Review error message and update Tally data"
            
        r_no = err.get("receipt_number") or err.get("Receipt Number") or err.get("voucher_number") or ""
        r_date = err.get("date") or err.get("Date") or err.get("receipt_date") or ""
        r_cust = err.get("customer") or err.get("Customer") or err.get("customer_name") or err.get("party_name") or ""
        r_amt = err.get("amount") or err.get("Amount") or err.get("total_amount") or ""

        row = [
            idx,
            r_no,
            r_date,
            r_cust,
            r_amt,
            "FAILED",
            error_msg,
            fix,
            logged_at
        ]
        ws2.append(row)
        for col_idx in range(1, len(row) + 1):
            ws2.cell(row=ws2.max_row, column=col_idx).border = thin_border
            
    for col in ws2.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws2.column_dimensions[column].width = min((max_length + 2) * 1.2, 50)
        
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def sync_receipts_to_zoho_job(from_date="20250401", to_date="20250430", limit=None, company_name=None, *, receipt_numbers=None, cutoff_date="2025-03-31", opening_invoice_id="", log=None, stop_event=None):
    """
    Sync Receipts to Zoho Books using allocation-level routing rules:
    - Advance/On Account (Cr) -> Customer Advance
    - Agst Ref (Cr) -> Customer Payment applied to invoices (with prev/current year accumulation)
    - New Ref (Cr) -> if ref in Sales_Invoice_Master -> Invoice Payment else Customer Advance
    """
    _emit = _make_emitter(log)

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

    # Build Sales_Invoice_Master from local invoices table
    sales_invoice_master = set()
    try:
        for inv in (database_manager.get_all_invoices() or []):
            no = (inv.get("invoice_number") or "").strip()
            if no:
                sales_invoice_master.add(no)
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
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    # Cache: customers by normalized name (backed by SQLite zoho_masters_cache)
    customer_cache = {}

    def _load_customers():
        creds = _get_creds()
        org_id = creds.get("org_id", "")
        # 1. Try SQLite master cache first
        if database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
            cached = database_manager.get_zoho_master_cache('receipt_customers', expected_org_id=org_id)
            if cached and isinstance(cached, dict):
                customer_cache.update(cached)
                _emit(f"Loaded {len(customer_cache)} customers from local SQLite DB cache (0 API calls)")
                return

        # 2. Query Zoho API if not in DB cache
        page = 1
        while True:
            resp = zoho.api_call("GET", "/contacts", params={"page": page, "per_page": 200, "contact_type": "customer"})
            if resp.get("code") != 0:
                break
            items = resp.get("contacts", []) or []
            for c in items:
                nm = (c.get("contact_name") or "").strip()
                cid = (c.get("contact_id") or "").strip()
                if nm and cid:
                    customer_cache[_norm(nm)] = cid
            has_more = resp.get("page_context", {}).get("has_more_page", False)
            if not has_more:
                break
            page += 1
        
        # Save to SQLite master cache for subsequent runs
        if customer_cache and database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
            database_manager.save_zoho_master_cache('receipt_customers', customer_cache, org_id=org_id)

    def _get_customer_id(customer_name: str) -> str:
        if not customer_cache:
            _emit("Loading Zoho customers...")
            _load_customers()
            _emit(f"Loaded {len(customer_cache)} customers")
        
        c_norm = _norm(customer_name)
        # 1. Exact normalized match
        if c_norm in customer_cache:
            return customer_cache[c_norm]
        
        # 2. Strip "ms" or "m/s" prefix
        c_clean = re.sub(r'^(ms|m/s)\s*', '', c_norm)
        if c_clean in customer_cache:
            return customer_cache[c_clean]
        
        # 3. Partial substring match in customer_cache
        for k, cid in customer_cache.items():
            if c_clean and (c_clean in k or k in c_clean):
                return cid
            if "dhl" in c_norm and "dhl" in k:
                return cid
            if "coldchain" in c_norm and "coldchain" in k:
                return cid
            if "arscent" in c_norm and "arscent" in k:
                return cid
            if "biomerieux" in c_norm and "biomerieux" in k:
                return cid
            if "perkin" in c_norm and "perkin" in k:
                return cid
        return ""

    # Cache: bank accounts (backed by SQLite zoho_masters_cache)
    bank_account_cache = {}

    def _load_bank_accounts():
        creds = _get_creds()
        org_id = creds.get("org_id", "")
        # 1. Try SQLite master cache first
        if database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
            cached = database_manager.get_zoho_master_cache('bank_accounts', expected_org_id=org_id)
            if cached and isinstance(cached, dict):
                bank_account_cache.update(cached)
                _emit(f"Loaded {len(bank_account_cache)} bank accounts from local SQLite DB cache (0 API calls)")
                return

        # 2. Query Zoho API if not in DB cache
        resp = zoho.api_call("GET", "/bankaccounts")
        if resp.get("code") == 0:
            accounts = resp.get("bankaccounts", []) or []
            for acc in accounts:
                nm = (acc.get("account_name") or "").strip()
                aid = (acc.get("account_id") or "").strip()
                if nm and aid:
                    bank_account_cache[_norm(nm)] = aid
            
            # Save to SQLite master cache
            if bank_account_cache and database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
                database_manager.save_zoho_master_cache('bank_accounts', bank_account_cache, org_id=org_id)

    def _get_account_id(bank_name: str) -> str:
        if not bank_account_cache:
            _emit("Loading Zoho bank accounts...")
            _load_bank_accounts()
            _emit(f"Loaded {len(bank_account_cache)} bank accounts")
        if not bank_name:
            # Fallback to first available if no bank name provided
            return list(bank_account_cache.values())[0] if bank_account_cache else ""
        norm_bank = _norm(bank_name)
        # 1) Exact match
        if norm_bank in bank_account_cache:
            return bank_account_cache[norm_bank]
        # 2) Partial match
        for nm, aid in bank_account_cache.items():
            if norm_bank in nm or nm in norm_bank:
                return aid
        # 3) Fallback
        return list(bank_account_cache.values())[0] if bank_account_cache else ""

    # Load cached Zoho invoices from local SQLite table (Zero-API reuse)
    all_zoho_invoices_cache = []
    try:
        all_zoho_invoices_cache = database_manager.get_all_cached_zoho_invoices() or []
    except Exception:
        all_zoho_invoices_cache = []

    # Merge synced invoices from local invoices table (e.g. TTMH/276/18-19)
    existing_ids = {i.get("invoice_id") for i in all_zoho_invoices_cache if i.get("invoice_id")}
    try:
        local_invoices = database_manager.get_all_invoices() or []
        added_from_local = 0
        for li in local_invoices:
            zid = str(li.get("zoho_invoice_id") or "").strip()
            if zid and zid not in existing_ids:
                ld = str(li.get("date") or "").replace("-", "").strip()
                fdate = f"{ld[:4]}-{ld[4:6]}-{ld[6:]}" if len(ld) == 8 else ld
                tot = float(li.get("total_amount") or 0.0)
                cname = li.get("customer_name") or ""
                cid = _get_customer_id(cname)
                all_zoho_invoices_cache.append({
                    "invoice_id": zid,
                    "invoice_number": li.get("invoice_number"),
                    "customer_id": cid,
                    "customer_name": cname,
                    "date": fdate,
                    "total": tot,
                    "balance": tot,
                })
                existing_ids.add(zid)
                added_from_local += 1
        if added_from_local > 0:
            _emit(f"Merged {added_from_local} synced invoices from local invoices table into cache.")
    except Exception as e:
        _emit(f"Warning merging local invoices to cache: {e}")

    if all_zoho_invoices_cache:
        _emit(f"Loaded {len(all_zoho_invoices_cache)} Zoho invoices in cache (0 API calls)")

    # If cache table is empty, query Zoho Books API and populate cache
    if not all_zoho_invoices_cache:
        try:
            _emit("Fetching Zoho invoices for local cache...")
            page = 1
            while True:
                resp = zoho.api_call("GET", "/invoices", params={"page": page, "per_page": 200})
                if resp.get("code") != 0:
                    break
                items = resp.get("invoices", []) or []
                all_zoho_invoices_cache.extend(items)
                if not resp.get("page_context", {}).get("has_more_page", False):
                    break
                page += 1
            if all_zoho_invoices_cache:
                database_manager.bulk_save_zoho_invoices_cache(all_zoho_invoices_cache)
                _emit(f"Saved {len(all_zoho_invoices_cache)} Zoho invoices to local cache database.")
        except Exception as e:
            _emit(f"Warning: Could not fetch invoices from Zoho: {e}")

    def _extract_digits(s):
        import re
        return [int(d) for d in re.findall(r"\d+", str(s or "")) if d.isdigit()]

    def _extract_narration_invoice_numbers(narration_text: str):
        """
        Dynamically extracts invoice/bill numbers mentioned in narration.
        Handles patterns like:
        - 'Being payment received against invoice no 28' -> ['28']
        - 'Being payment made 28 bill no' -> ['28']
        - 'Payment received against bill no. 330 & 349' -> ['330', '349']
        - 'inv 45, 46' -> ['45', '46']
        - 'KLE/33/16-17' -> ['KLE/33/16-17', '33']
        """
        if not narration_text:
            return []
        patterns = [
            r'(?:invoice|bill|inv|bill\s*no|inv\s*no|invoice\s*no|no)[.:\s#]+([a-zA-Z0-9/\-_&,\s]+)',
            r'([a-zA-Z0-9/\-_]+)\s*(?:bill\s*no|inv\s*no|bill|invoice)',
            r'([A-Z]{2,4}/[\d]+/[A-Z\d\-]+)'
        ]
        found_tokens = []
        for pat in patterns:
            matches = re.findall(pat, narration_text, re.IGNORECASE)
            for m in matches:
                # Split multiple numbers like "330 & 349" or "45, 46"
                parts = re.split(r'[&,/\s]+', str(m).strip())
                for p in parts:
                    clean_p = p.strip()
                    if clean_p and (clean_p.isdigit() or any(c.isdigit() for c in clean_p)):
                        found_tokens.append(clean_p)
        # Also extract individual digits from narration
        raw_digits = [str(d) for d in _extract_digits(narration_text)]
        all_found = list(dict.fromkeys(found_tokens + raw_digits))
        return all_found

    def _parse_date_int(d_str):
        if not d_str:
            return 0
        s = re.sub(r'[^0-9]', '', str(d_str))
        if len(s) >= 8:
            return int(s[:8])
        return 0

    _customer_ob_cache = {}
    def _get_customer_ob_invoice(cid):
        if not cid:
            return None
        if cid in _customer_ob_cache:
            return _customer_ob_cache[cid]
        try:
            resp = zoho.api_call("GET", f"/contacts/{cid}")
            contact = (resp or {}).get("contact", {})
            obs = contact.get("opening_balances", [])
            if obs and isinstance(obs, list) and len(obs) > 0:
                ob_info = obs[0]
                ob_inv_id = ob_info.get("ob_invoice_id")
                ob_amt = _safe_float(ob_info.get("opening_balance_amount") or contact.get("opening_balance_amount"))
                if ob_inv_id:
                    res_obj = {
                        "invoice_id": str(ob_inv_id),
                        "invoice_number": "Customer opening balance",
                        "total": ob_amt,
                        "balance": ob_amt,
                        "customer_id": cid,
                        "customer_name": contact.get("contact_name") or "",
                        "date": "2018-03-31",
                        "is_ob": True
                    }
                    _customer_ob_cache[cid] = res_obj
                    return res_obj
        except Exception as e:
            pass
        _customer_ob_cache[cid] = None
        return None

    def _resolve_invoice(invoice_number: str, cust_id: str = "", cust_name: str = "", amount: float = 0.0, narration: str = "", exclude_invoice_ids: set = None, receipt_date: str = ""):
        """
        Dynamic Multi-Tier Invoice Resolution for Payment Received:
        1. Condition 1: Direct Exact Invoice Number Match (e.g. TTMH/282/21-22 or 282 or GF/282).
        2. Condition 2: Dynamic VCH / Ref Number Match (e.g. '282', 'GST-282', 'GST - 282' -> matching 'TTMH/282/21-22' or '282').
           - Filters and resolves year change duplicates: prioritizes invoices issued BEFORE / ON payment received date.
           - Checks amount matching (full amount or partial/TDS).
        3. Condition 3: Dynamic Narration Number Extraction + Date Prior + Amount Matching.
        4. Condition 4: Exact Customer + Prior Date + Exact Amount Match.
        5. Condition 1 Fallback: Direct Zoho Search API if not found in cache.
        """
        exclude_invoice_ids = exclude_invoice_ids or set()
        key = (invoice_number or "").strip()
        ref_digits = _extract_digits(key)
        narr_tokens = _extract_narration_invoice_numbers(narration)
        narr_digits = _extract_digits(narration)
        r_date_int = _parse_date_int(receipt_date)

        cname_clean = _norm(cust_name)
        # Filter all Zoho invoices belonging to this customer (excluding already matched ones)
        cust_invs = [
            inv for inv in all_zoho_invoices_cache
            if inv.get("invoice_id") not in exclude_invoice_ids
            and (
                (cust_id and inv.get("customer_id") == cust_id)
                or (_norm(inv.get("customer_name")) in cname_clean or cname_clean in _norm(inv.get("customer_name")))
                or ("becton" in cname_clean and "becton" in _norm(inv.get("customer_name")))
                or ("perkin" in cname_clean and "perkin" in _norm(inv.get("customer_name")))
                or ("biomerieux" in cname_clean and "biomerieux" in _norm(inv.get("customer_name")))
                or ("dhl" in cname_clean and "dhl" in _norm(inv.get("customer_name")))
            )
        ]

        def _is_amount_matching(inv_obj, target_amount):
            tot = float(inv_obj.get("total") or 0)
            bal = float(inv_obj.get("balance") or 0)
            # Check match against total or outstanding balance within tolerance
            return abs(tot - target_amount) <= 10.0 or abs(bal - target_amount) <= 10.0

        # -------------------------------------------------------------
        # CONDITION 1: Exact Agst Ref Match
        # -------------------------------------------------------------
        if key:
            for inv in cust_invs:
                inv_no = (inv.get("invoice_number") or "").strip()
                if inv_no.lower() == key.lower():
                    inv_bal = float(inv.get("balance") or inv.get("total") or 0)
                    if _is_amount_matching(inv, amount):
                        return inv, "Condition 1: Exact Agst Ref & Full Amount Match"
                    elif amount <= (inv_bal + 50.0):
                        return inv, f"Condition 1: Exact Agst Ref (Partial/TDS Payment: ₹{amount:,.2f} of Total ₹{inv.get('total', 0):,.2f})"

        # -------------------------------------------------------------
        # CONDITION 2: Dynamic VCH / Ref Number Match (e.g. '276' or 'GST-276' -> 'TTMH/276/18-19')
        # Handles Year Change: prioritizes formatted invoices (TTMH/...) issued BEFORE / ON payment received date
        # -------------------------------------------------------------
        if ref_digits:
            target_num = ref_digits[0]
            inv_pool = cust_invs if cust_invs else all_zoho_invoices_cache
            matching_candidates = []
            for inv in inv_pool:
                inv_no = (inv.get("invoice_number") or "").strip()
                inv_digits = _extract_digits(inv_no)
                if target_num in inv_digits:
                    # Check if formatted (e.g. TTMH/276/18-19) vs raw number (e.g. 276)
                    is_formatted = bool('/' in inv_no or any(c.isalpha() for c in inv_no))
                    inv_d_int = _parse_date_int(inv.get("date"))
                    is_prior = (not r_date_int or inv_d_int <= r_date_int)
                    matching_candidates.append((is_formatted, is_prior, inv_d_int, inv))

            if matching_candidates:
                # Sort: formatted first (True > False), prior date first (True > False), latest date first
                matching_candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

                # 2a. Priority 1: Formatted invoice (e.g. TTMH/276/18-19) issued ON or BEFORE payment received date
                # User Rule: "CHECK FIRST INVOICE NUMBER LIKE TTMH/276/18-19, BEFORE PAYMENT RECEIVED DATE, THEN AMOUNT LEAVE"
                for is_fmt, is_pr, d_int, inv in matching_candidates:
                    if is_fmt and is_pr:
                        inv_no = (inv.get("invoice_number") or "").strip()
                        if _is_amount_matching(inv, amount):
                            return inv, f"Condition 2: Formatted Invoice Match ('{inv_no}') + Prior Date ({inv.get('date')}) + Full Amount Match"
                        else:
                            return inv, f"Condition 2: Formatted Invoice Match ('{inv_no}') + Prior Date ({inv.get('date')}) (Applied: ₹{amount:,.2f} of Total ₹{inv.get('total', 0):,.2f})"

                # 2b. Priority 2: Prior date candidate with exact amount match
                for is_fmt, is_pr, d_int, inv in matching_candidates:
                    if is_pr and _is_amount_matching(inv, amount):
                        inv_no = (inv.get("invoice_number") or "").strip()
                        return inv, f"Condition 2: VCH No Match ({target_num} in '{inv_no}') + Prior Date ({inv.get('date')}) + Amount Match"

                # 2c. Priority 3: Prior date candidate with partial / TDS amount
                for is_fmt, is_pr, d_int, inv in matching_candidates:
                    if is_pr:
                        inv_no = (inv.get("invoice_number") or "").strip()
                        inv_bal = float(inv.get("balance") or inv.get("total") or 0)
                        if amount <= (inv_bal + 50.0):
                            return inv, f"Condition 2: VCH No Match ({target_num} in '{inv_no}') + Prior Date (Partial/TDS Payment: ₹{amount:,.2f})"

                # 2d. Priority 4: Fallback across any candidate for exact amount match
                for is_fmt, is_pr, d_int, inv in matching_candidates:
                    if _is_amount_matching(inv, amount):
                        inv_no = (inv.get("invoice_number") or "").strip()
                        return inv, f"Condition 2: VCH No Match ({target_num} in '{inv_no}') + Amount Match"

        # -------------------------------------------------------------
        # CONDITION 3: Dynamic Narration Number Extraction + Date Prior + Amount Matching
        # -------------------------------------------------------------
        if narr_tokens or narr_digits:
            narr_candidates = []
            for inv in cust_invs:
                inv_no = (inv.get("invoice_number") or "").strip()
                inv_digits = _extract_digits(inv_no)
                narr_match = (
                    any(tok.lower() in inv_no.lower() or inv_no.lower() in tok.lower() for tok in narr_tokens)
                    or any(nd in inv_digits for nd in narr_digits)
                )
                if narr_match:
                    narr_candidates.append(inv)

            if narr_candidates:
                prior_narr = [
                    inv for inv in narr_candidates 
                    if not r_date_int or _parse_date_int(inv.get("date")) <= r_date_int
                ]
                prior_narr.sort(key=lambda x: _parse_date_int(x.get("date")), reverse=True)

                for inv in prior_narr:
                    if _is_amount_matching(inv, amount):
                        inv_no = (inv.get("invoice_number") or "").strip()
                        return inv, f"Condition 3: Dynamic Narration Match ('{inv_no}') + Prior Date + Amount Match"

                for inv in narr_candidates:
                    if _is_amount_matching(inv, amount):
                        inv_no = (inv.get("invoice_number") or "").strip()
                        return inv, f"Condition 3: Dynamic Narration Match ('{inv_no}') + Amount Match"

        # -------------------------------------------------------------
        # CONDITION 4: Exact Customer + Prior Date + Exact Amount Match
        # -------------------------------------------------------------
        prior_cust_invs = [
            inv for inv in cust_invs 
            if not r_date_int or _parse_date_int(inv.get("date")) <= r_date_int
        ]
        prior_cust_invs.sort(key=lambda x: _parse_date_int(x.get("date")), reverse=True)
        for inv in prior_cust_invs:
            if _is_amount_matching(inv, amount):
                inv_no = (inv.get("invoice_number") or "").strip()
                return inv, f"Condition 4: Customer Prior Date + Exact Amount Match ('{inv_no}')"

        for inv in cust_invs:
            if _is_amount_matching(inv, amount):
                inv_no = (inv.get("invoice_number") or "").strip()
                return inv, f"Condition 4: Customer Exact Amount Match ('{inv_no}')"

        # -------------------------------------------------------------
        # CONDITION 1 Fallback: Direct Zoho Search API if key exists
        # -------------------------------------------------------------
        if key:
            resp = zoho.api_call("GET", "/invoices", params={"search_text": key, "per_page": 200})
            if resp.get("code") == 0:
                for inv in resp.get("invoices", []) or []:
                    inv_no = (inv.get("invoice_number") or "").strip()
                    if inv_no.lower() == key.lower():
                        inv_bal = float(inv.get("balance") or inv.get("total") or 0)
                        if _is_amount_matching(inv, amount):
                            return inv, "Condition 1: Exact API Match & Full Amount Match"
                        elif amount <= (inv_bal + 50.0):
                            return inv, f"Condition 1: Exact API Match (Partial/TDS Payment: ₹{amount:,.2f})"

        # -------------------------------------------------------------
        # CONDITION 5: Customer Opening Balance Match (for pre-FY 18-19 bills)
        # Database only covers FY 18-22; if invoice is prior to 18-19,
        # apply payment amount to the customer's Opening Balance invoice.
        # -------------------------------------------------------------
        if cust_id:
            ob_inv = _get_customer_ob_invoice(cust_id)
            if ob_inv:
                ob_inv_no = ob_inv.get("invoice_number", "Customer opening balance")
                ob_tot = float(ob_inv.get("total") or ob_inv.get("balance") or 0)
                if ob_tot > 0 or amount > 0:
                    return ob_inv, f"Condition 5: Customer Opening Balance Match ('{ob_inv_no}') (Pre-FY 18-19 Bill '{key}', OB Total: ₹{ob_tot:,.2f})"

        return None, "Amount mismatch or Invoice not found"

    # Read receipts from DB (active company DB is already set in session via before_request)
    receipts = database_manager.get_all_receipts() or []

    if receipt_numbers and isinstance(receipt_numbers, list):
        target_set = {str(rn).strip() for rn in receipt_numbers if str(rn).strip()}
        receipts = [r for r in receipts if str(r.get("receipt_number") or "").strip() in target_set]
    else:
        # filter by date range (YYYYMMDD strings) if provided
        if from_date:
            receipts = [r for r in receipts if (r.get("date") or "") >= from_date]
        if to_date:
            receipts = [r for r in receipts if (r.get("date") or "") <= to_date]

    if limit:
        try:
            receipts = receipts[: int(limit)]
        except Exception:
            pass

    if not receipts:
        return {"status": "error", "message": "No receipts found in DB for the selected selection or date range"}

    stats = {"total": len(receipts), "payments_created": 0, "advances_created": 0, "already_synced": 0, "skipped": 0, "failed": 0}
    errors = []

    for idx, r in enumerate(receipts, 1):
        if _should_stop(stop_event):
            _emit("Stopped by user.")
            return {"status": "stopped", "stats": stats, "errors": errors}

        receipt_no = r.get("receipt_number") or ""
        receipt_date_iso = _tally_to_iso(r.get("date") or "")

        customer_name = (r.get("customer_name") or "").strip()
        customer_id = _get_customer_id(customer_name)
        if not customer_id:
            stats["failed"] += 1
            err_msg = f"Customer not found in Zoho: {customer_name}"
            errors.append({
                "receipt_number": receipt_no,
                "date": receipt_date_iso,
                "customer": customer_name,
                "payment_mode": r.get("payment_mode") or "Bank Transfer",
                "amount": float(r.get("total_amount") or r.get("amount") or 0),
                "bank_account": r.get("bank_account") or "",
                "reference_number": r.get("reference_number") or "",
                "description": r.get("narration") or "",
                "error": err_msg
            })
            _emit(f"[{idx}/{stats['total']}] Customer not found: {customer_name} (receipt {receipt_no})")
            continue

        try:
            allocs = r.get("invoice_allocations") or "[]"
            if isinstance(allocs, str):
                allocs = json.loads(allocs) if allocs.strip() else []
        except Exception:
            allocs = []

        if not isinstance(allocs, list):
            allocs = []

        prev_sum = 0.0
        curr_sum = 0.0
        invoice_lines = []
        advance_lines = []
        payment_customer_id = customer_id

        used_invoice_ids = set()
        for a in allocs:
            btype = str((a or {}).get("bill_type") or (a or {}).get("billtype") or "").strip() or "Agst Ref"
            ref = str((a or {}).get("invoice_number") or (a or {}).get("invoice") or "").strip()
            amount = _safe_float((a or {}).get("amount"))
            drcr = str((a or {}).get("dr_cr") or (a or {}).get("drcr") or "").strip() or "Cr"

            if not ref:
                continue

            if drcr.lower() != "cr":
                _emit(f"Receipt {receipt_no}: unsupported Dr line '{ref}' ({btype}) amount={amount}")
                continue

            btype_norm = btype.lower()

            # 1) Advance / On Account -> ONLY these go to Customer Advance
            if "on account" in btype_norm or "advance" in btype_norm:
                advance_lines.append({"ref": ref, "amount": amount})
                continue

            # 2) Agst Ref or New Ref -> Match invoice by dynamic VCH number, prior date, and amount matching
            matched_inv, match_reason = _resolve_invoice(
                invoice_number=ref,
                cust_id=customer_id,
                cust_name=customer_name,
                amount=amount,
                narration=r.get("narration") or "",
                exclude_invoice_ids=used_invoice_ids,
                receipt_date=receipt_date_iso or r.get("date") or ""
            )

            if matched_inv:
                zoho_invoice_id = matched_inv.get("invoice_id")
                if not matched_inv.get("is_ob"):
                    used_invoice_ids.add(zoho_invoice_id)
                inv_cust_id = matched_inv.get("customer_id")
                if inv_cust_id:
                    payment_customer_id = inv_cust_id

                inv_bal = float(matched_inv.get("balance") or matched_inv.get("total") or amount)
                inv_no = matched_inv.get("invoice_number", "")

                _emit(f"Receipt {receipt_no}: [{match_reason}] Matched Invoice in Zoho '{inv_no}' (ID: {zoho_invoice_id}) for ref '{ref}' (₹{amount:,.2f})")
                curr_sum += amount
                invoice_lines.append({"invoice_id": zoho_invoice_id, "amount_applied": amount, "ref": (inv_no if matched_inv.get("is_ob") else ref), "bucket": "current"})
            else:
                # User Rule: If amount is matching with nothing, give the error message in the report immediately!
                err_msg = f"Amount mismatch or Invoice not found for customer '{customer_name}' (Ref: '{ref}', Amount: ₹{amount:,.2f}, Narration: '{r.get('narration', '')}')"
                _emit(f"Receipt {receipt_no}: {err_msg}")
                stats["failed"] += 1
                errors.append({
                    "receipt_number": receipt_no,
                    "date": receipt_date_iso,
                    "customer": customer_name,
                    "payment_mode": r.get("payment_mode") or "Bank Transfer",
                    "amount": amount,
                    "bank_account": r.get("bank_account") or "",
                    "reference_number": ref,
                    "description": r.get("narration") or "",
                    "Invoice Numbers": ref,
                    "Type": "Agst Ref (Amount/Invoice Mismatch)",
                    "error": err_msg
                })
                try:
                    database_manager.update_receipt_status(receipt_no, zoho_payment_id=None, zoho_status='failed', zoho_error=err_msg[:500])
                except Exception:
                    pass

        # Fallback if no invoice_lines and no advance_lines (e.g. empty invoice_allocations / On-Account payment)
        if not invoice_lines and not advance_lines:
            receipt_amt = _safe_float(r.get("customer_ledger_amount") or r.get("amount"))
            if receipt_amt > 0:
                ref_cand = (str(r.get("against_reference") or r.get("reference_number") or "")).strip()
                matched_inv, match_reason = _resolve_invoice(
                    invoice_number=ref_cand,
                    cust_id=customer_id,
                    cust_name=customer_name,
                    amount=receipt_amt,
                    narration=r.get("narration") or "",
                    exclude_invoice_ids=used_invoice_ids,
                    receipt_date=receipt_date_iso or r.get("date") or ""
                )
                if matched_inv:
                    zoho_invoice_id = matched_inv.get("invoice_id")
                    used_invoice_ids.add(zoho_invoice_id)
                    inv_cust_id = matched_inv.get("customer_id")
                    if inv_cust_id:
                        payment_customer_id = inv_cust_id
                    inv_no = matched_inv.get("invoice_number", "")
                    _emit(f"Receipt {receipt_no}: [{match_reason}] Matched Invoice in Zoho '{inv_no}' (ID: {zoho_invoice_id}) (₹{receipt_amt:,.2f})")
                    curr_sum += receipt_amt
                    invoice_lines.append({"invoice_id": zoho_invoice_id, "amount_applied": receipt_amt, "ref": ref_cand or receipt_no, "bucket": "current"})
                else:
                    _emit(f"Receipt {receipt_no}: Unallocated payment, creating On-Account Customer Payment for '{customer_name}' (₹{receipt_amt:,.2f})")
                    advance_lines.append({"ref": ref_cand or receipt_no or "On Account", "amount": receipt_amt})

        bank_account_name = (r.get("bank_account") or "").strip()
        account_id = _get_account_id(bank_account_name)

        # Build and submit ONE customer payment (accumulated)
        total_payment = prev_sum + curr_sum
        if total_payment > 0 and invoice_lines:
            ref_no = (r.get("reference_number") or "").strip()
            if not ref_no:
                distinct_refs = []
                for x in invoice_lines:
                    rf = str(x.get("ref", "")).strip()
                    if rf and rf not in distinct_refs:
                        distinct_refs.append(rf)
                if distinct_refs:
                    ref_no = f"AGST-{'/'.join(distinct_refs)}"[:100]
                else:
                    ref_no = receipt_no

            payload = {
                "customer_id": payment_customer_id,
                "payment_mode": "banktransfer" if (r.get("payment_mode") or "").lower().startswith("bank") else "cash",
                "amount": round(total_payment, 2),
                "date": receipt_date_iso or datetime.now().strftime("%Y-%m-%d"),
                "payment_number": receipt_no,
                "reference_number": str(ref_no or receipt_no)[:45],
                "description": (r.get("narration") or "").strip(),
                "invoices": [{"invoice_id": x["invoice_id"], "amount_applied": round(_safe_float(x["amount_applied"]), 2)} for x in invoice_lines],
            }
            if account_id:
                payload["account_id"] = account_id

            resp = zoho.api_call("POST", "/customerpayments", payload=payload)
            if resp.get("code") == 0:
                stats["payments_created"] += 1
                zoho_pid = (resp.get('payment', {}) or resp.get('customerpayment', {}) or {}).get('payment_id', '')
                if zoho_pid and receipt_no:
                    try:
                        zoho.api_call("PUT", f"/customerpayments/{zoho_pid}", payload={"payment_number": receipt_no, "reference_number": receipt_no})
                    except Exception:
                        pass
                _emit(f"[{idx}/{stats['total']}] Invoice Payment created: receipt {receipt_no} amount={round(total_payment,2)} -> Zoho ID: {zoho_pid}")
                try:
                    database_manager.update_receipt_status(receipt_no, zoho_payment_id=str(zoho_pid), zoho_status='synced', zoho_error=None)
                except Exception:
                    pass
            else:
                stats["failed"] += 1
                msg = resp.get("message") or "Zoho error"
                inv_nums = ", ".join([x.get("ref", "") for x in invoice_lines])
                errors.append({"Receipt Number": receipt_no, "Customer": customer_name, "Invoice Numbers": inv_nums, "Type": "Invoice Payment", "Error Message": msg})
                _emit(f"[{idx}/{stats['total']}] Payment failed: receipt {receipt_no} ({msg})")
                try:
                    database_manager.update_receipt_status(receipt_no, zoho_payment_id=None, zoho_status='failed', zoho_error=msg[:500])
                except Exception:
                    pass

        # Submit Customer Advances per line
        for adv in advance_lines:
            if _should_stop(stop_event):
                _emit("Stopped by user.")
                break
            payload = {
                "customer_id": payment_customer_id,
                "payment_mode": "banktransfer" if (r.get("payment_mode") or "").lower().startswith("bank") else "cash",
                "amount": round(_safe_float(adv.get("amount")), 2),
                "date": receipt_date_iso or datetime.now().strftime("%Y-%m-%d"),
                "payment_number": receipt_no,
                "reference_number": str(adv.get("ref") or receipt_no)[:100],
                "description": (r.get("narration") or "").strip(),
            }
            if account_id:
                payload["account_id"] = account_id

            resp = zoho.api_call("POST", "/customerpayments", payload=payload)
            if resp.get("code") == 0:
                stats["advances_created"] += 1
                zoho_pid = (resp.get("payment", {}) or resp.get("customerpayment", {}) or {}).get("payment_id", "") or "ADV_CREATED"
                if zoho_pid and receipt_no and zoho_pid != "ADV_CREATED":
                    try:
                        zoho.api_call("PUT", f"/customerpayments/{zoho_pid}", payload={"payment_number": receipt_no, "reference_number": receipt_no})
                    except Exception:
                        pass
                _emit(f"[{idx}/{stats['total']}] Advance created: receipt {receipt_no} ref='{adv.get('ref')}' amount={payload['amount']} -> Zoho ID: {zoho_pid}")
                try:
                    database_manager.update_receipt_status(receipt_no, zoho_payment_id=str(zoho_pid), zoho_status='synced', zoho_error=None)
                except Exception:
                    pass
            else:
                stats["failed"] += 1
                msg = resp.get("message") or "Zoho error"
                errors.append({"Receipt Number": receipt_no, "Customer": customer_name, "Invoice Numbers": adv.get("ref"), "Type": "Customer Advance", "Error Message": msg})
                _emit(f"[{idx}/{stats['total']}] Advance failed: receipt {receipt_no} ref='{adv.get('ref')}' ({msg})")
                try:
                    database_manager.update_receipt_status(receipt_no, zoho_payment_id=None, zoho_status='failed', zoho_error=msg[:500])
                except Exception:
                    pass

        if idx % 25 == 0 or idx == 1 or idx == stats["total"]:
            _emit(f"Progress: {idx}/{stats['total']} payments={stats['payments_created']} advances={stats['advances_created']} failed={stats['failed']}")

    if errors:
        try:
            import pandas as pd
            import os
            df = pd.DataFrame(errors)
            filename = f"Receipts_Received_Errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            filepath = os.path.join(os.getcwd(), filename)
            df.to_excel(filepath, index=False)
            _emit(f"Errors exported to Excel file: {filename}")
        except Exception as e:
            _emit(f"Failed to export errors to Excel: {e}")

    return {"status": "success", "stats": stats, "errors": errors}

