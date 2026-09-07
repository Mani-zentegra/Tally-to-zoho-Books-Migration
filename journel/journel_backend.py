
import requests
import json
import os
import sys
import re
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from collections import defaultdict

# Add parent directory to path so we can import shared database_manager
parent_dir = Path(__file__).parent.parent
sys.path.append(str(parent_dir))

try:
    import database_manager
except ImportError:
    print("️ Warning: Could not import database_manager. SQLite sync will be skipped.")
    database_manager = None

# Load credentials from project root .env (not from inside /journel folder)
_env_path = Path(__file__).parent.parent / ".env"

def _reload_env():
    """Re-read .env from disk every call so switching company in .env takes effect immediately."""
    load_dotenv(dotenv_path=_env_path, override=True)

def _get_creds():
    """Return fresh credentials for active company on every call — never stale."""
    try:
        from modules.zoho_connector import get_thread_company
        from modules import company_manager
        active_comp = get_thread_company()
        if not active_comp:
            try:
                from flask import session, has_request_context
                if has_request_context():
                    cid = session.get("active_company_id")
                    if cid:
                        active_comp = company_manager.get_active_company(cid)
            except Exception:
                pass
        if not active_comp:
            active_comp = company_manager.get_active_company()

        if active_comp and active_comp.get("client_id") and active_comp.get("org_id"):
            zoho_dc = (active_comp.get("zoho_dc") or "in").strip().lower()
            accounts_domain = "accounts.zoho.in" if zoho_dc == "in" else "accounts.zoho.com"
            api_domain = "www.zohoapis.in" if zoho_dc == "in" else "www.zohoapis.com"
            return {
                "client_id":      active_comp.get("client_id", "").strip(),
                "client_secret":  active_comp.get("client_secret", "").strip(),
                "refresh_token":  active_comp.get("refresh_token", "").strip(),
                "access_token":   active_comp.get("access_token", "").strip(),
                "org_id":         active_comp.get("org_id", "").strip(),
                "accounts_domain": accounts_domain,
                "api_domain":     api_domain,
                "base_url":       f"https://{api_domain}/books/v3",
            }
    except Exception:
        pass

    _reload_env()
    zoho_dc = os.getenv("ZOHO_DC", "in").strip().lower()
    accounts_domain = "accounts.zoho.in" if zoho_dc == "in" else "accounts.zoho.com"
    api_domain = "www.zohoapis.in" if zoho_dc == "in" else "www.zohoapis.com"
    return {
        "client_id":      os.getenv("CLIENT_ID", "").strip(),
        "client_secret":  os.getenv("CLIENT_SECRET", "").strip(),
        "refresh_token":  os.getenv("REFRESH_TOKEN", "").strip(),
        "access_token":   os.getenv("ACCESS_TOKEN", "").strip(),
        "org_id":         os.getenv("ORGANIZATION_ID", "").strip(),
        "accounts_domain": accounts_domain,
        "api_domain":     api_domain,
        "base_url":       f"https://{api_domain}/books/v3",
    }

# Legacy module-level names kept for backward compat — read dynamically
def _org_id():   return _get_creds()["org_id"]
def _base_url(): return _get_creds()["base_url"]

# Aliases used widely in this file (re-evaluated per call via property-style functions)
ORGANIZATION_ID = property(_org_id)   # not a real descriptor — just for greppability

# URLs (kept for TALLY only — Zoho URLs built dynamically)
TALLY_URL = "http://localhost:9000"
_ACCESS_TOKEN_CACHE = {"token": None, "expires_at": 0, "org_id": None}

def get_access_token(force_refresh=False):
    """Get Zoho Books access token.
    Logic:
    1. Check SQLite zoho_tokens table (or memory cache). If current_time < expiry_time: return cached access_token.
    2. Else (expired/missing): generate new access_token via OAuth API and update the record in zoho_tokens DB table.
    """
    import time
    global _ACCESS_TOKEN_CACHE
    creds = _get_creds()
    org_id = creds["org_id"]
    now = time.time()

    # 1. Check DB cache if not force_refresh
    if not force_refresh:
        if database_manager and hasattr(database_manager, 'get_zoho_token_from_db'):
            db_token = database_manager.get_zoho_token_from_db(org_id=org_id)
            if db_token:
                return db_token

        if _ACCESS_TOKEN_CACHE["token"] and _ACCESS_TOKEN_CACHE["expires_at"] > now:
            if _ACCESS_TOKEN_CACHE["org_id"] == org_id:
                return _ACCESS_TOKEN_CACHE["token"]

    # 2. Generate fresh token via OAuth refresh_token API
    if creds["client_id"] and creds["client_secret"] and creds["refresh_token"]:
        payload = {
            "refresh_token": creds["refresh_token"],
            "client_id":     creds["client_id"],
            "client_secret": creds["client_secret"],
            "grant_type":    "refresh_token"
        }
        try:
            res = requests.post(f"https://{creds['accounts_domain']}/oauth/v2/token", data=payload, timeout=15)
            data = res.json()
            token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            if token:
                cache_ttl = max(expires_in - 300, 300)
                _ACCESS_TOKEN_CACHE = {
                    "token": token,
                    "expires_at": now + cache_ttl,
                    "org_id": org_id
                }
                # 3. Update record in zoho_tokens DB table with new access_token & expiry_time
                if database_manager and hasattr(database_manager, 'save_zoho_token_to_db'):
                    database_manager.save_zoho_token_to_db(token, expires_in_seconds=expires_in, org_id=org_id, refresh_token=creds["refresh_token"])
                print(f"    Token refreshed & updated in DB zoho_tokens table (valid for {expires_in // 60}m, org: {org_id})")
                return token
            else:
                print(f"    Token refresh failed: {data}")
        except Exception as e:
            print(f"    Token refresh error: {e}")

    # Fallback: use static ACCESS_TOKEN from .env (may be expired)
    if creds["access_token"]:
        print("    WARNING: Using static ACCESS_TOKEN from .env — may be expired!")
        return creds["access_token"]

    print(" No valid Zoho credentials found (CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN)")
    return None

# ----------------------------------------------------------
# SQLITE CACHING FOR PERFORMANCE
# ----------------------------------------------------------

import sqlite3
from pathlib import Path

# Database file location
DB_FILE = Path(__file__).parent / "tally_cache.db"

def init_cache_db():
    """Initialize SQLite cache database"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Tally data tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            name TEXT PRIMARY KEY,
            parent TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ledgers (
            name TEXT PRIMARY KEY,
            ledger_type TEXT
        )
    ''')
    
    # Zoho Books data tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS zoho_contacts (
            contact_id TEXT PRIMARY KEY,
            contact_name TEXT,
            contact_name_lower TEXT,
            contact_type TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS zoho_accounts (
            account_id TEXT PRIMARY KEY,
            account_name TEXT,
            account_name_lower TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create indexes for faster lookups
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_contact_name_lower ON zoho_contacts(contact_name_lower)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_account_name_lower ON zoho_accounts(account_name_lower)')
    
    conn.commit()
    conn.close()

def get_ledger_map_from_cache():
    """Get ledger map from SQLite cache"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name, ledger_type FROM ledgers")
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            ledger_map = {name: ledger_type for name, ledger_type in rows}
            print(f"    Loaded {len(ledger_map)} ledgers from cache")
            return ledger_map
        return None
    except:
        return None

