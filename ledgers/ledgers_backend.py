import requests
import re
import html as html_module
from collections import defaultdict
import sys
import os
import json

# Ensure root directory is in path to import database_manager
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mapping_manager

try:
    import database_manager
except ImportError:
    print("️ Warning: Could not import database_manager. SQLite sync will be skipped.")
    database_manager = None

# ... (rest of imports)

# ----------------------------------------------------------
# XML HELPERS
# ----------------------------------------------------------

def extract_field(xml_block, tag):
    """Extract first occurrence <TAG>value</TAG>"""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml_block, re.DOTALL)
    return match.group(1).strip() if match else ""


def extract_all_fields(xml_block, tag):
    """Extract all repeated tags like <ADDRESS>"""
    matches = re.findall(rf"<{tag}>(.*?)</{tag}>", xml_block, re.DOTALL)
    return [m.strip() for m in matches if m.strip()]


# ----------------------------------------------------------
# FETCH GROUPS
# ----------------------------------------------------------

def fetch_groups_from_tally():
    # Initialize DB if possible
    if database_manager:
        database_manager.init_db()

    xml_request = """<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Accounts</REPORTNAME>
        <STATICVARIABLES>
          <ACCOUNTTYPE>Groups</ACCOUNTTYPE>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

    try:
        response = requests.post("http://localhost:9000",
                                 data=xml_request.encode("utf-8"),
                                 timeout=60)
        xml_data = response.text

        group_blocks = re.findall(
            r'<GROUP NAME="([^"]*)"[^>]*>(.*?)</GROUP>',
            xml_data, re.DOTALL
        )

        groups = []

        for name, block in group_blocks:
            if not name or name == "?":
                continue

            group_data = {
                "name": name.replace("&amp;", "&"),
                "parent": extract_field(block, "PARENT")
            }
            groups.append(group_data)
            
            # ------------------------------------------------
            # SAVE TO SQLITE
            # ------------------------------------------------
            if database_manager:
                database_manager.insert_or_update_group({
                    "name": group_data["name"],
                    "parent": group_data["parent"],
                    "primary_group": "" # Tally doesn't explicitly give this easily here without traversal
                })

        print(f" Groups fetched: {len(groups)}")
        return groups

    except Exception as e:
        print(" Error fetching groups:", e)
        return []


# ----------------------------------------------------------
# FETCH LEDGERS
# ----------------------------------------------------------

def fetch_ledgers_from_tally():

    xml_request = """<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Accounts</REPORTNAME>
        <STATICVARIABLES>
          <ACCOUNTTYPE>Ledgers</ACCOUNTTYPE>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

    try:
        print(" Connecting to Tally on port 9000...")
        response = requests.post(
            "http://localhost:9000",
            data=xml_request.encode("utf-8"),
            timeout=60
        )
        xml_data = response.text

        ledger_blocks = re.findall(
            r'<LEDGER NAME="([^"]*)"[^>]*>(.*?)</LEDGER>',
            xml_data, re.DOTALL
        )

        ledgers = []

        for ledger_name, ledger_block in ledger_blocks:

            ledger_name = ledger_name.replace("&amp;", "&")

            # MULTI-LINE ADDRESS FIX
            address_lines = extract_all_fields(ledger_block, "ADDRESS")

            # FIX — STATE FALLBACK LOGIC
            state = (
                extract_field(ledger_block, "STATENAME")
                or extract_field(ledger_block, "LEDSTATENAME")
                or extract_field(ledger_block, "MAILSTATENAME")
                or extract_field(ledger_block, "PRIORSTATENAME")
            )

            ledgers.append({
                "name": ledger_name,
                "parent": extract_field(ledger_block, "PARENT"),

                "opening_balance": extract_field(ledger_block, "OPENINGBALANCE"),
                "closing_balance": extract_field(ledger_block, "CLOSINGBALANCE"),

                "gstin": extract_field(ledger_block, "GSTIN"),
                "gst_reg_type": extract_field(ledger_block, "GSTREGISTRATIONTYPE"),
                "pan": extract_field(ledger_block, "INCOMETAXNUMBER"),

                "address_lines": address_lines,
                "address": "\n".join(address_lines),

                "state": state,
                "country": extract_field(ledger_block, "COUNTRY"),
                "pincode": extract_field(ledger_block, "PINCODE"),

                "phone": extract_field(ledger_block, "PHONE"),
                "email": extract_field(ledger_block, "EMAIL"),
            })

        print(f" Ledgers fetched: {len(ledgers)}")
        return ledgers

    except Exception as e:
        print(" Error fetching ledgers:", e)
        return []


# ----------------------------------------------------------
# GROUP HIERARCHY
# ----------------------------------------------------------

def build_group_hierarchy(groups):
    children_map = defaultdict(list)
    for g in groups:
        children_map[g["parent"]].append(g["name"])
    return children_map


def get_all_descendants(group_name, children_map, visited=None):
    if visited is None:
        visited = set()

    if group_name in visited:
        return []

    visited.add(group_name)
    descendants = []

    children = children_map.get(group_name, [])
    descendants.extend(children)

    for child in children:
        descendants.extend(get_all_descendants(child, children_map, visited))

    return descendants


# ----------------------------------------------------------
# LEDGER CLASSIFICATION
# ----------------------------------------------------------