def save_ledger_map_to_cache(ledger_map, groups_dict):
    """Save ledger map and groups to SQLite cache"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Clear existing data
        cursor.execute("DELETE FROM ledgers")
        cursor.execute("DELETE FROM groups")
        
        # Save groups
        for group_name, parent in groups_dict.items():
            cursor.execute("INSERT OR REPLACE INTO groups (name, parent) VALUES (?, ?)", 
                         (group_name, parent))
        
        # Save ledgers
        for ledger_name, ledger_type in ledger_map.items():
            cursor.execute("INSERT OR REPLACE INTO ledgers (name, ledger_type) VALUES (?, ?)", 
                         (ledger_name, ledger_type))
        
        # Update metadata
        cursor.execute("INSERT OR REPLACE INTO cache_metadata (key, value) VALUES (?, ?)",
                     ("last_updated", datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        print(f"    Cached {len(ledger_map)} ledgers and {len(groups_dict)} groups to database")
    except Exception as e:
        print(f"   ️  Failed to cache data: {e}")

# ----------------------------------------------------------
# ZOHO BOOKS CACHING
# ----------------------------------------------------------

def save_zoho_contacts_to_cache(contact_map):
    """Save Zoho contacts to SQLite cache (org-aware) with full state & GST details"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS zoho_contacts (
                contact_id TEXT PRIMARY KEY,
                contact_name TEXT,
                contact_name_lower TEXT,
                contact_type TEXT,
                place_of_contact TEXT,
                gst_no TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("ALTER TABLE zoho_contacts ADD COLUMN place_of_contact TEXT")
        except Exception: pass
        try:
            cursor.execute("ALTER TABLE zoho_contacts ADD COLUMN gst_no TEXT")
        except Exception: pass

        # Clear existing contacts
        cursor.execute("DELETE FROM zoho_contacts")
        
        # Save contacts
        for contact_name_lower, contact_info in contact_map.items():
            cursor.execute("""
                INSERT OR REPLACE INTO zoho_contacts 
                (contact_id, contact_name, contact_name_lower, contact_type, place_of_contact, gst_no) 
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                contact_info["contact_id"],
                contact_info["original_name"],
                contact_name_lower,
                contact_info["contact_type"],
                contact_info.get("place_of_contact", ""),
                contact_info.get("gst_no", "")
            ))
        
        # Update metadata — store org_id so we detect org switches
        cursor.execute("CREATE TABLE IF NOT EXISTS cache_metadata (key TEXT PRIMARY KEY, value TEXT)")
        cursor.execute("INSERT OR REPLACE INTO cache_metadata (key, value) VALUES (?, ?)",
                     ("zoho_contacts_updated", datetime.now().isoformat()))
        cursor.execute("INSERT OR REPLACE INTO cache_metadata (key, value) VALUES (?, ?)",
                     ("zoho_contacts_org_id", _get_creds()["org_id"]))
        
        conn.commit()
        conn.close()
        print(f"    Cached {len(contact_map)} Zoho contacts to database (org: {_get_creds()['org_id']})")

        # Also save to main active database (so zoho_contacts & zoho_masters_cache show up in DB Browser)
        if database_manager:
            try:
                main_conn = database_manager.get_db_connection(write=True)
                main_cursor = main_conn.cursor()
                main_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS zoho_contacts (
                        contact_id TEXT PRIMARY KEY,
                        contact_name TEXT,
                        contact_name_lower TEXT,
                        contact_type TEXT,
                        place_of_contact TEXT,
                        gst_no TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                try:
                    main_cursor.execute("ALTER TABLE zoho_contacts ADD COLUMN place_of_contact TEXT")
                except Exception: pass
                try:
                    main_cursor.execute("ALTER TABLE zoho_contacts ADD COLUMN gst_no TEXT")
                except Exception: pass

                main_cursor.execute("DELETE FROM zoho_contacts")
                for contact_name_lower, contact_info in contact_map.items():
                    main_cursor.execute("""
                        INSERT OR REPLACE INTO zoho_contacts 
                        (contact_id, contact_name, contact_name_lower, contact_type, place_of_contact, gst_no) 
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        contact_info["contact_id"],
                        contact_info["original_name"],
                        contact_name_lower,
                        contact_info["contact_type"],
                        contact_info.get("place_of_contact", ""),
                        contact_info.get("gst_no", "")
                    ))
                if hasattr(database_manager, 'save_zoho_master_cache'):
                    database_manager.save_zoho_master_cache('contacts', contact_map, org_id=_get_creds()["org_id"])
            except Exception as m_err:
                print(f"    Warning: Could not save contacts to main DB: {m_err}")
    except Exception as e:
        print(f"   ️  Failed to cache Zoho contacts: {e}")

def get_zoho_contacts_from_cache():
    """Get Zoho contacts from SQLite cache — returns None if org has changed"""
    try:
        # First try main database_manager master cache
        org_id = _get_creds()["org_id"]
        if database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
            cached_main = database_manager.get_zoho_master_cache('contacts', expected_org_id=org_id)
            if cached_main:
                print(f"    Loaded {len(cached_main)} Zoho contacts from main DB master cache (org: {org_id})")
                return cached_main

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Check if cache belongs to the current organization
        cursor.execute("SELECT value FROM cache_metadata WHERE key = 'zoho_contacts_org_id'")
        row = cursor.fetchone()
        cached_org_id = row[0] if row else None
        
        if cached_org_id != org_id:
            conn.close()
            print(f"    Organization changed ({cached_org_id} → {org_id}) — clearing contacts cache")
            return None  # Force fresh fetch
        
        try:
            cursor.execute("SELECT contact_id, contact_name, contact_name_lower, contact_type, place_of_contact, gst_no FROM zoho_contacts")
            rows = cursor.fetchall()
        except Exception:
            cursor.execute("SELECT contact_id, contact_name, contact_name_lower, contact_type FROM zoho_contacts")
            rows = [r + ('', '') for r in cursor.fetchall()]

        conn.close()
        
        if rows:
            contact_map = {}
            for r in rows:
                c_id, c_name, c_lower, c_type = r[0], r[1], r[2], r[3]
                poc = r[4] if len(r) > 4 else ""
                gst = r[5] if len(r) > 5 else ""
                
                # If contact already exists in map, prefer vendor contact over customer
                if c_lower in contact_map:
                    if contact_map[c_lower].get("contact_type") == "vendor" and c_type != "vendor":
                        continue
                
                contact_map[c_lower] = {
                    "contact_id": c_id,
                    "contact_name": c_name,
                    "original_name": c_name,
                    "contact_type": c_type,
                    "place_of_contact": poc,
                    "gst_no": gst
                }
            print(f"    Loaded {len(contact_map)} Zoho contacts from local cache (org: {org_id})")
            return contact_map
        return None
    except Exception as e:
        print(f"    Error reading contacts from cache: {e}")
        return None

def save_zoho_accounts_to_cache(account_map):
    """Save Zoho accounts to SQLite cache (org-aware)"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Clear existing accounts
        cursor.execute("DELETE FROM zoho_accounts")
        
        # Save accounts (account_map is {name_lower: account_id})
        for account_name_lower, account_id in account_map.items():
            cursor.execute("""
                INSERT OR REPLACE INTO zoho_accounts 
                (account_id, account_name_lower) 
                VALUES (?, ?)
            """, (account_id, account_name_lower))
        
        # Store org_id so we detect org switches
        cursor.execute("INSERT OR REPLACE INTO cache_metadata (key, value) VALUES (?, ?)",
                     ("zoho_accounts_updated", datetime.now().isoformat()))
        cursor.execute("INSERT OR REPLACE INTO cache_metadata (key, value) VALUES (?, ?)",
                     ("zoho_accounts_org_id", _get_creds()["org_id"]))
        
        conn.commit()
        conn.close()
        print(f"    Cached {len(account_map)} Zoho accounts to database")
    except Exception as e:
        print(f"   ️  Failed to cache Zoho accounts: {e}")

def get_zoho_accounts_from_cache():
    """Get Zoho accounts from SQLite cache — returns None if org has changed"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Check if cache belongs to the current organization
        cursor.execute("SELECT value FROM cache_metadata WHERE key = 'zoho_accounts_org_id'")
        row = cursor.fetchone()
        cached_org_id = row[0] if row else None
        
        if cached_org_id != _get_creds()["org_id"]:
            conn.close()
            print(f"    Organization changed ({cached_org_id} → {_get_creds()['org_id']}) — clearing accounts cache")
            return None  # Force fresh fetch
        
        cursor.execute("SELECT account_id, account_name_lower FROM zoho_accounts")
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            account_map = {account_name_lower: account_id for account_id, account_name_lower in rows}
            print(f"    Loaded {len(account_map)} Zoho accounts from cache (org: {_get_creds()['org_id']})")
            return account_map
        return None
    except:
        return None


def get_ledger_map_from_tally(use_cache=True, force_refresh=False):
    """
    FULLY DYNAMIC: Builds ledger map by analyzing Tally's group hierarchy.
    Now with SQLite caching for performance!
    
    Args:
        use_cache: If True, try to load from cache first
        force_refresh: If True, ignore cache and fetch fresh from Tally
    """
    # Initialize database
    init_cache_db()
    
    # Try cache first (unless force refresh)
    if use_cache and not force_refresh:
        cached_map = get_ledger_map_from_cache()
        if cached_map:
            return cached_map
    
    print("\n Building DYNAMIC ledger map from Tally...")
    
    # Step 1: Fetch all Groups to build the hierarchy
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
            if name:
                children_map[parent].append(name)
        print(f"    Found {len(children_map)} group relationships")
    except Exception as e:
        print(f"   ️  Could not fetch Tally groups: {e}")
        return {}

    # Step 2: Recursive function to find ALL descendants of a group
    def get_all_descendants(group_name, visited=None):
        if visited is None:
            visited = set()
        if group_name in visited:
            return set()
        visited.add(group_name)
        results = {group_name}
        for child in children_map.get(group_name, []):
            results.update(get_all_descendants(child, visited))
        return results

    # Step 3: Identify all vendor and customer groups
    vendor_groups = get_all_descendants("Sundry Creditors")
    customer_groups = get_all_descendants("Sundry Debtors")
    
    print(f"    Vendor groups (under Sundry Creditors): {len(vendor_groups)}")
    print(f"    Customer groups (under Sundry Debtors): {len(customer_groups)}")
    
    # Show some examples
    if vendor_groups:
        examples = list(vendor_groups)[:5]
        print(f"      Vendor examples: {', '.join(examples)}")
    if customer_groups:
        examples = list(customer_groups)[:5]
        print(f"      Customer examples: {', '.join(examples)}")

    # Step 4: Fetch ALL Ledgers using the correct XML format
    # Use the SAME format as Tally_journel.py which works
    ledger_xml = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Accounts</REPORTNAME>
    <STATICVARIABLES><ACCOUNTTYPE>Ledgers</ACCOUNTTYPE><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    </REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    
    ledger_map = {}
    try:
        print(f"    Fetching ledgers from Tally (this may take a minute)...")
        res = requests.post(TALLY_URL, data=ledger_xml, timeout=60)  # Increased timeout to 60s
        soup = BeautifulSoup(res.content, 'lxml-xml')
        
        # Debug: Check what we got
        all_ledgers = soup.find_all('LEDGER')
        print(f"    Raw ledger count from Tally: {len(all_ledgers)}")
        
        vendor_count = 0
        customer_count = 0
        account_count = 0
        
        for ledger in all_ledgers:
            name = ledger.get('NAME', '').strip()
            if not name:
                continue
                
            parent = ledger.find('PARENT')
            parent_name = parent.text.strip() if parent and parent.text else ""
            
            # Debug first few ledgers
            if len(ledger_map) < 5:
                print(f"      Debug: Ledger '{name}' -> Parent '{parent_name}'")
            
            if parent_name in vendor_groups:
                ledger_map[name] = "vendor"
                vendor_count += 1
            elif parent_name in customer_groups:
                ledger_map[name] = "customer"
                customer_count += 1
            else:
                ledger_map[name] = "account"
                account_count += 1
        
        print(f"    Classified {len(ledger_map)} ledgers:")
        print(f"      - Vendors: {vendor_count}")
        print(f"      - Customers: {customer_count}")
        print(f"      - Accounts: {account_count}")
        
        # Show some vendor/customer examples
        if vendor_count > 0:
            vendor_examples = [name for name, type in list(ledger_map.items())[:20] if type == "vendor"][:3]
            if vendor_examples:
                print(f"      Vendor ledger examples: {', '.join(vendor_examples)}")
        
        if customer_count > 0:
            customer_examples = [name for name, type in list(ledger_map.items())[:20] if type == "customer"][:3]
            if customer_examples:
                print(f"      Customer ledger examples: {', '.join(customer_examples)}")
        
    except requests.exceptions.Timeout:
        print(f"   ️  Tally ledger fetch timed out after 60s")
        print(f"    Your Tally database may have many ledgers. Continuing with empty map...")
    except Exception as e:
        print(f"   ️  Could not fetch Tally ledgers: {e}")
        import traceback
        traceback.print_exc()
    
    # Save to cache for next time
    if ledger_map:
        # Build groups dict for caching
        groups_dict = {}
        for parent, children in children_map.items():
            for child in children:
                groups_dict[child] = parent
        save_ledger_map_to_cache(ledger_map, groups_dict)
    
    return ledger_map