def analyze_ledgers(ledgers, groups):
    children_map = build_group_hierarchy(groups)
    
    # Initialize DB if possible
    if database_manager:
        database_manager.init_db()

    # All sub-groups under Sundry Debtors
    sd = {"Sundry Debtors"}
    sd.update(get_all_descendants("Sundry Debtors", children_map))

    # All sub-groups under Sundry Creditors
    sc = {"Sundry Creditors"}
    sc.update(get_all_descendants("Sundry Creditors", children_map))

    sundry_debtors = []
    sundry_creditors = []
    other_ledgers = []

    for ledger in ledgers:
        parent = ledger["parent"]
        ledger_type = "other"

        if parent in sd:
            ledger_type = "customer"
            sundry_debtors.append(ledger)

        elif parent in sc:
            ledger_type = "vendor"
            sundry_creditors.append(ledger)

        else:
            other_ledgers.append(ledger)
        
        ledger["type"] = ledger_type

        # ------------------------------------------------
        # SAVE TO SQLITE
        # ------------------------------------------------
        if database_manager:
            db_data = {
                "name": ledger["name"],
                "parent": ledger["parent"],
                "type": ledger_type,
                "address": ledger.get("address", ""),
                "state": ledger.get("state", ""),
                "country": ledger.get("country", ""),
                "pincode": ledger.get("pincode", ""),
                "email": ledger.get("email", ""),
                "phone": ledger.get("phone", ""),
                "gstin": ledger.get("gstin", ""),
                "gst_reg_type": ledger.get("gst_reg_type", ""),
                "pan": ledger.get("pan", ""),
                "opening_balance": ledger.get("opening_balance", 0) or 0,
                "closing_balance": ledger.get("closing_balance", 0) or 0
            }
            database_manager.insert_or_update_ledger(db_data)

    return {
        "ledgers": ledgers,
        "sundry_debtors": sundry_debtors,
        "sundry_creditors": sundry_creditors,
        "other_ledgers": other_ledgers
    }


# ----------------------------------------------------------
# SEARCH ENGINE
# ----------------------------------------------------------

def search_ledger(query, ledgers):

    query = query.lower().strip()
    matches = [l for l in ledgers if query in l["name"].lower()]

    if not matches:
        print(f"\n No ledger found for '{query}'")
        return

    print(f"\n Found {len(matches)} result(s) for '{query}':")

    for l in matches:
        print("\n" + "="*80)
        print(f" NAME           : {l['name']}")
        print(f" UNDER GROUP    : {l['parent']}")
        print(f"️ LEDGER TYPE    : {l['type']}")

        print(" ADDRESS:")
        for line in l["address_lines"]:
            print(f"   {line}")

        print(f" STATE          : {l.get('state','')}")
        print(f" COUNTRY        : {l.get('country','')}")
        print(f" PINCODE        : {l.get('pincode','')}")
        print(f" PAN            : {l.get('pan','')}")
        print(f" GSTIN          : {l.get('gstin','')}")
        print(f" GST REG TYPE   : {l.get('gst_reg_type','')}")

        opening = l.get("opening_balance", "")
        if not opening:
            print(" OPENING BAL    :  No opening balance in Tally")
        else:
            print(f" OPENING BAL    : {opening}")

        print(f" CLOSING BAL    : {l.get('closing_balance','')}")
        print("="*80)


# ----------------------------------------------------------
# MAIN PROGRAM
# ----------------------------------------------------------

if __name__ == "__main__":

    print(" TALLY LEDGER ANALYSIS + SEARCH TOOL")
    print("="*80)

    print("\n Fetching Groups...")
    groups = fetch_groups_from_tally()

    print("\n Fetching Ledgers...")
    ledgers = fetch_ledgers_from_tally()

    if not ledgers:
        print(" No ledgers fetched from Tally.")
        exit()

    print("\n Analyzing Ledger Hierarchy...")
    analysis = analyze_ledgers(ledgers, groups)

    print("\n SUMMARY")
    print(f"   Customers : {len(analysis['sundry_debtors'])}")
    print(f"   Vendors   : {len(analysis['sundry_creditors'])}")
    print(f"   Others    : {len(analysis['other_ledgers'])}")

    # SEARCH LOOP
    while True:
        query = input("\n Enter Ledger/Customer/Vendor to search (or EXIT): ")
        if query.lower() == "exit":
            print(" Exiting tool.")
            break

        search_ledger(query, analysis["ledgers"])


# ----------------------------------------------------------
# API WRAPPER
# ----------------------------------------------------------

# ----------------------------------------------------------
# COST CENTERS IMPORT
# ----------------------------------------------------------
try:
    from cost_centers import cost_center_backend
except ImportError:
    print("️ Warning: Could not import cost_center_backend")
    cost_center_backend = None


def analyze_ledgers_and_groups():
    """Wrapper function for API to get all data"""
    # 1. Fetch Groups
    groups = fetch_groups_from_tally()
    
    # 2. Fetch Ledgers
    ledgers = fetch_ledgers_from_tally()

    # 3. Fetch Cost Centers (New)
    if cost_center_backend:
        print("\n Fetching Cost Categories...")
        cats = cost_center_backend.fetch_cost_categories()
        print(f" Cost Categories fetched: {len(cats)}")

        print("\n Fetching Cost Centres...")
        cents = cost_center_backend.fetch_cost_centres()
        print(f" Cost Centres fetched: {len(cents)}")
    
    if not ledgers: return None
    
    analysis = analyze_ledgers(ledgers, groups)
    children_map = build_group_hierarchy(groups)
    
    return {
        "ledgers": analysis["ledgers"],
        "customers": analysis["sundry_debtors"],
        "vendors": analysis["sundry_creditors"],
        "others": analysis["other_ledgers"],
        "groups": groups,
        "group_hierarchy": children_map,
        "stats": {
            "total_ledgers": len(ledgers),
            "total_customers": len(analysis["sundry_debtors"]),
            "total_vendors": len(analysis["sundry_creditors"]),
            "total_others": len(analysis["other_ledgers"]),
            "total_groups": len(groups)
        }
    }

# ----------------------------------------------------------
# ZOHO SYNC
# ----------------------------------------------------------

def save_groups_mapping(mapping):
    """Save the group mapping to a persistent file."""
    return mapping_manager.save_mapping(mapping)

def get_groups_mapping():
    """Retrieve the saved group mapping."""
    return mapping_manager.load_mapping()

def sync_groups_to_zoho(mapping=None, log=None, selected_ledgers=None):
    def _log(msg):
        print(msg)
        if log:
            log(msg)
    if mapping is None:
        _log(" Loading saved mapping...")
        mapping = mapping_manager.load_mapping()

    if not mapping:
        return {"status": "error", "message": "No mapping found. Please save mapping first."}

    try:
        from modules.zoho_connector import zoho
    except ImportError:
        try:
            from zoho_connector import zoho
        except:
            return {"status": "error", "message": "Zoho Connector missing"}

    _log(f" Starting Zoho Sync (Groups & Ledgers)... Mapping size: {len(mapping)}")
    stats = {"created": 0, "updated": 0, "failed": 0, "skipped": 0, "children_created": 0, "ledgers_created": 0}

    # ─────────────────────────────────────────────────────────
    # EXCLUDED GROUPS — these groups + ALL their descendants
    # are SKIPPED from chart of accounts sync.
    # (They are already handled as Zoho Contacts — customers/vendors)
    # Add any future group names here as needed.
    # ─────────────────────────────────────────────────────────
    EXCLUDED_GROUPS = {
        "Sundry Debtors",       # → synced as Zoho Customers
        "Sundry Creditors",     # → synced as Zoho Vendors
    }
    # This set grows dynamically as we discover their sub-groups
    excluded_group_names = set(g.lower() for g in EXCLUDED_GROUPS)
    failed_ledgers = []   # Ledgers that failed due to type mismatch
    duplicates = []       # Duplicate account names found in Zoho
    sync_log = []         # Detailed log of every action taken

    # 1. Fetch Source Data (From DB as requested)
    _log(" Fetching Groups & Ledgers from Local Database...")
    if database_manager:
        tally_groups = database_manager.get_all_groups()
        all_db_ledgers = database_manager.get_all_ledgers()
        if selected_ledgers and len(selected_ledgers) > 0:
            sel_names = set(str(x).strip().lower() for x in selected_ledgers)
            tally_ledgers = [l for l in all_db_ledgers if l.get("name", "").strip().lower() in sel_names]
            _log(f" Filtered to {len(tally_ledgers)} Selected Ledgers out of {len(all_db_ledgers)} total in DB.")
        else:
            tally_ledgers = all_db_ledgers
            _log(f" Loaded {len(tally_groups)} Groups and {len(tally_ledgers)} Ledgers from DB.")
    else:
        _log("️ Database Manager not loaded. Skipping child sync.")
        tally_groups = []
        tally_ledgers = []

    # 2. Get existing Chart of Accounts (all pages)
    existing_accounts = {}
    page = 1
    has_more = True

    def _fetch_all_accounts(filter_type="AccountType.All"):
        """Fetch ALL accounts from Zoho (all pages) with the given filter_by value."""
        seen = {}
        p = 1
        while True:
            r = zoho.api_call("GET", "/chartofaccounts", params={
                "page": p,
                "per_page": 200,
                "filter_by": filter_type   # AccountType.All includes system accounts
            })
            if r.get("code") != 0:
                _log(f"️ Error on page {p} [{filter_type}]: {r.get('message')}")
                break
            batch = r.get("chartofaccounts", [])
            if not batch:
                break
            for acc in batch:
                raw = html_module.unescape(acc.get("account_name", ""))
                acc["account_name"] = raw
                k = raw.lower()
                if k not in seen:
                    seen[k] = acc
            _log(f"    [{filter_type}] Page {p}: {len(batch)} fetched (running total: {len(seen)})")
            if not r.get("page_context", {}).get("has_more_page", False):
                break
            p += 1
        return seen

    _log(" Fetching Chart of Accounts from Zoho — user accounts...")
    existing_accounts = _fetch_all_accounts("AccountType.Active")

    # Also fetch ALL (includes system/built-in accounts like Capital Account, Fixed Assets etc.)
    _log(" Fetching Chart of Accounts from Zoho — including system accounts...")
    all_accounts = _fetch_all_accounts("AccountType.All")
    for k, v in all_accounts.items():
        if k not in existing_accounts:
            existing_accounts[k] = v  # merge — system accounts fill the gaps

    _log(f" Total accounts in local cache: {len(existing_accounts)} (user + system combined).")

    # ─────────────────────────────────────────────────────────
    # HELPER: When POST says 'already exists', recover from Zoho
    # ─────────────────────────────────────────────────────────
    def recover_existing_account(name):
        """
        Called when Zoho says 'account already exists' during a POST.
        Strategy (3 layers — works for user accounts AND Zoho system accounts):
          1. Check local cache (already fully populated with system accounts)
          2. Live search using search_text + AccountType.All
          3. Full page scan with AccountType.All as absolute last resort
        Returns the account dict or None.
        """
        key = html_module.unescape(name).lower()

        # Layer 1: local cache (already includes system accounts from startup fetch)
        if key in existing_accounts:
            return existing_accounts[key]

        # Layer 2: live search_text query with ALL filter
        _log(f"    [Layer 2] Searching Zoho for: '{name}'...")
        for filter_val in ["AccountType.All", "AccountType.Active"]:
            search_res = zoho.api_call("GET", "/chartofaccounts", params={
                "search_text": name,       # correct Zoho search param
                "filter_by": filter_val,
                "per_page": 200
            })
            if search_res.get("code") == 0:
                for acc in search_res.get("chartofaccounts", []):
                    decoded = html_module.unescape(acc.get("account_name", ""))
                    if decoded.lower() == key:
                        acc["account_name"] = decoded
                        existing_accounts[key] = acc   # cache for future calls
                        _log(f"    [Layer 2] Recovered '{name}' → id={acc.get('account_id')}")
                        return acc

        # Layer 3: Full page scan (last resort — walks all pages with AccountType.All)
        _log(f"    [Layer 3] Full page scan for '{name}'...")
        p = 1
        while True:
            r = zoho.api_call("GET", "/chartofaccounts", params={
                "page": p, "per_page": 200, "filter_by": "AccountType.All"
            })
            if r.get("code") != 0:
                break
            batch = r.get("chartofaccounts", [])
            if not batch:
                break
            for acc in batch:
                decoded = html_module.unescape(acc.get("account_name", ""))
                acc["account_name"] = decoded
                existing_accounts[decoded.lower()] = acc  # cache everything found
                if decoded.lower() == key:
                    _log(f"    [Layer 3] Recovered '{name}' → id={acc.get('account_id')}")
                    return acc
            if not r.get("page_context", {}).get("has_more_page", False):
                break
            p += 1

        _log(f"    Account '{name}' not found in Zoho via any method.")
        return None

    # 2b. Detect TALLY-side duplicates (same ledger name, different parents)
    tally_duplicates = []
    tally_ledger_name_map = {}  # name.lower() -> list of {name, parent}
    for ledger in tally_ledgers:
        key = ledger["name"].lower()
        if key not in tally_ledger_name_map:
            tally_ledger_name_map[key] = []
        tally_ledger_name_map[key].append({"name": ledger["name"], "parent": ledger.get("parent", "")})

    for key, entries in tally_ledger_name_map.items():
        if len(entries) > 1:
            tally_duplicates.append({
                "name": entries[0]["name"],
                "occurrences": entries  # list of {name, parent}
            })

    if tally_duplicates:
        _log(f"️ Found {len(tally_duplicates)} duplicate ledger names in Tally data!")

    # Track Valid Parents for subsequent phases (Name -> {id, type})
    valid_parents = {}

    # ---------------------------------------------------------
    # PHASE 1: Sync Mapped PARENT Groups
    # ---------------------------------------------------------
    _log(f" PHASE 1: Syncing {len(mapping)} Mapped Parent Groups...")
    
    for group_name, user_type in mapping.items():
        if not user_type:
            continue

        group_key = html_module.unescape(group_name).lower()  # FIX: decode before lookup
        account_type = user_type.lower().replace(" ", "_")

        # Check existence (HTML-decoded cache)
        if group_key in existing_accounts:
            acc_id = existing_accounts[group_key]["account_id"]
            _log(f"⏩ Parent already in Zoho (cached): '{group_name}' → id={acc_id}")
            sync_log.append({"phase": "Parent Groups", "name": group_name, "action": "Skipped", "reason": "Already exists in Zoho", "zoho_type": account_type})
            valid_parents[group_name] = {"id": acc_id, "type": account_type}
            stats["skipped"] += 1
            continue

        # Create Parent
        _log(f" Creating Parent Account: '{group_name}'...")
        payload = {
            "account_name": group_name,
            "account_type": account_type
        }

        res = zoho.api_call("POST", "/chartofaccounts", payload=payload)

        if res.get("code") == 0:
            new_acc = res.get("chart_of_account", {})
            _log(f" Created Parent: {group_name}")
            sync_log.append({"phase": "Parent Groups", "name": group_name, "action": "Created", "reason": "New account created in Zoho", "zoho_type": account_type})
            stats["created"] += 1
            existing_accounts[group_key] = new_acc
            valid_parents[group_name] = {"id": new_acc.get("account_id"), "type": account_type}

        else:
            error_msg = res.get("message", "")
            _log(f"️ Could not create Parent '{group_name}': {error_msg}")

            # FIX: 'Already exists' → recover the account_id so children are not abandoned
            if "already exists" in error_msg.lower():
                recovered = recover_existing_account(group_name)
                if recovered:
                    acc_id = recovered.get("account_id")
                    existing_accounts[group_key] = recovered
                    valid_parents[group_name] = {"id": acc_id, "type": account_type}
                    _log(f" Recovered Parent '{group_name}' → id={acc_id}")
                    sync_log.append({"phase": "Parent Groups", "name": group_name, "action": "Recovered", "reason": "Already exists - recovered ID from Zoho", "zoho_type": account_type})
                    stats["skipped"] += 1
                else:
                    sync_log.append({"phase": "Parent Groups", "name": group_name, "action": "Failed", "reason": error_msg, "zoho_type": account_type})
                    stats["failed"] += 1
            else:
                sync_log.append({"phase": "Parent Groups", "name": group_name, "action": "Failed", "reason": error_msg, "zoho_type": account_type})
                stats["failed"] += 1

    # ---------------------------------------------------------
    # PHASE 2: Sync CHILD Groups (Sub-Accounts) 
    # ---------------------------------------------------------
    _log(f" PHASE 2: Syncing Child Groups under Mapped Parents...")

    for grp in tally_groups:
        tally_name = grp["name"]
        tally_parent = grp["parent"]  # The immediate parent in Tally

        # EXCLUDE: skip Sundry Debtors/Creditors and ALL their descendants
        if tally_name.lower() in excluded_group_names or tally_parent.lower() in excluded_group_names:
            excluded_group_names.add(tally_name.lower())  # propagate exclusion to children
            _log(f" Skipping excluded group: '{tally_name}' (contacts, not chart of accounts)")
            sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Excluded", "reason": "Under Sundry Debtors/Creditors (synced as contacts)", "zoho_type": "-"})
            continue

        # Check if Parent is Valid (Mapped)
        if tally_parent in valid_parents:
            parent_info = valid_parents[tally_parent]
            parent_zoho_id = parent_info["id"]
            account_type = parent_info["type"] # Inherit Type

            # Check existence and Parent Link
            if tally_name.lower() in existing_accounts:
                existing = existing_accounts[tally_name.lower()]
                acc_id = existing["account_id"]
                current_parent_id = existing.get("parent_account_id", "")
                
                # Update Parent Link if mismatched
                if str(current_parent_id) != str(parent_zoho_id):
                    _log(f" Correcting Parent for Group '{tally_name}'...")
                    res = zoho.api_call("PUT", f"/chartofaccounts/{acc_id}", payload={
                         "parent_account_id": parent_zoho_id
                    })
                    if res.get("code") == 0:
                        _log(f" Re-linked Group Parent: {tally_name}")
                        stats["updated"] += 1
                        sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Updated", "reason": f"Re-linked parent to {tally_parent}", "zoho_type": account_type})
                else:
                    stats["skipped"] += 1
                    sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Skipped", "reason": "Already exists with correct parent", "zoho_type": account_type})
                
                valid_parents[tally_name] = {"id": acc_id, "type": account_type}
                continue
            
            _log(f" Creating Child Group: '{tally_name}' under '{tally_parent}'...")

            payload = {
                "account_name": tally_name,
                "account_type": account_type,
                "parent_account_id": parent_zoho_id,
                "is_sub_account": True,
            }

            res = zoho.api_call("POST", "/chartofaccounts", payload=payload)

            if res.get("code") == 0:
                new_acc = res.get("chart_of_account", {})
                _log(f" Created Child Group: {tally_name}")
                stats["children_created"] += 1
                sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Created", "reason": f"New sub-group under {tally_parent}", "zoho_type": account_type})
                existing_accounts[tally_name.lower()] = new_acc
                valid_parents[tally_name] = {"id": new_acc.get("account_id"), "type": account_type}

            else:
                error_msg = res.get("message", "")
                _log(f"️ Could not create Child Group '{tally_name}': {error_msg}")

                # FIX: 'Already exists' → recover so grandchildren/ledgers are not abandoned
                if "already exists" in error_msg.lower():
                    recovered = recover_existing_account(tally_name)
                    if recovered:
                        acc_id = recovered.get("account_id")
                        existing_accounts[tally_name.lower()] = recovered
                        valid_parents[tally_name] = {"id": acc_id, "type": account_type}
                        _log(f" Recovered Child Group '{tally_name}' → id={acc_id}")
                        sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Recovered", "reason": "Already exists - recovered ID", "zoho_type": account_type})
                        stats["skipped"] += 1
                    else:
                        sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Failed", "reason": error_msg, "zoho_type": account_type})
                        stats["failed"] += 1
                else:
                    sync_log.append({"phase": "Child Groups", "name": tally_name, "action": "Failed", "reason": error_msg, "zoho_type": account_type})
                    stats["failed"] += 1

    # ---------------------------------------------------------
    # PHASE 3: Sync LEDGERS (as Sub-Accounts)
    # ---------------------------------------------------------
    _log(f" PHASE 3: Syncing Ledgers under Valid Groups...")

    for ledger in tally_ledgers:
        ledger_name = ledger["name"]
        ledger_parent = ledger["parent"]

        # EXCLUDE: skip ledgers under Sundry Debtors/Creditors (already Zoho contacts)
        if ledger_parent.lower() in excluded_group_names:
            stats["skipped"] += 1
            sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Excluded", "reason": f"Under {ledger_parent} (contacts)", "zoho_type": "-"})
            continue
        
        # Check if Ledger's Parent is in our Valid Scope (Mapped or Created Child)
        if ledger_parent in valid_parents:
            parent_info = valid_parents[ledger_parent]
            parent_zoho_id = parent_info["id"]
            account_type = parent_info["type"] # Inherit Type
            
            # Check existence and Parent Link
            if ledger_name.lower() in existing_accounts:
                existing = existing_accounts[ledger_name.lower()]
                acc_id = existing["account_id"]
                current_parent_id = existing.get("parent_account_id", "")
                
                # Update Parent Link if mismatched
                if str(current_parent_id) != str(parent_zoho_id):
                    _log(f" Correcting Parent for Ledger '{ledger_name}'...")
                    res = zoho.api_call("PUT", f"/chartofaccounts/{acc_id}", payload={
                         "parent_account_id": parent_zoho_id
                    })
                    if res.get("code") == 0:
                        _log(f" Re-linked Ledger Parent: {ledger_name}")
                        stats["updated"] += 1
                        if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                            database_manager.update_ledger_zoho_status(ledger_name, acc_id, 'synced')
                        sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Updated", "reason": f"Re-linked parent to {ledger_parent}", "zoho_type": account_type})
                else:
                    stats["skipped"] += 1
                    if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                        database_manager.update_ledger_zoho_status(ledger_name, acc_id, 'synced')
                    sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Skipped", "reason": "Already exists with correct parent", "zoho_type": account_type})
                continue
                
            _log(f" Creating Ledger Account: '{ledger_name}' under '{ledger_parent}'...")
            
            payload = {
                "account_name": ledger_name,
                "account_type": account_type,
                "parent_account_id": parent_zoho_id,
                "is_sub_account": True,
                "description": f"Imported from Tally Ledger: {ledger_name}"
            }

            res = zoho.api_call("POST", "/chartofaccounts", payload=payload)

            if res.get("code") == 0:
                _log(f" Created Ledger: {ledger_name}")
                stats["ledgers_created"] += 1
                new_acc = res.get("chart_of_account", {})
                new_acc_id = new_acc.get("account_id", "")
                if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                    database_manager.update_ledger_zoho_status(ledger_name, new_acc_id, 'synced')
                sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Created", "reason": f"New ledger under {ledger_parent}", "zoho_type": account_type})
                existing_accounts[ledger_name.lower()] = new_acc

            else:
                error_msg = res.get("message", "Unknown error")

                # FIX: 'Already exists' → silently skip, not a real failure
                if "already exists" in error_msg.lower():
                    recovered = recover_existing_account(ledger_name)
                    if recovered:
                        rec_id = recovered.get("account_id", "")
                        existing_accounts[ledger_name.lower()] = recovered
                        if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                            database_manager.update_ledger_zoho_status(ledger_name, rec_id, 'synced')
                        _log(f"⏩ Ledger already exists in Zoho (skipped): '{ledger_name}'")
                        sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Recovered", "reason": "Already exists - recovered ID", "zoho_type": account_type})
                        stats["skipped"] += 1
                    else:
                        _log(f"️ Ledger '{ledger_name}' reported as existing but could not be found.")
                        sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Skipped", "reason": "Reported existing but not found", "zoho_type": account_type})
                        stats["skipped"] += 1
                else:
                    _log(f" Failed to create Ledger '{ledger_name}': {error_msg}")
                    stats["failed"] += 1
                    if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                        database_manager.update_ledger_zoho_status(ledger_name, '', 'failed')
                    sync_log.append({"phase": "Ledgers", "name": ledger_name, "action": "Failed", "reason": error_msg, "zoho_type": account_type})
                    failed_ledgers.append({
                        "name": ledger_name,
                        "parent": ledger_parent,
                        "error": error_msg,
                        "inherited_type": account_type
                    })

    # Return combined stats
    stats["total_created"] = stats["created"] + stats["children_created"] + stats["ledgers_created"]
    return {
        "status": "success",
        "stats": stats,
        "failed_ledgers": failed_ledgers,
        "duplicates": duplicates,           # Zoho-side duplicates
        "tally_duplicates": tally_duplicates,  # Tally-side duplicates (same name, diff parent)
        "sync_log": sync_log                # Detailed action log for every item
    }