def fetch_tally_journals(from_date="20250401", to_date="20250430", limit=None):
    """Fetch journal vouchers from Tally with DYNAMIC ledger classification"""
    ledger_map = get_ledger_map_from_tally()
    
    def get_ledger_type_fuzzy(ledger_name):
        """
        Get ledger type with fuzzy matching to handle name variations.
        Tally sometimes returns different names in journals vs ledger list.
        E.g., 'MATAJI ELECTRICAL & LIGHT HOUSE' becomes 'MATAJI ELECTRICAL  LIGHT HOUSE'
        """
        # Try exact match first
        if ledger_name in ledger_map:
            return ledger_map[ledger_name]
        
        # Normalize the name for fuzzy matching
        # Remove '&', replace multiple spaces with single space
        def normalize_name(name):
            return ' '.join(name.replace('&', '').split())
        
        normalized_search = normalize_name(ledger_name).lower()
        
        # Try fuzzy match
        for map_name, ledger_type in ledger_map.items():
            normalized_map = normalize_name(map_name).lower()
            if normalized_search == normalized_map:
                # Found a match!
                if ledger_type != "account":
                    print(f"       Fuzzy matched '{ledger_name}' -> '{map_name}' ({ledger_type})")
                return ledger_type
        
        # Default to account
        return "account"
    
    xml_request = f"""<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
    <BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
    <SVFROMDATE>{from_date}</SVFROMDATE><SVTODATE>{to_date}</SVTODATE>
    </STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""

    try:
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
        
        journal_data = []
        
        for v in vouchers:
            v_date = v.find('DATE').text if v.find('DATE') else ""
            v_no = v.find('VOUCHERNUMBER').text if v.find('VOUCHERNUMBER') else ""
            narration = v.find('NARRATION').text if v.find('NARRATION') else ""
            
            line_items = []
            for entry in v.find_all('LEDGERENTRIES.LIST') or v.find_all('ALLLEDGERENTRIES.LIST'):
                name = entry.find('LEDGERNAME').text.strip()
                
                # FIX: Handle currency conversion strings
                amt_text = entry.find('AMOUNT').text if entry.find('AMOUNT') else "0"
                try:
                    # Try direct conversion first
                    amt = float(amt_text)
                except ValueError:
                    # Handle currency conversion format: '-$1116.86 @ ? 88.1409/$ = -? 98441.05'
                    # Extract the final amount after '='
                    import re
                    if '=' in amt_text:
                        # Get the amount after '='
                        final_part = amt_text.split('=')[-1].strip()
                        # Extract number (remove currency symbols)
                        match = re.search(r'-?\d+\.?\d*', final_part.replace('?', '').replace(',', ''))
                        amt = float(match.group()) if match else 0.0
                    else:
                        # Just extract first number found
                        match = re.search(r'-?\d+\.?\d*', amt_text.replace(',', ''))
                        amt = float(match.group()) if match else 0.0
                
                # Use fuzzy matching to get ledger type
                l_type = get_ledger_type_fuzzy(name)
                
                # Get reporting tags
                tag_category = ""
                tag_option = ""
                cat_alloc = entry.find('CATEGORYALLOCATIONS.LIST')
                if cat_alloc:
                    tag_category = cat_alloc.find('CATEGORY').text if cat_alloc.find('CATEGORY') else ""
                    cc_list = cat_alloc.find('COSTCENTREALLOCATIONS.LIST')
                    if cc_list:
                        tag_option = cc_list.find('NAME').text if cc_list.find('NAME') else ""
                
                line_items.append({
                    "ledger_name": name,
                    "ledger_type": l_type,
                    "amount": abs(amt),
                    "debit_or_credit": "debit" if amt < 0 else "credit",
                    "tag_category": tag_category,
                    "tag_option": tag_option
                })
            
            journal_data.append({
                "date": v_date,
                "journal_number": v_no,
                "narration": narration,
                "line_items": line_items
            })
        
        return journal_data
    
    except Exception as e:
        print(f" Error processing Tally journals: {e}")
        raise e

def find_tag_ids_by_name(token, target_tag_name, target_option_name):
    """Find reporting tag IDs by name"""
    if not target_tag_name or not target_option_name:
        return None, None
        
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    try:
        res = requests.get(f"{creds['base_url']}/settings/tags", headers=headers, params=params)
        categories = res.json().get("reporting_tags", [])

        tag_id = next((cat.get("tag_id") for cat in categories if cat.get("tag_name", "").strip().lower() == target_tag_name.lower()), None)
        if not tag_id:
            return None, None

        detail_res = requests.get(f"{creds['base_url']}/settings/tags/{tag_id}", headers=headers, params=params)
        detail_data = detail_res.json()
        tag_obj = detail_data.get("tag", detail_data.get("reporting_tag", {}))
        options = tag_obj.get("tag_options", [])

        tag_option_id = next((opt.get("tag_option_id") for opt in options if opt.get("tag_option_name", "").strip().lower() == target_option_name.lower()), None)
        return tag_id, tag_option_id
    except:
        return None, None

def get_zoho_accounts(token, use_cache=True, force_refresh=False):
    """
    Fetch all Zoho Books chart of accounts with caching
    
    Args:
        token: Zoho OAuth token
        use_cache: If True, try to load from cache first
        force_refresh: If True, ignore cache and fetch fresh from Zoho
    """
    creds = _get_creds()
    
    # Try cache first (unless force refresh)
    if use_cache and not force_refresh:
        cached_accounts = get_zoho_accounts_from_cache()
        if cached_accounts:
            return cached_accounts
    
    print("    Fetching accounts from Zoho Books...")
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    
    account_map = {}
    page = 1
    while True:
        params = {
            "organization_id": creds["org_id"],
            "per_page": 200,
            "page": page
        }
        res = requests.get(f"{creds['base_url']}/chartofaccounts", headers=headers, params=params)
        print(f"    [DEBUG] COA API status: {res.status_code}")
        data = res.json()
        accounts = data.get("chartofaccounts", [])
        if not accounts:
            print(f"    [DEBUG] COA API response: {str(data)[:500]}")
            break
        for acc in accounts:
            account_map[acc["account_name"].lower().strip()] = acc["account_id"]
        
        page_context = data.get("page_context", {})
        if not page_context.get("has_more_page", False):
            break
        page += 1
    
    print(f"    Fetched {len(account_map)} accounts across {page} page(s)")
    
    # Save to cache
    save_zoho_accounts_to_cache(account_map)
    
    return account_map

def get_zoho_contacts(token, use_cache=True, force_refresh=False):
    """
    Fetch all Zoho Books contacts with pagination and caching
    
    Args:
        token: Zoho OAuth token
        use_cache: If True, try to load from cache first
        force_refresh: If True, ignore cache and fetch fresh from Zoho
    """
    creds = _get_creds()
    
    # Try cache first (unless force refresh)
    if use_cache and not force_refresh:
        cached_contacts = get_zoho_contacts_from_cache()
        if cached_contacts:
            return cached_contacts
    
    print("    Fetching contacts from Zoho Books...")
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"], "per_page": 200}
    
    contact_map = {}
    page = 1
    total_contacts = 0
    
    while True:
        params["page"] = page
        res = requests.get(f"{creds['base_url']}/contacts", headers=headers, params=params)
        data = res.json()
        
        contacts = data.get("contacts", [])
        if not contacts:
            break
        
        for contact in contacts:
            contact_name = contact["contact_name"].lower().strip()
            contact_map[contact_name] = {
                "contact_id": contact["contact_id"],
                "contact_type": contact["contact_type"],
                "original_name": contact["contact_name"],
                "place_of_contact": contact.get("place_of_contact", ""),
                "place_of_contact_formatted": contact.get("place_of_contact_formatted", ""),
                "gst_no": contact.get("gst_no", ""),
                "gst_treatment": contact.get("gst_treatment", "")
            }
            total_contacts += 1
        
        page_context = data.get("page_context", {})
        if not page_context.get("has_more_page", False):
            break
        
        page += 1
    
    print(f"    Fetched {total_contacts} contacts across {page} page(s)")
    
    # Save to cache
    save_zoho_contacts_to_cache(contact_map)
    
    return contact_map

def create_contact_in_zoho(token, contact_name, contact_type):
    """
    AUTOMATIC CONTACT CREATION
    Creates a new contact (vendor or customer) in Zoho Books
    """
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    payload = {
        "contact_name": contact_name,
        "contact_type": contact_type,  # "vendor" or "customer"
        "company_name": contact_name
    }
    
    try:
        res = requests.post(f"{creds['base_url']}/contacts", headers=headers, params=params, json=payload)
        if res.status_code in [200, 201] and res.json().get("code") == 0:
            contact_data = res.json().get("contact", {})
            contact_id = contact_data.get("contact_id")
            print(f"      Created new {contact_type}: {contact_name} (ID: {contact_id})")
            created_info = {
                "contact_id": contact_id,
                "contact_type": contact_type,
                "original_name": contact_name
            }
            # Save newly created contact into SQLite DB zoho_contacts table
            try:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS zoho_contacts (
                        contact_id TEXT PRIMARY KEY, contact_name TEXT, contact_name_lower TEXT,
                        contact_type TEXT, place_of_contact TEXT, gst_no TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("""
                    INSERT OR REPLACE INTO zoho_contacts (contact_id, contact_name, contact_name_lower, contact_type)
                    VALUES (?, ?, ?, ?)
                """, (contact_id, contact_name, contact_name.lower(), contact_type))
                conn.commit()
                conn.close()
            except Exception as db_e:
                print(f"      Warning: Failed to store new contact in DB: {db_e}")
            return created_info
        else:
            print(f"     ️  Failed to create contact: {res.json()}")
            return None
    except Exception as e:
        print(f"     ️  Error creating contact: {e}")
        return None

def find_or_create_contact(token, contact_map, contact_name, contact_type, auto_create=False):
    """
    Find contact in Zoho Books by exact, clean, or normalized matching.
    Strips prefixes ('Ship to:', 'Bill to:', etc.) and honorifics ('M/s', etc.).
    Punctuation-insensitive matching (e.g. 'R. K' vs 'R.K').
    """
    if not contact_name:
        return None

    PREFIX_PATTERN = r'^(ship\s*to\s*:|bill\s*to\s*:|consignee\s*:|c/o\s*:?)\s*'
    MS_PATTERN = r'^(m/s\.?|messrs\.?)\s*'

    raw_name = str(contact_name).strip()

    def _has_prefix(name):
        return bool(re.match(PREFIX_PATTERN, name, flags=re.IGNORECASE))

    def _strict_clean(name):
        s = re.sub(PREFIX_PATTERN, '', str(name), flags=re.IGNORECASE).strip()
        s = re.sub(MS_PATTERN, '', s, flags=re.IGNORECASE).strip()
        return re.sub(r'[^\w]', '', s).lower()

    # 1. Build a normalized lookup map of non-prefixed Zoho contacts (prioritizing matching contact_type)
    norm_map = {}
    for c_key, c_info in contact_map.items():
        orig_name = c_info.get('original_name', c_key)
        if _has_prefix(orig_name):
            continue  # Skip dirty prefixed contacts in Zoho
        sc = _strict_clean(orig_name)
        if not sc:
            continue
        c_type = str(c_info.get("contact_type", "")).lower()
        if sc not in norm_map:
            norm_map[sc] = c_info
        elif contact_type == 'customer' and ('customer' in c_type or c_type != 'vendor'):
            norm_map[sc] = c_info  # Overwrite vendor contact with customer contact

    # 2. Strict clean match
    raw_sc = _strict_clean(raw_name)
    if raw_sc in norm_map:
        matched = norm_map[raw_sc]
        m_type = str(matched.get("contact_type", "")).lower()
        if not (contact_type == 'customer' and m_type == 'vendor'):
            print(f"      Matched '{contact_name}' to '{matched.get('original_name')}'")
            return matched

    # 3. Try exact clean lower match
    clean_name = re.sub(PREFIX_PATTERN, '', raw_name, flags=re.IGNORECASE).strip()
    clean_lower = clean_name.lower()
    if clean_lower in contact_map and not _has_prefix(clean_lower):
        matched = contact_map[clean_lower]
        m_type = str(matched.get("contact_type", "")).lower()
        if not (contact_type == 'customer' and m_type == 'vendor'):
            return matched

    # 4. Fuzzy containment search on strict clean key (excluding vendor contacts when looking for customers)
    for sc_key, c_info in norm_map.items():
        if len(sc_key) >= 4 and (sc_key in raw_sc or raw_sc in sc_key):
            c_type = str(c_info.get("contact_type", "")).lower()
            if contact_type == 'customer' and c_type == 'vendor':
                continue  # Skip vendor contacts when searching for customers
            display_name = c_info.get('original_name') or c_info.get('contact_name', sc_key)
            print(f"      Fuzzy matched '{contact_name}' to '{display_name}'")
            return c_info

    # 5. Live Search against Zoho Books API if missing from local cache
    try:
        from modules.zoho_connector import zoho
        search_res = zoho.api_call("GET", "/contacts", params={"search_text": clean_name})
        if search_res.get("code") == 0:
            api_contacts = search_res.get("contacts", [])
            for ac in api_contacts:
                ac_name = (ac.get("contact_name") or "").strip().lower()
                if ac_name == clean_lower or clean_lower in ac_name or ac_name in clean_lower:
                    c_obj = {
                        "contact_id": ac.get("contact_id"),
                        "contact_type": ac.get("contact_type"),
                        "original_name": ac.get("contact_name"),
                        "place_of_contact": ac.get("place_of_contact", ""),
                        "place_of_contact_formatted": ac.get("place_of_contact_formatted", ""),
                        "gst_no": ac.get("gst_no", ""),
                        "gst_treatment": ac.get("gst_treatment", "")
                    }
                    contact_map[ac_name] = c_obj
                    contact_map[clean_lower] = c_obj
                    print(f"      Live matched '{contact_name}' -> '{ac.get('contact_name')}' (ID: {ac.get('contact_id')}) via Zoho API")
                    return c_obj
    except Exception as live_err:
        print(f"      Live contact search error: {live_err}")

    # 6. Automatic creation disabled unless explicitly requested
    if auto_create:
        print(f"      Contact '{clean_name}' not found - creating new {contact_type}...")
        new_contact = create_contact_in_zoho(token, clean_name, contact_type)
        if new_contact:
            contact_map[clean_lower] = new_contact
            return new_contact

    print(f"      Contact '{clean_name}' NOT FOUND in Zoho Books")
    return None

def create_zoho_journal(token, journal_data, account_map, contact_map):
    """Create a journal entry in Zoho Books with FULL AUTOMATION"""
    creds = _get_creds()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"organization_id": creds["org_id"]}
    
    print(f"\n{'='*80}")
    print(f" Processing Journal #{journal_data['journal_number']} - Date: {journal_data['date']}")
    print(f"{'='*80}")
    
    # Get AP and AR account IDs
    ap_account_id = account_map.get("accounts payable")
    ar_account_id = account_map.get("accounts receivable")
    
    if not ap_account_id:
        print("  ️  Accounts Payable account not found in Zoho Books")
    if not ar_account_id:
        print("  ️  Accounts Receivable account not found in Zoho Books")
    
    # Build line items
    zoho_line_items = []
    
    for item in journal_data["line_items"]:
        ledger_name = item["ledger_name"]
        ledger_type = item["ledger_type"]
        amount = item["amount"]
        debit_or_credit = item["debit_or_credit"]
        
        print(f"   {ledger_name} ({ledger_type}): {debit_or_credit.upper()} ₹{amount:,.2f}")
        
        # Determine account ID & ledger type dynamically
        account_id = None
        
        # 1. First check if DB or Tally explicitly identifies ledger as vendor or customer
        norm_name = ledger_name.lower().strip()
        db_type = None
        db_rec = None
        if database_manager:
            db_rec = database_manager.get_ledger_by_name(ledger_name)
            if db_rec:
                db_type = (db_rec.get("type") or "").strip().lower()

        contact_match = contact_map.get(norm_name)
        
        if ledger_type == "vendor" or db_type == "vendor" or (contact_match and contact_match.get("contact_type") == "vendor"):
            ledger_type = "vendor"
            account_id = ap_account_id
            if not account_id:
                err_msg = f"Accounts Payable account not found in Zoho Books for vendor line '{ledger_name}'"
                print(f"     ️  {err_msg} - SKIPPING")
                return {"success": False, "error": err_msg}
            print(f"      Resolved to Vendor -> Using Accounts Payable account")
            
        elif ledger_type == "customer" or db_type == "customer" or (contact_match and contact_match.get("contact_type") == "customer"):
            ledger_type = "customer"
            account_id = ar_account_id
            if not account_id:
                err_msg = f"Accounts Receivable account not found in Zoho Books for customer line '{ledger_name}'"
                print(f"     ️  {err_msg} - SKIPPING")
                return {"success": False, "error": err_msg}
            print(f"      Resolved to Customer -> Using Accounts Receivable account")
            
        else:
            # 2. It is an Account ledger (Expense, Income, Asset, Liability, Round Off, etc.)
            # A. Check if ledger was synced as an account in local DB
            if db_rec and db_rec.get("zoho_contact_id") and db_type not in ["vendor", "customer"]:
                account_id = db_rec.get("zoho_contact_id")
                print(f"      Found synced account ID from DB: {ledger_name} -> {account_id}")

            # B. Check Chart of Accounts directly with exact match
            if not account_id:
                account_id = account_map.get(norm_name)
                if account_id:
                    print(f"      Found in Chart of Accounts: {ledger_name} (ID: {account_id})")

            # C. Normalize punctuation/spaces exact match (e.g., 'Salary A/c' vs 'Salary Ac')
            if not account_id:
                clean_norm = norm_name.replace("/", "").replace(".", "").replace("-", " ").strip()
                clean_norm = " ".join(clean_norm.split())
                for a_name, a_id in account_map.items():
                    clean_a = a_name.replace("/", "").replace(".", "").replace("-", " ").strip()
                    clean_a = " ".join(clean_a.split())
                    if clean_norm == clean_a:
                        account_id = a_id
                        print(f"      Clean matched '{ledger_name}' -> COA '{a_name}' (ID: {a_id})")
                        break

            if not account_id:
                err_msg = f"Account '{ledger_name}' not found in Zoho Chart of Accounts. Please create or sync this account first."
                print(f"      {err_msg} - SKIPPING")
                return {"success": False, "error": err_msg}
        
        # Build description: include bill allocations (e.g. Agst Ref 199, New Ref RI/3F/2022/12)
        item_desc_parts = []
        bill_allocs = item.get("bill_allocations") or []
        if bill_allocs:
            ref_strs = []
            for b in bill_allocs:
                b_type = b.get("bill_type") or "Ref"
                b_name = b.get("name") or ""
                b_amt = b.get("amount")
                if b_name:
                    if b_amt:
                        ref_strs.append(f"{b_type}: {b_name} (₹{b_amt})")
                    else:
                        ref_strs.append(f"{b_type}: {b_name}")
            if ref_strs:
                item_desc_parts.append("; ".join(ref_strs))

        line_item_desc = " | ".join(item_desc_parts)

        # Build line item
        line_item = {
            "account_id": account_id,
            "amount": amount,
            "debit_or_credit": debit_or_credit,
            "description": line_item_desc
        }
        
        # Add contact for vendors/customers (no auto-creation - report error if missing)
        if ledger_type in ["vendor", "customer"]:
            contact_info = find_or_create_contact(token, contact_map, ledger_name, ledger_type, auto_create=False)
            if contact_info:
                line_item["customer_id"] = contact_info["contact_id"]
                line_item["contact_id"] = contact_info["contact_id"]
                print(f"      Mapped to {ledger_type}: {contact_info['original_name']} (ID: {contact_info['contact_id']})")
            else:
                err_msg = f"{ledger_type.capitalize()} '{ledger_name}' not found in Zoho Books contacts. Please create or sync this {ledger_type} first."
                print(f"      {err_msg} - SKIPPING")
                return {"success": False, "error": err_msg}
        
        # Add reporting tags
        if item.get("tag_category") and item.get("tag_option"):
            t_id, o_id = find_tag_ids_by_name(token, item["tag_category"], item["tag_option"])
            if t_id and o_id:
                line_item["tags"] = [{"tag_id": t_id, "tag_option_id": o_id}]
                print(f"     ️  Tag: {item['tag_category']} > {item['tag_option']}")
        
        zoho_line_items.append(line_item)
    
    # Convert date format
    tally_date = journal_data["date"]
    zoho_date = f"{tally_date[:4]}-{tally_date[4:6]}-{tally_date[6:8]}"
    
    # Build notes with Tally journal number / narration
    notes_text = (journal_data.get("narration") or "").strip()[:900]
    
    # Try multiple approaches to set custom journal number
    payload = {
        "journal_date": zoho_date,
        "journal_number": journal_data['journal_number'],        # Direct number
        "entry_number": journal_data['journal_number'],          # Alternative field name
        "reference_number": journal_data['journal_number'],      # Reference field
        "notes": notes_text,
        "line_items": zoho_line_items,
        "status": "published"
    }
    
    print(f"\n  [DEBUG] Payload:")
    print(f"    Journal Number: {journal_data['journal_number']}")
    print(f"    Entry Number: {journal_data['journal_number']}")
    print(f"    Reference Number: {journal_data['journal_number']}")
    print(f"    Journal Date: {zoho_date}")
    print(f"    Line Items: {len(zoho_line_items)}")
    
    print(f"\n   Creating journal in Zoho Books...")
    from modules.zoho_connector import zoho
    res_data = zoho.api_call("POST", "/journals", payload=payload)
    
    if res_data.get("code") == 0:
        journal_id = res_data.get("journal", {}).get("journal_id", "N/A")
        print(f"   SUCCESS! Journal created with ID: {journal_id}")
        return {"success": True, "journal_id": journal_id}
    else:
        error_msg = res_data.get("message") or f"API Error (Code {res_data.get('code')})"
        print(f"   FAILED! Code: {res_data.get('code')}")
        print(f"  Response: {json.dumps(res_data, indent=2)}")
        return {"success": False, "error": error_msg}

def main():
    print(" FULLY DYNAMIC Journal Migration: Tally → Zoho Books")
    print("="*80)
    print("Features:")
    print("   - Automatic vendor/customer detection from Tally groups")
    print("   - Automatic contact creation in Zoho Books")
    print("   - Zero manual configuration required!")
    print("="*80)
    
    # Get access token
    print("\n Authenticating with Zoho Books...")
    token = get_access_token()
    if not token:
        print(" Failed to get access token")
        return
    print(" Authentication successful")
    
    # Fetch Zoho data
    print("\n Fetching Zoho Books accounts and contacts...")
    account_map = get_zoho_accounts(token)
    contact_map = get_zoho_contacts(token)
    print(f" Found {len(account_map)} accounts and {len(contact_map)} contacts")
    
    # Fetch Tally journals (first 5 for testing)
    print("\n Fetching journals from Tally...")
    journals = fetch_tally_journals(from_date="20250401", to_date="20250407", limit=5)
    print(f" Found {len(journals)} journal(s) to migrate")
    
    # Process each journal
    success_count = 0
    fail_count = 0
    created_contacts = []
    
    for journal in journals:
        result = create_zoho_journal(token, journal, account_map, contact_map)
        if isinstance(result, dict) and result.get("success"):
            success_count += 1
        elif result is True:
            success_count += 1
        else:
            fail_count += 1
    
    # Summary
    print(f"\n{'='*80}")
    print(f" MIGRATION SUMMARY")
    print(f"{'='*80}")
    print(f" Successful: {success_count}")
    print(f" Failed: {fail_count}")
    print(f" Total: {len(journals)}")
    print(f"{'='*80}")

if __name__ == "__main__":
    main()

# ----------------------------------------------------------
# API WRAPPER FOR FRONTEND
# ----------------------------------------------------------

def get_all_journals_data(from_date="20250401", to_date="20250430", limit=None):
    """
    Wrapper function for API to get journal data.
    Fetches from Tally, saves ALL fields to SQLite DB, returns formatted data
    for frontend display.  Mirrors get_all_receipts_data() pattern exactly.
    """
    # Ensure DB tables exist
    if database_manager:
        database_manager.init_db()

    try:
        journals = fetch_tally_journals(from_date, to_date, limit)

        if journals is None:
            return None
            
        if not journals:
            return {
                "journals": [],
                "stats": {
                    "total": 0,
                    "debit": 0,
                    "credit": 0
                }
            }

        # ----------------------------------------------------------------
        # SAVE EVERY FIELD TO SQLITE  (same pattern as get_all_receipts_data)
        # ----------------------------------------------------------------
        if database_manager and journals:
            now = datetime.now().isoformat()
            db_data_list = []

            for journal in journals:
                line_items = journal.get("line_items", [])

                # Compute debit / credit totals from line_items
                total_debit  = sum(
                    item["amount"] for item in line_items
                    if item.get("debit_or_credit") == "debit"
                )
                total_credit = sum(
                    item["amount"] for item in line_items
                    if item.get("debit_or_credit") == "credit"
                )

                # line_items JSON captures ALL per-line fields:
                #   ledger_name, ledger_type, amount, debit_or_credit,
                #   tag_category, tag_option  (nothing skipped)
                db_data_list.append({
                    "journal_number": journal.get("journal_number", ""),
                    "date":           journal.get("date", ""),
                    "narration":      journal.get("narration", ""),
                    "total_debit":    round(total_debit,  2),
                    "total_credit":   round(total_credit, 2),
                    # JSON-stringify the full line_items list — same as invoice_allocations in receipts
                    "line_items":     json.dumps(line_items),
                    "from_date":      from_date,
                    "to_date":        to_date,
                    "created_at":     now,
                    "updated_at":     now,
                })

            # Bulk-save to prevent 'database is locked' errors
            database_manager.bulk_save_journals(db_data_list)
            print(f" Saved {len(journals)} journals to database")

        # ----------------------------------------------------------------
        # Build return stats (same as before)
        # ----------------------------------------------------------------
        total_journals = len(journals)
        total_debit_all  = 0
        total_credit_all = 0

        for journal in journals:
            for item in journal.get("line_items", []):
                if item["debit_or_credit"] == "debit":
                    total_debit_all  += item["amount"]
                else:
                    total_credit_all += item["amount"]

        return {
            "journals": journals,
            "stats": {
                "total_journals": total_journals,
                "total_debit":    round(total_debit_all,  2),
                "total_credit":   round(total_credit_all, 2),
                "from_date":      from_date,
                "to_date":        to_date
            }
        }

    except Exception as e:
        print(f" Error in get_all_journals_data: {e}")
        raise e

def sync_journals_to_zoho(selected_journals=None, from_date="20250401", to_date="20250430", limit=None, log=None, stop_event=None):
    """
    Sync journals to Zoho Books
    If selected_journals is None, fetches and syncs all journals in date range
    
    Args:
        selected_journals: List of journal objects to sync (if None, fetches from Tally)
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        limit: Maximum number of journals to sync (respects user input)
        log: Optional logger callback for live UI console streaming
        stop_event: Optional threading event to interrupt sync gracefully
    """
    def _log(msg):
        try:
            print(msg)
        except Exception:
            try:
                print(str(msg).encode('ascii', errors='replace').decode('ascii'))
            except Exception:
                pass
        if log:
            try:
                log(str(msg))
            except Exception:
                pass

    try:
        _log(" Starting Zoho Sync (Journals)...")
        
        if stop_event and stop_event.is_set():
            _log(" Sync cancelled by user before starting.")
            return {"status": "stopped", "message": "Cancelled by user"}

        # Get access token
        token = get_access_token()
        if not token:
            _log(" Failed to get Zoho access token")
            return {"status": "error", "message": "Failed to get access token"}
        
        # Fetch Zoho data
        _log(" Fetching Zoho Books accounts & contacts...")
        account_map = get_zoho_accounts(token)
        contact_map = get_zoho_contacts(token)
        _log(f" Loaded {len(account_map)} accounts and {len(contact_map)} contacts.")
        
        # Get journals to sync
        journals_to_sync = []
        if selected_journals:
            journals_to_sync = selected_journals
            # Apply limit if provided
            if limit and len(journals_to_sync) > limit:
                journals_to_sync = journals_to_sync[:limit]
        else:
            # If no specific journals are selected, try to get them from the DB first
            if database_manager:
                db_journals = database_manager.get_journals_by_date_range(from_date, to_date, limit)
                if db_journals:
                    _log(f" Found {len(db_journals)} journals in database for sync.")
                    # Parse line_items from JSON string back to list of dicts
                    for journal in db_journals:
                        if "line_items" in journal and isinstance(journal["line_items"], str):
                            try:
                                journal["line_items"] = json.loads(journal["line_items"])
                            except json.JSONDecodeError:
                                _log(f" Error decoding line_items for journal {journal.get('journal_number')}")
                                journal["line_items"] = [] # Fallback to empty list
                    journals_to_sync = db_journals
                else:
                    _log(" No journals found in database. Fetching from Tally...")
                    journals_to_sync = fetch_tally_journals(from_date, to_date, limit)
            else:
                _log(" Database manager not available. Fetching from Tally...")
                journals_to_sync = fetch_tally_journals(from_date, to_date, limit)
        
        if not journals_to_sync:
            _log(" No journals found to sync.")
            return {"status": "error", "message": "No journals to sync"}
        
        _log(f" Syncing {len(journals_to_sync)} journal(s) to Zoho Books...\n")
        
        stats = {"created": 0, "failed": 0, "errors": []}
        
        for idx, journal in enumerate(journals_to_sync, 1):
            if stop_event and stop_event.is_set():
                _log(f"⏹️ Sync stopped by user at journal #{journal.get('journal_number')} ({idx-1}/{len(journals_to_sync)})")
                return {"status": "stopped", "message": "Sync stopped by user", "stats": stats}

            j_num = journal.get('journal_number') or 'N/A'
            _log(f"[{idx}/{len(journals_to_sync)}] Processing Journal #{j_num} (Date: {journal.get('date')})...")

            result = create_zoho_journal(token, journal, account_map, contact_map)
            is_ok = False
            err_str = "Failed to create journal in Zoho Books"
            
            if isinstance(result, dict):
                is_ok = result.get("success", False)
                err_str = result.get("error", err_str)
            elif result is True:
                is_ok = True
                
            if is_ok:
                stats["created"] += 1
                zoho_j_id = result.get("journal_id") if isinstance(result, dict) else None
                if database_manager and hasattr(database_manager, 'update_journal_zoho_status'):
                    try:
                        database_manager.update_journal_zoho_status(journal.get('journal_number'), zoho_journal_id=zoho_j_id, status='synced')
                    except Exception as db_e:
                        _log(f" Warning: Could not update journal DB status: {db_e}")
                _log(f"   ✓ Synced Journal #{j_num} (Zoho ID: {zoho_j_id})\n")
            else:
                stats["failed"] += 1
                if database_manager and hasattr(database_manager, 'update_journal_zoho_status'):
                    try:
                        database_manager.update_journal_zoho_status(journal.get('journal_number'), status='failed', error=err_str)
                    except Exception as db_e:
                        _log(f" Warning: Could not update journal DB status: {db_e}")
                _log(f"   ✗ Failed Journal #{j_num}: {err_str}\n")
                stats["errors"].append({
                    "journal_number": journal.get("journal_number", ""),
                    "narration": journal.get("narration", ""),
                    "error": err_str
                })
        
        _log(f" Completed! Created: {stats['created']} | Failed: {stats['failed']}")
        return {"status": "success", "stats": stats}
        
    except Exception as e:
        _log(f" Error in sync_journals_to_zoho: {e}")
        return {"status": "error", "message": str(e)}




def parse_tally_xml(xml_path):
    import re
    from bs4 import BeautifulSoup
    
    try:
        with open(xml_path, 'r', encoding='utf-16', errors='ignore') as f:
            content = f.read()
            if "<TALLYMESSAGE" not in content.upper():
                raise ValueError("Not utf-16 xml")
    except:
        with open(xml_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

    soup = BeautifulSoup(content, "html.parser")
    vouchers = soup.find_all("voucher")
    
    journels = []
    for v in vouchers:
        v_type_node = v.find("vouchertypename")
        if not v_type_node: continue
        voucher_type = v_type_node.text.strip()
        
        guid_node = v.find("guid")
        tally_guid = guid_node.text.strip() if guid_node else ""

        j_no_node = v.find("vouchernumber")
        j_no = j_no_node.text.strip() if j_no_node else ""
        if not j_no:
            vk_node = v.find("voucherkey")
            j_no = vk_node.text.strip() if vk_node else ""
        if not j_no:
            j_no = tally_guid
        if not j_no:
            import hashlib
            # Fallback to content hash
            j_no = "JNL-AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
        
        j_date_node = v.find("date")
        j_date = j_date_node.text.strip() if j_date_node else ""
        
        narration_node = v.find("narration")
        narration = narration_node.text.strip() if narration_node else ""
        
        # Prevent duplicate journal numbers in SQLite and Zoho Books
        if 'seen_j_no' not in locals(): seen_j_no = set()
        original_j_no = j_no
        counter = 1
        while j_no in seen_j_no:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            j_no = f"{original_j_no}_{suffix}"
            counter += 1
        seen_j_no.add(j_no)

        ledger_entries = []
        cost_centers_dict = {}
        
        all_entries = v.find_all("allledgerentries.list", recursive=False)
        for entry in all_entries:
            lname_node = entry.find("ledgername", recursive=False)
            if not lname_node: continue
            lname = lname_node.text.strip()
            
            amt_node = entry.find("amount", recursive=False)
            amt_str = amt_node.text.strip() if amt_node else "0"
            nums = re.findall(r'[-\d.]+', amt_str)
            amt = float(nums[-1]) if nums else 0.0
            
            is_deemed_node = entry.find("isdeemedpositive", recursive=False)
            is_deemed = is_deemed_node.text.strip().lower() if is_deemed_node else "no"
            
            if is_deemed in ['yes', 'true']:
                debit = abs(amt); credit = 0.0
            else:
                debit = 0.0; credit = abs(amt)
                
            ledger_entries.append({"ledger_name": lname, "debit": debit, "credit": credit})
            
            cats = entry.find_all("categoryallocations.list", recursive=False)
            for ca in cats:
                cat_node = ca.find("category", recursive=False)
                cat_name = cat_node.text.strip() if cat_node else ""
                
                ccs = ca.find_all("costcentreallocations.list", recursive=False)
                for cc in ccs:
                    cc_name_node = cc.find("name", recursive=False)
                    cc_name = cc_name_node.text.strip() if cc_name_node else ""
                    
                    cc_amt_node = cc.find("amount", recursive=False)
                    cc_amt_str = cc_amt_node.text.strip() if cc_amt_node else "0"
                    ccnums = re.findall(r'[-\d.]+', cc_amt_str)
                    cc_amt = float(ccnums[-1]) if ccnums else 0.0
                    
                    full = f"{cat_name} - {cc_name}" if cat_name and cc_name else (cc_name or cat_name)
                    if full:
                        cost_centers_dict[full] = abs(cc_amt)

        # Apply filtering for journals exactly like JSON does
        if not ledger_entries: continue
        
        # In JSON parser, it checks:
        # if not any('Journal' in str(v_type).title() for v_type in [voucher_type]): continue
        # Let's mirror the exact logic below:

        journels.append({
            "date": j_date,
            "journal_number": j_no,
            "reference_number": "",
            "notes": narration,
            "line_items": ledger_entries,
            "tally_guid": tally_guid,
            "cost_centers": cost_centers_dict,
            "voucher_type": voucher_type
        })
        
    return journels

def parse_tally_json(json_path):
    import json, re
    from bs4 import BeautifulSoup
    
    # Check if XML
    is_xml = False
    try:
        with open(json_path, 'r', encoding='utf-16', errors='ignore') as f:
            c = f.read(2000)
            if "<TALLYMESSAGE" in c.upper() or "<?xml" in c.lower(): is_xml = True
    except:
        pass
    if not is_xml:
        try:
            with open(json_path, 'r', encoding='utf-8', errors='ignore') as f:
                c = f.read(2000)
                if "<TALLYMESSAGE" in c.upper() or "<?xml" in c.lower(): is_xml = True
        except:
            pass
            
    if is_xml:
        return parse_tally_xml(json_path)


    import json, re
    try:
        with open(json_path, 'r', encoding='utf-16') as f: data = json.load(f)
    except:
        with open(json_path, 'r', encoding='utf-8') as f: data = json.load(f)
    if isinstance(data, list) and len(data) > 0 and 'date' in data[0]: return data
    vouchers = data.get('tallymessage', [])
    if isinstance(vouchers, dict): vouchers = [vouchers]
    
    journels = []
    for v in vouchers:
        if not isinstance(v, dict): continue
        if 'vouchernumber' not in v and 'vouchertypename' not in v: continue
        
        tally_guid = str(v.get('guid', '')).strip()
        j_date = str(v.get('date', '')).strip()
        j_no = str(v.get('vouchernumber') or v.get('voucherkey') or v.get('reference') or tally_guid or '').strip()
        if not j_no:
            import hashlib
            j_no = "JNL-AUTO-" + hashlib.md5(str(v).encode('utf-8')).hexdigest()[:8]
        voucher_type = str(v.get('vouchertypename', 'Journal')).strip()
        narration = str(v.get('narration', '')).strip()

        # Prevent duplicate journal numbers in SQLite and Zoho Books
        if 'seen_j_no' not in locals(): seen_j_no = set()
        original_j_no = j_no
        counter = 1
        while j_no in seen_j_no:
            suffix = tally_guid[-4:] if tally_guid and counter == 1 else str(counter)
            j_no = f"{original_j_no}_{suffix}"
            counter += 1
        seen_j_no.add(j_no)

        ledger_entries = []
        cost_centers_dict = {}

        all_entries = v.get('allledgerentries.list', v.get('allledgerentries', []))
        if not isinstance(all_entries, list): all_entries = [all_entries]
        
        for entry in all_entries:
            if not isinstance(entry, dict): continue
            lname = str(entry.get('ledgername', '')).strip()
            amt_str = str(entry.get('amount', '0'))
            nums = re.findall(r'[-\d.]+', amt_str)
            amt = float(nums[-1]) if nums else 0.0
            
            is_deemed_positive = entry.get('isdeemedpositive', False)
            if str(is_deemed_positive).lower() == 'true' or is_deemed_positive is True or 'yes' in str(is_deemed_positive).lower():
                debit = abs(amt); credit = 0.0
            else: debit = 0.0; credit = abs(amt)
            
            # Capture bill allocations (New Ref / Agst Ref)
            bill_allocs = entry.get('billallocations.list', entry.get('billallocations', []))
            if not isinstance(bill_allocs, list): bill_allocs = [bill_allocs]
            bills = []
            for b in bill_allocs:
                if not isinstance(b, dict): continue
                b_name = str(b.get('name', '')).strip()
                b_type = str(b.get('billtype', '')).strip()
                b_amt_str = str(b.get('amount', '0'))
                b_nums = re.findall(r'[-\d.]+', b_amt_str)
                b_amt = float(b_nums[-1]) if b_nums else 0.0
                if b_name or b_type:
                    bills.append({"name": b_name, "bill_type": b_type, "amount": abs(b_amt)})

            ledger_entries.append({
                "ledger_name": lname,
                "debit": debit,
                "credit": credit,
                "bill_allocations": bills
            })
            
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

        cost_center_allocations = [{"category": k, "amount": v} for k, v in cost_centers_dict.items()]
        journels.append({"date": j_date, "journal_number": j_no, "voucher_type": voucher_type, "tally_guid": tally_guid, "narration": narration, "ledger_entries": ledger_entries, "cost_center_allocations": cost_center_allocations})
    return journels