def get_gst_treatment(ledger):
    gstin = ledger.get("gstin", "")
    reg_type = ledger.get("gst_reg_type", "").lower()
    place = ledger.get("state", "").lower()
    
    if gstin:
        return "business_gst"
    if "consumer" in reg_type:
        return "consumer"
    if "composition" in reg_type:
        return "business_composition"
    if "unregistered" in reg_type:
        return "business_none"

def create_standalone_account(ledger_name, account_type):
    """
    Create a ledger in Zoho Books WITHOUT a parent account.
    Used when the normal sub-account creation fails due to type mismatch.
    The migration team selects the correct account_type manually.
    """
    try:
        from modules.zoho_connector import zoho
    except ImportError:
        try:
            from zoho_connector import zoho
        except:
            return {"status": "error", "message": "Zoho Connector missing"}

    # Zoho requires snake_case for account_type e.g. "other_asset", "bank", "long_term_liability"
    zoho_account_type = account_type.lower().replace(" ", "_")

    payload = {
        "account_name": ledger_name,
        "account_type": zoho_account_type
    }

    print(f" Creating Standalone Account: '{ledger_name}' as '{zoho_account_type}'...")
    res = zoho.api_call("POST", "/chartofaccounts", payload=payload)

    if res.get("code") == 0:
        new_acc = res.get("chart_of_account", {})
        print(f" Created Standalone: {ledger_name}")
        return {
            "status": "success",
            "account_id": new_acc.get("account_id"),
            "account_name": ledger_name,
            "account_type": account_type
        }
    else:
        error_msg = res.get("message", "Unknown error")
        print(f" Failed Standalone '{ledger_name}': {error_msg}")
        return {"status": "error", "message": error_msg}

    if "overseas" in place or "foreign" in place:
        return "overseas"
    return "business_none" # Default

def sync_ledgers_to_zoho(selected_ledgers=None, contact_type_filter=None, update_existing=False, log=None):  # OPTIMISED
    """
    Syncs ledgers to Zoho Books as Contacts with live console logs.
    
    Args:
        selected_ledgers: Optional list of specific ledgers to sync.
        contact_type_filter: 'customer' or 'vendor' — if set, only syncs that type.
                             If None, syncs both customers AND vendors.
        update_existing: If True, updates existing Zoho contacts.
        log: Optional logger callback (e.g. SSE job log or print).
    """
    def _emit(msg):
        print(f" {msg}")
        if log and callable(log):
            try:
                log(str(msg))
            except Exception:
                pass
    try:
        from modules.zoho_connector import zoho
    except ImportError:
        try:
            from zoho_connector import zoho
        except:
            print(" Could not import Zoho Connector")
            return {"status": "error", "message": "Zoho Connector missing"}

    try:
        import field_mapping_manager
        customers_mapping = field_mapping_manager.load("customers").get("mapping", {})
        vendors_mapping = field_mapping_manager.load("vendors").get("mapping", {})
    except ImportError:
        customers_mapping = {}
        vendors_mapping = {}

    def set_nested_value(d, key_path, value):
        import re
        parts = key_path.replace(']', '').replace('[', '.').split('.')
        parts = [p for p in parts if p]
        
        current = d
        for i, part in enumerate(parts[:-1]):
            next_part = parts[i+1]
            is_next_list = next_part.isdigit()
            
            if part.isdigit():
                part = int(part)
                while len(current) <= part:
                    current.append({} if not is_next_list else [])
                current = current[part]
            else:
                if part not in current:
                    current[part] = [] if is_next_list else {}
                current = current[part]
                
        last_part = parts[-1]
        if last_part.isdigit():
            last_part = int(last_part)
            while len(current) <= last_part:
                current.append(None)
            current[last_part] = value
        else:
            current[last_part] = value


    print(f" Starting Zoho Sync (Ledgers) — Filter: {contact_type_filter or 'all'} | update_existing={bool(update_existing)}...")

    # Get fresh data if not provided
    if not selected_ledgers:
        if database_manager:
            print(" Fetching ledgers from SQLite Database...")
            all_ledgers = database_manager.get_all_ledgers()
            # Filter by type
            if contact_type_filter:
                ledgers_to_sync = [l for l in all_ledgers if l['type'] == contact_type_filter]
            else:
                ledgers_to_sync = [l for l in all_ledgers if l['type'] in ['customer', 'vendor']]
            
            if not ledgers_to_sync:
                print("️ No matching ledgers found in DB. Trying Tally fetch...")
                data = analyze_ledgers_and_groups()
                if data:
                    if contact_type_filter == 'customer':
                        ledgers_to_sync = data["customers"]
                    elif contact_type_filter == 'vendor':
                        ledgers_to_sync = data["vendors"]
                    else:
                        ledgers_to_sync = data["customers"] + data["vendors"]
        else:
            print(" Fetching ledgers directly from Tally...")
            data = analyze_ledgers_and_groups()
            if not data: return {"status": "error", "message": "No Tally Data"}
            if contact_type_filter == 'customer':
                ledgers_to_sync = data["customers"]
            elif contact_type_filter == 'vendor':
                ledgers_to_sync = data["vendors"]
            else:
                ledgers_to_sync = data["customers"] + data["vendors"]
    else:
        # If a filter is passed with selected_ledgers, still honour it
        if contact_type_filter:
            ledgers_to_sync = [l for l in selected_ledgers if l.get('type') == contact_type_filter]
        else:
            ledgers_to_sync = selected_ledgers

    # ─────────────────────────────────────────────────────────────────────────
    # OPTIMISATION: Check SQLite zoho_masters_cache FIRST (Zero API calls)
    # If not cached, fetch paginated contacts from Zoho and cache to SQLite.
    # ─────────────────────────────────────────────────────────────────────────
    existing_contacts = {}   # key: contact_name.lower() -> contact dict
    per_page = 200           # Zoho max per page
    types_to_preload = [contact_type_filter] if contact_type_filter else ["customer", "vendor"]

    org_id = ""
    try:
        from modules.zoho_connector import _get_creds
        org_id = _get_creds().get("org_id", "")
    except Exception:
        pass

    # 1. Try loading from SQLite cache
    cached_contacts = None
    if database_manager and hasattr(database_manager, 'get_zoho_master_cache'):
        cached_contacts = database_manager.get_zoho_master_cache('contacts', expected_org_id=org_id)

    if cached_contacts and isinstance(cached_contacts, (dict, list)):
        print(f" Loaded {len(cached_contacts)} Zoho contacts from local SQLite master cache.")
        if isinstance(cached_contacts, dict):
            for k, c in cached_contacts.items():
                cname = (c.get("contact_name") or c.get("original_name") or k).lower().strip()
                existing_contacts[cname] = c
        elif isinstance(cached_contacts, list):
            for c in cached_contacts:
                cname = (c.get("contact_name") or c.get("original_name") or "").lower().strip()
                if cname:
                    existing_contacts[cname] = c
    else:
        print(" Pre-loading existing Zoho contacts from Zoho Books API (paginated)...")
        for ctype in types_to_preload:
            page = 1
            while True:
                res = zoho.api_call("GET", "/contacts", params={
                    "contact_type": ctype,
                    "page": page,
                    "per_page": per_page
                })
                if res.get("code") != 0:
                    print(f"️ Could not pre-load {ctype} contacts page {page}: {res.get('message')}")
                    break

                contacts_page = res.get("contacts", [])
                for c in contacts_page:
                    key = c["contact_name"].lower().strip()
                    existing_contacts[key] = c

                has_more = res.get("page_context", {}).get("has_more_page", False)
                print(f"    Loaded {ctype} page {page} — {len(contacts_page)} contacts (has_more={has_more})")
                if not has_more:
                    break
                page += 1

        # Cache to SQLite DB for future zero-API lookups
        if database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
            database_manager.save_zoho_master_cache('contacts', existing_contacts, org_id=org_id)
            print(f" Saved {len(existing_contacts)} contacts to SQLite master cache.")

    print(f" Available {len(existing_contacts)} Zoho contacts for reconciliation.")

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN SYNC LOOP — uses local map for existence check (zero extra GETs)
    # ─────────────────────────────────────────────────────────────────────────
    stats = {"created": 0, "updated": 0, "failed": 0, "skipped": 0}
    failed_names = []  # track which contacts failed for frontend display
    total = len(ledgers_to_sync)

    def clean(val): return (val or "").replace("\r", "").replace("\n", "").strip()

    def clean_address(val):
        """Join Tally multi-line address with ', ' so each line stays readable."""
        if not val:
            return ""
        lines = [l.strip() for l in (val or "").replace("\r", "").split("\n") if l.strip()]
        return ", ".join(lines)

    def normalize_mobile(val):
        """
        Return mobile in Zoho-friendly format: +91-XXXXXXXXXX
        """
        if not val:
            return ""
        digits = re.sub(r"\D", "", str(val))
        if not digits:
            return ""
        # Keep last 10 digits as mobile (strip country/STD codes)
        if len(digits) > 10:
            digits = digits[-10:]
        if len(digits) != 10:
            return ""
        return f"+91-{digits}"

    def _get_contact_details(contact_id):
        """Fetch contact details to get contact_person_id for reliable phone clearing."""
        try:
            d = zoho.api_call("GET", f"/contacts/{contact_id}")
            if d.get("code") != 0:
                return {}
            return d.get("contact", {}) or {}
        except Exception:
            return {}

    for idx, l in enumerate(ledgers_to_sync, 1):
        # Strip \r \n and extra whitespace — Tally data often has carriage returns embedded
        name = l["name"].replace("\r", "").replace("\n", "").strip()
        name_key = name.lower()

        # Progress log every 50 records
        if idx % 50 == 0 or idx == 1 or idx == total:
            print(f"    Progress: {idx}/{total} | created={stats['created']} skipped={stats['skipped']} failed={stats['failed']}")

        # Determine contact type — use the ledger's own type field
        contact_type = "customer"
        if l.get("type") == "vendor":
            contact_type = "vendor"

        # LOCAL existence check — no API call needed
        exists = name_key in existing_contacts
        if exists and not update_existing:
            stats["skipped"] += 1
            continue  # Already in Zoho, skip silently

        address_str = clean_address(l.get("address", ""))  # joins multi-line with ', '
        city        = clean(l.get("city", ""))
        state       = clean(l.get("state", ""))
        zip_code    = clean(l.get("pincode", ""))
        country     = clean(l.get("country", ""))
        email       = clean(l.get("email", ""))
        phone_raw   = clean(l.get("phone", ""))  # Tally phone → used as mobile in Zoho
        mobile      = normalize_mobile(phone_raw)

        gstin_clean = clean(l.get("gstin", ""))
        gst_treatment = get_gst_treatment(l) or ("business_gst" if gstin_clean else "consumer")

        payload = {
            "contact_name": name,
            "company_name": name,
            "contact_type": contact_type,
            "gst_treatment": gst_treatment,
            "gst_no": gstin_clean or "",
            "email":  email,
            "mobile": mobile,
            "billing_address": {
                "address": address_str,
                "city":    city,
                "state":   state,
                "zip":     zip_code,
                "country": country
            },
            "shipping_address": {
                "address": address_str,
                "city":    city,
                "state":   state,
                "zip":     zip_code,
                "country": country
            }
        }

        # Keep Phone empty (only Mobile should be filled)
        payload["phone"] = ""

        # If GSTIN is empty, don't send GST-only fields (Zoho keeps them blank).
        if not payload.get("gst_no"):
            payload.pop("gst_no", None)
            payload.pop("gst_treatment", None)
        else:
            # Zoho Books India UI requires these once GST Treatment is selected.
            payload["business_legal_name"] = name
            payload["business_trade_name"] = name
            if state:
                payload["place_of_supply"] = state


        # --- DYNAMIC FIELD MAPPING OVERRIDE ---
        mapping = customers_mapping if contact_type == "customer" else vendors_mapping
        for zoho_key, tally_key in mapping.items():
            tally_val = l.get(tally_key)
            if tally_val is not None:
                # Clean strings slightly if needed
                if isinstance(tally_val, str):
                    if tally_key == "address":
                        tally_val = clean_address(tally_val)
                    elif tally_key == "phone":
                        tally_val = normalize_mobile(tally_val)
                    else:
                        tally_val = clean(tally_val)
                        
                set_nested_value(payload, zoho_key, tally_val)

        # CREATE / UPDATE — with [{idx}/{total}] progress logging format
        if exists:
            contact_id = (existing_contacts.get(name_key) or {}).get("contact_id")
            if not contact_id:
                stats["skipped"] += 1
                _emit(f"[{idx}/{total}] ⏩ Skipped (missing ID): {name}")
                continue
            payload["phone"] = ""  # keep Phone empty; only Mobile should be filled
            if payload.get("contact_persons"):
                details = _get_contact_details(contact_id)
                cps = details.get("contact_persons", []) or []
                primary = None
                for cp in cps:
                    if cp.get("is_primary_contact"):
                        primary = cp
                        break
                if not primary and cps:
                    primary = cps[0]
                if primary and primary.get("contact_person_id"):
                    payload["contact_persons"][0]["contact_person_id"] = primary.get("contact_person_id")
            res = zoho.api_call("PUT", f"/contacts/{contact_id}", payload=payload)
            if res.get("code") == 0:
                stats["updated"] += 1
                if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                    database_manager.update_ledger_zoho_status(name, contact_id, 'synced')
                _emit(f"[{idx}/{total}]  Updated ({contact_type}): {name}")
            else:
                stats["failed"] += 1
                err_msg = res.get('message', 'Unknown error')
                failed_names.append({"name": name, "reason": err_msg})
                if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                    database_manager.update_ledger_zoho_status(name, contact_id or '', 'failed')
                _emit(f"[{idx}/{total}] ❌ Update Failed '{name}': {err_msg}")
        else:
            res = zoho.api_call("POST", "/contacts", payload=payload)
            if res.get("code") == 0:
                stats["created"] += 1
                new_contact = res.get("contact", {})
                new_cid = new_contact.get("contact_id", "")
                existing_contacts[name_key] = new_contact
                if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                    database_manager.update_ledger_zoho_status(name, new_cid, 'synced')
                _emit(f"[{idx}/{total}]  Created ({contact_type}): {name}")
            else:
                stats["failed"] += 1
                err_msg = res.get('message', 'Unknown error')
                failed_names.append({"name": name, "reason": err_msg})
                if database_manager and hasattr(database_manager, 'update_ledger_zoho_status'):
                    database_manager.update_ledger_zoho_status(name, '', 'failed')
                _emit(f"[{idx}/{total}] ❌ Create Failed '{name}': {err_msg}")

    # Persist updated contacts state to local SQLite master cache
    if (stats["created"] > 0 or stats["updated"] > 0) and database_manager and hasattr(database_manager, 'save_zoho_master_cache'):
        database_manager.save_zoho_master_cache('contacts', existing_contacts, org_id=org_id)
        _emit(f" Updated SQLite master cache with latest contacts state.")

    _emit(f"Sync Complete — Created: {stats['created']}, Updated: {stats['updated']}, Skipped: {stats['skipped']}, Failed: {stats['failed']}")
    return {"status": "success", "stats": stats, "failed_contacts": failed_names}

