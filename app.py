from flask import Flask, jsonify, render_template, send_file, request, session, Response, stream_with_context
from flask_cors import CORS
import sys
import os
import json
import re
import time
import io
import threading

# Reconfigure stdout/stderr for UTF-8 on Windows terminal
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

import field_mapping_manager
try:
    import openpyxl
except ImportError:
    openpyxl = None

# Add modules directory to path
sys.path.append(os.path.dirname(__file__))

try:
    from modules.job_manager import jobs as job_manager, sse_format
except Exception:
    job_manager = None
    sse_format = None

# Import backend modules
try:
    from ledgers import ledgers_backend as ledgers_module
    print(" Successfully imported ledgers_text backend")
except ImportError as e:
    print(f" Error importing ledgers_backend: {e}")
    ledgers_module = None

try:
    from items import items_backend as items_module
    print(" Successfully imported items_backend")
except ImportError as e:
    print(f" Error importing items_backend: {e}")
    items_module = None

try:
    from journel import journel_backend as journel_module
    print(" Successfully imported journel_backend")
except ImportError as e:
    print(f" Error importing journel_backend: {e}")
    journel_module = None

try:
    from invoice import invoice_backend as invoice_module
    print(" Successfully imported invoice_backend")
except ImportError as e:
    print(f" Error importing invoice_backend: {e}")
    invoice_module = None

try:
    from bills import bills_backend as bills_module
    print(" Successfully imported bills_backend")
except ImportError as e:
    print(f" Error importing bills_backend: {e}")
    bills_module = None

try:
    from sales_order import sale_backend as sales_order_module
    print(" Successfully imported sales_order_backend")
except ImportError as e:
    print(f" Error importing sales_order_backend: {e}")
    sales_order_module = None

try:
    from purchase_order import purchase_order_backend as purchase_order_module
    print(" Successfully imported purchase_order_backend")
except ImportError as e:
    print(f" Error importing purchase_order_backend: {e}")
    purchase_order_module = None

try:
    from receipts import receipts_backend as receipts_module
    print(" Successfully imported receipts_backend")
except ImportError as e:
    print(f" Error importing receipts_backend: {e}")
    receipts_module = None

try:
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("payments_backend", os.path.join(os.path.dirname(__file__), 'Payments made', 'payments_backend.py'))
    payments_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(payments_module)
    print(" Successfully imported payments_backend")
except Exception as e:
    print(f" Error importing payments_backend: {e}")
    payments_module = None

try:
    spec_contra = importlib.util.spec_from_file_location("contra_backend", os.path.join(os.path.dirname(__file__), 'contra', 'contra_backend.py'))
    contra_module = importlib.util.module_from_spec(spec_contra)
    spec_contra.loader.exec_module(contra_module)
    print(" Successfully imported contra_backend")
except Exception as e:
    print(f" Error importing contra_backend: {e}")
    contra_module = None

try:
    spec_credit_note = importlib.util.spec_from_file_location("credit_note_backend", os.path.join(os.path.dirname(__file__), 'credit_note', 'credit_note_backend.py'))
    credit_note_module = importlib.util.module_from_spec(spec_credit_note)
    spec_credit_note.loader.exec_module(credit_note_module)
    print(" Successfully imported credit_note_backend")
except Exception as e:
    print(f" Error importing credit_note_backend: {e}")
    credit_note_module = None

try:
    spec_debit_note = importlib.util.spec_from_file_location("debit_note_backend", os.path.join(os.path.dirname(__file__), 'debit_note', 'debit_note_backend.py'))
    debit_note_module = importlib.util.module_from_spec(spec_debit_note)
    spec_debit_note.loader.exec_module(debit_note_module)
    print(" Successfully imported debit_note_backend")
except Exception as e:
    print(f" Error importing debit_note_backend: {e}")
    debit_note_module = None

try:
    import database_manager
    print(" Successfully imported database_manager")
except ImportError as e:
    print(f" Error importing database_manager: {e}")
    database_manager = None

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

def _sanitize_db_filename(name: str) -> str:
    """
    Return a safe sqlite filename in the project folder.
    Allows letters/numbers/space/._- and forces .db extension.
    """
    if not name:
        return database_manager.get_default_db_name()
    base = str(name).strip()
    # Normalize common inputs like "AGRITOUGH MACHINERIES - (from 1-Apr-25)"
    base = re.sub(r"\s+", " ", base)
    base = re.sub(r"[^A-Za-z0-9 ._\\-]", "", base).strip()
    base = base.replace(" ", "_")
    if not base:
        base = "company"
    if not base.lower().endswith(".db"):
        base = base + ".db"
    # Block path traversal / directories
    base = os.path.basename(base)
    return base

@app.before_request
def _set_active_company_db():
    try:
        from modules import company_manager
        from modules.zoho_connector import set_thread_company
        active_cid = session.get("active_company_id")
        if not active_cid:
            active_cid = company_manager.get_active_company_id()
            session["active_company_id"] = active_cid
        
        comp = company_manager.get_active_company(active_cid)
        db_name = session.get("active_db") or comp.get("db_name") or getattr(database_manager, "DEFAULT_DB_NAME", database_manager.get_default_db_name())
        session["active_db"] = db_name
        
        # 1. Lock DB context for this request thread
        if database_manager:
            database_manager.set_active_db(db_name)
        # 2. Lock Zoho credentials context for this request thread
        if comp:
            set_thread_company(comp)
    except Exception as e:
        pass

@app.route('/api/companies', methods=['GET'])
def api_get_companies():
    """List all registered companies with their database and Zoho org configuration."""
    try:
        from modules import company_manager
        companies = company_manager.load_companies()
        active_id = session.get("active_company_id") or company_manager.get_active_company_id()
        return jsonify({
            "status": "success",
            "companies": list(companies.values()),
            "active_company_id": active_id,
            "active_company": companies.get(active_id, company_manager.get_active_company())
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/companies/switch', methods=['POST'])
def api_switch_company():
    """Switch active company profile, database, and Zoho credentials."""
    try:
        from modules import company_manager
        data = request.get_json(force=True, silent=True) or {}
        company_id = data.get("company_id")
        if not company_id:
            return jsonify({"error": "Company ID required"}), 400
        
        comp = company_manager.get_active_company(company_id)
        if not comp:
            return jsonify({"error": f"Company '{company_id}' not found"}), 404
        
        company_manager.set_active_company_id(company_id)
        session["active_company_id"] = company_id
        session["active_db"] = comp.get("db_name")
        
        if database_manager:
            database_manager.set_active_db(comp.get("db_name"))
            database_manager.init_db(db_name=comp.get("db_name"))
            
        return jsonify({
            "status": "success",
            "message": f"Successfully switched to {comp.get('name')}",
            "active_company": comp
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/companies/save', methods=['POST'])
def api_save_company():
    """Create or update a company profile."""
    try:
        from modules import company_manager
        data = request.get_json(force=True, silent=True) or {}
        cid = company_manager.save_company(data)
        return jsonify({"status": "success", "company_id": cid, "message": "Company saved successfully!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/companies/<company_id>', methods=['DELETE'])
def api_delete_company(company_id):
    """Delete a company profile."""
    try:
        from modules import company_manager
        success = company_manager.delete_company(company_id)
        if success:
            return jsonify({"status": "success", "message": "Company deleted."})
        return jsonify({"error": "Company not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/companies', methods=['GET'])
def api_db_companies():
    """List available *.db files in the project folder."""
    try:
        files = []
        for fn in os.listdir(os.path.dirname(__file__)):
            if fn.lower().endswith(".db") and os.path.isfile(os.path.join(os.path.dirname(__file__), fn)):
                files.append(fn)
        files = sorted(files, key=lambda x: (x != database_manager.get_default_db_name(), x.lower()))
        return jsonify({"companies": files, "count": len(files), "active_db": session.get("active_db", database_manager.get_default_db_name())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/active', methods=['GET', 'POST'])
def api_db_active():
    if request.method == 'GET':
        return jsonify({"active_db": session.get("active_db", database_manager.get_default_db_name())})
    body = request.get_json(force=True, silent=True) or {}
    db_name = _sanitize_db_filename(body.get("db_name", database_manager.get_default_db_name()))
    session["active_db"] = db_name
    
    # Automatically match and switch the corresponding Zoho API profile
    matched_company = None
    try:
        from modules import company_manager
        companies = company_manager.load_companies()
        
        # 1. Exact match on db_name
        for cid, comp in companies.items():
            if comp.get("db_name", "").lower() == db_name.lower():
                matched_company = comp
                break
                
        # 2. Fuzzy match on company name vs db filename
        if not matched_company:
            db_clean = db_name.lower().replace(".db", "").replace("_", " ")
            for cid, comp in companies.items():
                c_name = comp.get("name", "").lower()
                if c_name in db_clean or db_clean in c_name:
                    matched_company = comp
                    break

        if matched_company:
            company_manager.set_active_company_id(matched_company["id"])
            session["active_company_id"] = matched_company["id"]
    except Exception as e:
        print(f"⚠️ Error auto-linking company to db: {e}")

    if database_manager:
        try:
            database_manager.set_active_db(db_name)
            database_manager.init_db(db_name=db_name)
        except Exception:
            pass
    return jsonify({
        "status": "ok",
        "active_db": db_name,
        "matched_company": matched_company
    })

@app.route('/api/zoho/diag', methods=['GET'])
def api_zoho_diag():
    """Non-sensitive Zoho auth diagnostics (DC/domains + token refresh result)."""
    try:
        from modules.zoho_connector import zoho, ACCOUNTS_DOMAIN, API_DOMAIN
    except Exception as e:
        return jsonify({"error": f"Zoho connector not available: {e}"}), 500

    token = zoho.get_access_token()
    return jsonify({
        "accounts_domain": ACCOUNTS_DOMAIN,
        "api_domain": API_DOMAIN,
        "auth_url": zoho.auth_url,
        "token_ok": bool(token),
        "has_client_id": bool(zoho.client_id),
        "has_client_secret": bool(zoho.client_secret),
        "has_refresh_token": bool(zoho.refresh_token),
        "client_id_tail": (zoho.client_id[-6:] if zoho.client_id else ""),
        "refresh_token_tail": (zoho.refresh_token[-6:] if zoho.refresh_token else ""),
    })

@app.route('/api/zoho/refresh_masters', methods=['POST'])
def api_zoho_refresh_masters():
    """One-click refresh of all Zoho masters (Contacts, COA, Taxes, Payment Terms, Tags) into SQLite DB"""
    try:
        from journel.journel_backend import get_access_token
        from invoice.invoice_backend import refresh_all_zoho_masters
        token = get_access_token()
        if not token:
            return jsonify({"status": "error", "message": "Failed to obtain Zoho access token"}), 400
        
        results = refresh_all_zoho_masters(token)
        return jsonify({"status": "success", "message": "Successfully refreshed all Zoho masters to SQLite cache!", "details": results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/zoho/contacts/<contact_id>/debug', methods=['GET'])
def api_zoho_contact_debug(contact_id):
    """
    Debug helper: fetch a single contact from Zoho Books and return only the fields
    we care about for phone/mobile formatting.
    """
    try:
        from modules.zoho_connector import zoho
    except Exception as e:
        return jsonify({"error": f"Zoho connector not available: {e}"}), 500

    res = zoho.api_call("GET", f"/contacts/{contact_id}")
    if res.get("code") != 0:
        return jsonify({"status": "error", "code": res.get("code"), "message": res.get("message"), "raw": res}), 400

    c = res.get("contact", {}) or {}
    cps = c.get("contact_persons", []) or []
    cps_slim = []
    for cp in cps:
        cps_slim.append({
            "contact_person_id": cp.get("contact_person_id"),
            "is_primary_contact": cp.get("is_primary_contact"),
            "phone": cp.get("phone"),
            "mobile": cp.get("mobile"),
            "email": cp.get("email"),
        })

    out = {
        "contact_id": c.get("contact_id"),
        "contact_name": c.get("contact_name"),
        "contact_type": c.get("contact_type"),
        "gst_treatment": c.get("gst_treatment"),
        "gst_no": c.get("gst_no"),
        "phone": c.get("phone"),
        "mobile": c.get("mobile"),
        "contact_persons": cps_slim,
    }
    return jsonify({"status": "ok", "contact": out})


@app.route('/api/zoho/items/<item_id>/debug', methods=['GET'])
def api_zoho_item_debug(item_id):
    """Debug helper: fetch one item and show tax + custom_fields payload from Zoho."""
    try:
        from modules.zoho_connector import zoho
    except Exception as e:
        return jsonify({"error": f"Zoho connector not available: {e}"}), 500

    res = zoho.api_call("GET", f"/items/{item_id}")
    if res.get("code") != 0:
        return jsonify({"status": "error", "code": res.get("code"), "message": res.get("message"), "raw": res}), 400

    it = res.get("item", {}) or {}
    out = {
        "item_id": it.get("item_id"),
        "name": it.get("name"),
        "tax_id": it.get("tax_id"),
        "tax_name": it.get("tax_name"),
        "item_tax_preferences": it.get("item_tax_preferences"),
        "custom_fields": it.get("custom_fields"),
    }
    return jsonify({"status": "ok", "item": out})



# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

@app.route('/api/db/ledgers', methods=['GET'])
def api_db_ledgers():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        ledgers = database_manager.get_all_ledgers()
        return jsonify({"ledgers": ledgers, "count": len(ledgers)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/import-masters', methods=['POST'])
def api_db_import_masters():
    """
    Import a Tally Master XML (All Masters) into SQLite.
    Supports multi-company by selecting/creating a DB file.
    """
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500

    mf = request.files.get('master_file') or request.files.get('xml_file') or request.files.get('file')
    if not mf or not mf.filename:
        return jsonify({"error": "master_file (.xml) is required"}), 400

    target = (request.form.get('target') or 'existing').strip().lower()  # existing | new
    requested_db = request.form.get('db_name') or request.form.get('company_db') or ''

    xml_bytes = mf.read()
    if not xml_bytes:
        return jsonify({"error": "Empty file"}), 400

    # Extract company name from XML (optional default db name)
    xml_text = ""
    try:
        xml_text = xml_bytes.decode("utf-16", errors="ignore")
    except Exception:
        try:
            xml_text = xml_bytes.decode("utf-8", errors="ignore")
        except Exception:
            xml_text = ""
    m = re.search(r"<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>", xml_text, flags=re.IGNORECASE | re.DOTALL)
    company_from_file = (m.group(1).strip() if m else "")

    if target == "new":
        chosen = requested_db or company_from_file or "new_company"
        db_name = _sanitize_db_filename(chosen)
    else:
        db_name = session.get("active_db", database_manager.get_default_db_name())
        db_name = _sanitize_db_filename(db_name)

    # Set active db for this request + session
    session["active_db"] = db_name
    database_manager.set_active_db(db_name)
    database_manager.init_db(db_name=db_name)
    
    # Clear previous masters data so new upload does not duplicate
    database_manager.clear_all_masters(db_name=db_name)

    # Parse XML masters (ledgers/groups/items)
    try:
        from modules import json_to_zoho_converter as conv
    except Exception as e:
        return jsonify({"error": f"Converter not available: {e}"}), 500

    parsed = conv.parse_tally_json(xml_bytes)
    records = parsed.get("records", []) or []
    ctx = parsed.get("context", {}) or {}
    group_parent_map = ctx.get("group_parent_map", {}) if isinstance(ctx, dict) else {}

    def _safe_float(v):
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return 0.0

    # Resolve primary group by traversing parents
    def _primary_group(group_name: str) -> str:
        if not group_name:
            return ""
        seen = set()
        cur = group_name
        parent = group_parent_map.get(cur, "")
        while parent and parent not in seen:
            seen.add(parent)
            cur = parent
            parent = group_parent_map.get(cur, "")
        return cur

    imported = {"groups": 0, "ledgers": 0, "items": 0, "errors": []}
    seen_groups = set()
    seen_ledgers = set()
    seen_items = set()

    # 1) Groups (use parsed XML context)
    for g in (ctx.get("groups") or []):
        name = (g.get("name") or "").strip()
        if not name:
            continue
        if name in seen_groups:
            continue
        seen_groups.add(name)
        parent = (g.get("parent") or "").strip()
        data = {"name": name, "parent": parent, "primary_group": _primary_group(name)}
        try:
            database_manager.insert_or_update_group(data)
            imported["groups"] += 1
        except Exception as e:
            imported["errors"].append(f"Group '{name}': {e}")

    # 2) Ledgers (Customers/Vendors/Others)
    for r in records:
        if str(r.get("metadata_type", "")).lower() != "ledger":
            continue
        name = (r.get("ContactName") or r.get("name") or "").strip()
        if not name:
            continue
        if name in seen_ledgers:
            continue
        seen_ledgers.add(name)
        parent = (r.get("Under") or r.get("parent") or "").strip()
        ct = str(r.get("ContactType") or "").strip().lower()
        if ct == "customer":
            ltype = "customer"
        elif ct == "vendor":
            ltype = "vendor"
        else:
            ltype = "other"

        data = {
            "name": name,
            "parent": parent,
            "type": ltype,
            "address": (r.get("BillingAddress") or r.get("address") or "").strip(),
            "original_address": (r.get("BillingAddress") or r.get("address") or "").strip(),
            "city": (r.get("BillingCity") or r.get("city") or "").strip(),
            "state": (r.get("BillingState") or r.get("state") or "").strip(),
            "country": (r.get("BillingCountry") or r.get("country") or "").strip(),
            "pincode": (r.get("BillingZip") or r.get("pincode") or "").strip(),
            "email": (r.get("EmailAddress") or r.get("email") or "").strip(),
            "phone": str(r.get("Phone") or r.get("phone") or "").strip(),
            "gstin": (r.get("GSTIN") or r.get("gstin") or "").strip(),
            "gst_reg_type": (r.get("RegistrationType") or r.get("gst_reg_type") or "").strip(),
            "pan": (r.get("PAN") or r.get("pan") or "").strip(),
            "opening_balance": _safe_float(r.get("openingbalance") or r.get("opening_balance") or r.get("OpeningBalance") or 0),
            "closing_balance": _safe_float(r.get("closingbalance") or r.get("closing_balance") or r.get("ClosingBalance") or 0),
        }
        try:
            database_manager.insert_or_update_ledger(data)
            imported["ledgers"] += 1
        except Exception as e:
            imported["errors"].append(f"Ledger '{name}': {e}")

    # 3) Items (if present in Master.xml)
    for r in records:
        if str(r.get("metadata_type", "")).lower() != "stockitem":
            continue
        name = (r.get("ItemName") or r.get("name") or "").strip()
        if not name:
            continue
        if name in seen_items:
            continue
        seen_items.add(name)
        data = {
            "name": name,
            "group_name": (r.get("parent") or r.get("group_name") or r.get("stockgroup") or "").strip(),
            "category": (r.get("category") or "").strip(),
            "unit": (r.get("BASEUNITS") or r.get("baseunits") or r.get("unit") or "").strip(),
            "hsn_source": "",
            "hsn": (r.get("hsn") or r.get("HSN") or r.get("hsncode") or "").strip(),
            "description": (r.get("description") or r.get("Description") or "").strip(),
            "gst_applicable": (r.get("gstapplicable") or r.get("GSTAPPLICABLE") or "").strip(),
            "gst_rate_source": "",
            "gst_rate": _safe_float(r.get("gstrate") or r.get("GST_RATE") or 0),
            "taxability": (r.get("taxability") or "").strip(),
            "supply_type": (r.get("supplytype") or "").strip(),
            "rate_of_duty": _safe_float(r.get("rateofduty") or 0),
            "qty": _safe_float(r.get("qty") or 0),
            "qty_unit": (r.get("qty_unit") or "").strip(),
            "rate": _safe_float(r.get("rate") or 0),
            "rate_unit": (r.get("rate_unit") or "").strip(),
            "value": _safe_float(r.get("value") or 0),
        }
        try:
            database_manager.insert_or_update_item(data)
            imported["items"] += 1
        except Exception as e:
            imported["errors"].append(f"Item '{name}': {e}")

    return jsonify({
        "status": "ok",
        "active_db": db_name,
        "company_from_file": company_from_file,
        "imported": imported,
    })


@app.route('/api/db/import-items-xml', methods=['POST'])
def api_db_import_items_xml():
    """
    Import Items XML (Stock Groups + Stock Items) into SQLite.
    Uses items/items_backend.py parsing so all item fields get populated.
    Supports multi-company by selecting/creating a DB file.
    """
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500
    if not items_module or not hasattr(items_module, "import_items_from_exported_xml"):
        return jsonify({"error": "Items backend not available"}), 500

    mf = request.files.get('items_file') or request.files.get('xml_file') or request.files.get('file')
    if not mf or not mf.filename:
        return jsonify({"error": "items_file (.xml) is required"}), 400

    target = (request.form.get('target') or 'existing').strip().lower()  # existing | new
    requested_db = request.form.get('db_name') or request.form.get('company_db') or ''

    xml_bytes = mf.read()
    if not xml_bytes:
        return jsonify({"error": "Empty file"}), 400

    # Decode XML (best-effort)
    xml_text = ""
    try:
        xml_text = xml_bytes.decode("utf-16", errors="ignore")
    except Exception:
        try:
            xml_text = xml_bytes.decode("utf-8", errors="ignore")
        except Exception:
            xml_text = ""

    # Extract company name from XML (optional default db name)
    m = re.search(r"<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>", xml_text, flags=re.IGNORECASE | re.DOTALL)
    company_from_file = (m.group(1).strip() if m else "")

    if target == "new":
        chosen = requested_db or company_from_file or "new_company"
        db_name = _sanitize_db_filename(chosen)
    else:
        db_name = session.get("active_db", database_manager.get_default_db_name())
        db_name = _sanitize_db_filename(db_name)

    # Set active db for this request + session
    session["active_db"] = db_name
    database_manager.set_active_db(db_name)
    database_manager.init_db(db_name=db_name)

    # Clear previous items data so new upload does not duplicate
    database_manager.clear_all_masters(db_name=db_name, clear_groups=False, clear_ledgers=False, clear_items=True)

    try:
        imported = items_module.import_items_from_exported_xml(xml_text, save_to_db=True)
    except Exception as e:
        return jsonify({"error": f"Failed to import items: {e}"}), 500

    return jsonify({
        "status": "ok",
        "active_db": db_name,
        "company_from_file": company_from_file,
        "imported": imported,
    })

@app.route('/api/db/items', methods=['GET'])
def api_db_items():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        items = database_manager.get_all_items()
        return jsonify({"items": items, "count": len(items)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/groups', methods=['GET'])
def api_db_groups():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        groups = database_manager.get_all_groups()
        return jsonify({"groups": groups, "count": len(groups)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/cost-categories', methods=['GET'])
def api_db_cost_categories():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        data = database_manager.get_all_cost_categories()
        return jsonify({"categories": data, "count": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/cost-centres', methods=['GET'])
def api_db_cost_centres():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        data = database_manager.get_all_cost_centres()
        return jsonify({"centres": data, "count": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# OPENING BALANCE — Upload Tally file → Download Zoho Excel
# ---------------------------------------------------------

@app.route('/opening-balance')
def opening_balance_page():
    return render_template('opening_balance.html')

@app.route('/api/opening-balance/convert', methods=['POST'])
def api_convert_opening_balance():
    """
    Single-file upload: tally_file
    ?preview=1  → JSON summary + preview rows
    ?preview=0  → Excel file download
    """
    try:
        from opening_balance_converter import convert
        import io

        tally_f = request.files.get('tally_file')
        if not tally_f or not tally_f.filename:
            return jsonify({"error": "Tally file is required"}), 400

        tally_bytes    = tally_f.read()
        migration_date = request.form.get('migration_date', None)
        preview_only   = request.args.get('preview', '1') == '1'

        # Fetch existing Groups from DB to filter out Group Headers
        db_group_names = []
        if database_manager:
            try:
                # get_all_groups returns list of dicts: [{'name': 'X'}, ...]
                all_groups = database_manager.get_all_groups()
                db_group_names = [g['name'] for g in all_groups if g.get('name')]
            except Exception as e:
                print(f"Warning: Failed to fetch groups for filtering: {e}")

        output_bytes, summary, errors = convert(
            tally_bytes, tally_f.filename, migration_date, db_group_names
        )

        if output_bytes is None:
            return jsonify({"error": errors[0] if errors else "Conversion failed"}), 400

        if preview_only:
            return jsonify({"status": "ok", "summary": summary, "errors": errors})
        else:
            return send_file(
                io.BytesIO(output_bytes),
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name='zoho_opening_balance.xlsx'
            )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route('/api/cost-centers/fetch', methods=['GET'])
def api_fetch_cost_centers():
    try:
        from cost_centers import cost_center_backend
        data = cost_center_backend.get_all_cost_data()
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cost-centers/sync-reporting-tags', methods=['POST'])
def api_sync_reporting_tags():
    try:
        from cost_centers import cost_center_backend
        result = cost_center_backend.sync_reporting_tags_to_zoho()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/')
def index():
    return render_template('ledgers.html')

@app.route('/ledgers')
def ledgers_page():
    return render_template('ledgers.html')

@app.route('/items')
def items_page():
    return render_template('items.html')

# ---------------------------------------------------------
# API ENDPOINTS
# ---------------------------------------------------------

@app.route('/api/ledgers/fetch', methods=['GET'])
def api_fetch_ledgers():
    try:
        data = ledgers_module.analyze_ledgers_and_groups()
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch data from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/items/fetch', methods=['GET'])
def api_fetch_items():
    try:
        data = items_module.get_all_items_data()
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch items from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ledgers/sync_zoho', methods=['POST'])
def api_sync_ledgers(): 
    try:
        selected = request.json.get("ledgers") if request.is_json else None
        update_existing = request.json.get("update_existing", False) if request.is_json else False
        result = ledgers_module.sync_ledgers_to_zoho(selected, update_existing=update_existing)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ledgers/sync_customers', methods=['POST'])
def api_sync_customers():
    """Sync ONLY customers to Zoho Books."""
    try:
        selected = request.json.get("ledgers") if request.is_json else None
        update_existing = request.json.get("update_existing", True) if request.is_json else True
        result = ledgers_module.sync_ledgers_to_zoho(selected, contact_type_filter='customer', update_existing=update_existing)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ledgers/sync_customers/start', methods=['POST'])
def api_sync_customers_start():
    """Start async streaming sync for customers with live console logs."""
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    job = job_manager.create("sync_customers")
    job.log("Customers synchronization job initialized.")
    selected = request.json.get("ledgers") if request.is_json else None
    update_existing = request.json.get("update_existing", True) if request.is_json else True
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)
        try:
            res = ledgers_module.sync_ledgers_to_zoho(selected, contact_type_filter='customer', update_existing=update_existing, log=job.log)
            st = (res or {}).get("status")
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            import traceback
            traceback.print_exc()
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    import threading
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/ledgers/sync_vendors', methods=['POST'])
def api_sync_vendors():
    """Sync ONLY vendors to Zoho Books."""
    try:
        selected = request.json.get("ledgers") if request.is_json else None
        update_existing = request.json.get("update_existing", True) if request.is_json else True
        result = ledgers_module.sync_ledgers_to_zoho(selected, contact_type_filter='vendor', update_existing=update_existing)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ledgers/sync_vendors/start', methods=['POST'])
def api_sync_vendors_start():
    """Start async streaming sync for vendors with live console logs."""
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    job = job_manager.create("sync_vendors")
    job.log("Vendors synchronization job initialized.")
    selected = request.json.get("ledgers") if request.is_json else None
    update_existing = request.json.get("update_existing", True) if request.is_json else True
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)
        try:
            res = ledgers_module.sync_ledgers_to_zoho(selected, contact_type_filter='vendor', update_existing=update_existing, log=job.log)
            st = (res or {}).get("status")
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            import traceback
            traceback.print_exc()
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    import threading
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/ledgers/save_mapping', methods=['POST'])
def api_save_group_mapping():
    try:
        mapping = request.json.get("mapping") if request.is_json else {}
        ledgers_module.save_groups_mapping(mapping)
        return jsonify({"status": "success", "message": "Mapping saved successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ledgers/get_mapping', methods=['GET'])
def api_get_group_mapping():
    try:
        mapping = ledgers_module.get_groups_mapping()
        return jsonify(mapping)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ledgers/execute_group_sync', methods=['POST'])
def api_execute_group_sync():
    try:
        body = request.get_json(silent=True) or {}
        selected_ledgers = body.get("ledgers")
        result = ledgers_module.sync_groups_to_zoho(None, selected_ledgers=selected_ledgers)
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ledgers/execute_group_sync/start', methods=['POST'])
def api_execute_group_sync_start():
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(silent=True) or {}
    selected_ledgers = body.get("ledgers")

    job = job_manager.create("group_sync_zoho")
    job.log(f"Group & Ledger sync job started{' (Selected Ledgers: ' + str(len(selected_ledgers)) + ')' if selected_ledgers else ''}.")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")
    
    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)
        try:
            res = ledgers_module.sync_groups_to_zoho(None, log=job.log, selected_ledgers=selected_ledgers)
            st = (res or {}).get("status")
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            import traceback
            traceback.print_exc()
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    import threading
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})



@app.route('/api/ledgers/create_standalone', methods=['POST'])
def api_create_standalone():
    try:
        ledger_name = request.json.get("ledger_name") if request.is_json else None
        account_type = request.json.get("account_type") if request.is_json else None
        if not ledger_name or not account_type:
            return jsonify({"status": "error", "message": "ledger_name and account_type are required"}), 400
        result = ledgers_module.create_standalone_account(ledger_name, account_type)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/sync_zoho', methods=['POST'])
def api_sync_items():
    try:
        selected = request.json.get("items") if request.is_json else None
        result = items_module.sync_items_to_zoho(selected)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/sync_zoho/start', methods=['POST'])
def api_items_sync_zoho_start():
    if not items_module:
        return jsonify({"status": "error", "message": "Items backend not available"}), 500
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    selected = body.get("items")

    job = job_manager.create("items_sync_zoho")
    job.log("Items sync job started.")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            res = items_module.sync_items_to_zoho(selected, log=job.log, stop_event=job.stop_event)
            st = (res or {}).get("status")
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})


@app.route('/api/jobs/<job_id>', methods=['GET'])
def api_job_status(job_id):
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500
    job = job_manager.get(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found"}), 404
    return jsonify({"status": "success", "job": job.snapshot(), "result": job.result if job.status != "running" else None})


@app.route('/api/jobs/<job_id>/stop', methods=['POST'])
def api_job_stop(job_id):
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500
    ok = job_manager.stop(job_id)
    if not ok:
        return jsonify({"status": "error", "message": "Job not found"}), 404
    return jsonify({"status": "success"})


@app.route('/api/jobs/<job_id>/stream', methods=['GET'])
def api_job_stream(job_id):
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500
    job = job_manager.get(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found"}), 404

    def _gen():
        last_seq = 0
        try:
            last_seq = int((request.args.get("from") or "0").strip() or 0)
        except Exception:
            last_seq = 0

        yield sse_format("status", job.snapshot())

        while True:
            entries = job.get_logs_since(last_seq)
            for seq, ts, msg in entries:
                last_seq = seq
                yield sse_format("log", {"seq": seq, "ts": ts, "message": msg})

            if job.status != "running":
                yield sse_format("done", {"job": job.snapshot(), "result": job.result})
                break

            job.wait(timeout=1.5)
            yield ": keepalive\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(_gen()), headers=headers, mimetype="text/event-stream")


@app.route('/api/items/inventory_adjustment/preview', methods=['POST'])
def api_items_inventory_adjustment_preview():
    """
    Upload Excel (opening stock totals) + XML (godown/warehouse summary) and return:
    - items to apply (non-negative)
    - negative items report
    """
    try:
        excel_file = request.files.get("excel_file")
        xml_file = request.files.get("xml_file")
        if not excel_file or not xml_file:
            return jsonify({"status": "error", "message": "excel_file and xml_file are required"}), 400

        upload_dir = os.path.join(os.path.dirname(__file__), "uploads", "inventory_adjustment")
        os.makedirs(upload_dir, exist_ok=True)

        ts = str(int(time.time()))
        excel_path = os.path.join(upload_dir, f"opening_{ts}.xlsx")
        xml_path = os.path.join(upload_dir, f"godown_{ts}.xml")
        excel_file.save(excel_path)
        xml_file.save(xml_path)

        excel_map = items_module.parse_opening_excel_xlsx(excel_path)
        xml_map = items_module.parse_godown_xml(xml_path)
        to_apply, negative, stats = items_module.compute_inventory_adjustment(excel_map, xml_map)

        # Store last preview file paths in session for apply
        session["inv_adj_excel_path"] = excel_path
        session["inv_adj_xml_path"] = xml_path
        session["inv_adj_run_id"] = ts

        return jsonify({
            "status": "success",
            "stats": stats,
            "to_apply_count": len(to_apply),
            "negative_count": len(negative),
            "negative_preview": negative[:200],
            "apply_preview": to_apply[:50],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/inventory_adjustment/apply', methods=['POST'])
def api_items_inventory_adjustment_apply():
    """
    Apply the latest preview (stored in session) to Zoho.
    """
    try:
        dry_run = str(request.form.get("dry_run", "0")).strip() in ("1", "true", "yes", "on")
        resume = str(request.form.get("resume", "1")).strip() in ("1", "true", "yes", "on")
        limit_raw = (request.form.get("limit") or "").strip()
        limit = 0
        try:
            limit = int(limit_raw) if limit_raw else 0
        except Exception:
            limit = 0
        excel_path = session.get("inv_adj_excel_path")
        xml_path = session.get("inv_adj_xml_path")
        run_id = session.get("inv_adj_run_id") or ""
        if not excel_path or not xml_path or (not os.path.exists(excel_path)) or (not os.path.exists(xml_path)):
            return jsonify({"status": "error", "message": "No preview found. Run Preview first."}), 400

        excel_map = items_module.parse_opening_excel_xlsx(excel_path)
        xml_map = items_module.parse_godown_xml(xml_path)
        to_apply, negative, stats = items_module.compute_inventory_adjustment(excel_map, xml_map)

        # Apply only non-negative items (optional limit for testing)
        if limit and limit > 0:
            to_apply = to_apply[:limit]
        result = items_module.apply_inventory_adjustment_to_zoho(to_apply, dry_run=dry_run, run_id=run_id, resume=resume)
        result["stats"] = stats
        result["negative_count"] = len(negative)
        result["negative_preview"] = negative[:200]
        result["applied_limit"] = limit or 0
        result["resume"] = bool(resume)
        result["run_id"] = run_id

        # Build a run report (xlsx) and store path in session for download
        try:
            from openpyxl import Workbook
            wb = Workbook()

            # Summary sheet
            ws = wb.active
            ws.title = "Summary"
            ws.append(["Key", "Value"])
            ws.append(["xml_items", stats.get("xml_items")])
            ws.append(["excel_items", stats.get("excel_items")])
            ws.append(["to_apply", stats.get("to_apply")])
            ws.append(["negative", stats.get("negative")])
            ws.append(["applied_limit", limit or 0])
            ws.append(["dry_run", bool(dry_run)])

            res_core = (result.get("results") or {})
            ws.append(["updated", res_core.get("updated", 0)])
            ws.append(["failed", res_core.get("failed", 0)])
            ws.append(["missing_item_id", res_core.get("missing_item_id", 0)])
            ws.append(["skipped", res_core.get("skipped", 0)])

            # Updated sheet
            ws_u = wb.create_sheet("Updated")
            ws_u.append(["Item Name", "Zoho Item ID"])
            for it in (res_core.get("updated_items") or []):
                ws_u.append([it.get("name", ""), it.get("item_id", "")])

            # Skipped sheet (includes missing_item_id + dry_run)
            ws_s = wb.create_sheet("Skipped")
            ws_s.append(["Item Name", "Reason", "Zoho Item ID"])
            for it in (res_core.get("skipped_items") or []):
                ws_s.append([it.get("name", ""), it.get("reason", ""), it.get("item_id", "")])

            # Errors sheet
            ws_e = wb.create_sheet("Errors")
            ws_e.append(["Item Name", "Reason"])
            for er in (res_core.get("errors") or []):
                ws_e.append([er.get("name", ""), er.get("reason", "")])

            # Negative sheet
            ws_n = wb.create_sheet("Negative")
            ws_n.append(["Item Name", "Reason", "Excel Qty", "XML Main Qty", "XML Other Qty", "Needed Main Qty"])
            for r in negative:
                ws_n.append([
                    r.get("name", ""),
                    r.get("reason", ""),
                    r.get("excel_qty", ""),
                    r.get("xml_main_qty", ""),
                    r.get("xml_other_qty", ""),
                    r.get("needed_main_qty", ""),
                ])

            upload_dir = os.path.join(os.path.dirname(__file__), "uploads", "inventory_adjustment")
            os.makedirs(upload_dir, exist_ok=True)
            ts2 = str(int(time.time()))
            report_path = os.path.join(upload_dir, f"inv_adj_run_report_{ts2}.xlsx")
            wb.save(report_path)
            session["inv_adj_last_run_report"] = report_path
            result["run_report_url"] = "/api/items/inventory_adjustment/last_run_report.xlsx"
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/inventory_adjustment/last_run_report.xlsx', methods=['GET'])
def api_items_inventory_adjustment_last_run_report():
    """Download the last Inventory Adjustment apply run report (xlsx)."""
    try:
        p = session.get("inv_adj_last_run_report")
        if not p or not os.path.exists(p):
            return jsonify({"status": "error", "message": "No run report found. Run Apply first."}), 400
        return send_file(
            p,
            as_attachment=True,
            download_name="inventory_adjustment_run_report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/inventory_adjustment/negative_report.xlsx', methods=['GET'])
def api_items_inventory_adjustment_negative_report():
    """
    Download full negative report as Excel (based on last preview stored in session).
    """
    try:
        excel_path = session.get("inv_adj_excel_path")
        xml_path = session.get("inv_adj_xml_path")
        if not excel_path or not xml_path or (not os.path.exists(excel_path)) or (not os.path.exists(xml_path)):
            return jsonify({"status": "error", "message": "No preview found. Run Preview first."}), 400

        excel_map = items_module.parse_opening_excel_xlsx(excel_path)
        xml_map = items_module.parse_godown_xml(xml_path)
        _to_apply, negative, _stats = items_module.compute_inventory_adjustment(excel_map, xml_map)

        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Negative Items"
        ws.append(["Item Name", "Reason", "Excel Qty", "XML Main Qty", "XML Other Qty", "Needed Main Qty"])
        for r in negative:
            ws.append([
                r.get("name", ""),
                r.get("reason", ""),
                r.get("excel_qty", ""),
                r.get("xml_main_qty", ""),
                r.get("xml_other_qty", ""),
                r.get("needed_main_qty", ""),
            ])

        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return send_file(
            bio,
            as_attachment=True,
            download_name="negative_stock_report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/inventory_adjustment/matched_report.xlsx', methods=['GET'])
def api_items_inventory_adjustment_matched_report():
    """
    Download matched (Excel+XML) report as Excel (based on last preview stored in session).
    """
    try:
        excel_path = session.get("inv_adj_excel_path")
        xml_path = session.get("inv_adj_xml_path")
        if not excel_path or not xml_path or (not os.path.exists(excel_path)) or (not os.path.exists(xml_path)):
            return jsonify({"status": "error", "message": "No preview found. Run Preview first."}), 400

        excel_map = items_module.parse_opening_excel_xlsx(excel_path)
        xml_map = items_module.parse_godown_xml(xml_path)
        matched = items_module.build_matched_items_report(excel_map, xml_map)

        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Matched Items"
        ws.append([
            "Item Name",
            "Status",
            "Excel Qty",
            "Excel Rate",
            "XML Main Qty",
            "XML Other Qty",
            "Needed Main Qty",
            "ExcelQty + XMLMainQty",
        ])
        for r in matched:
            ws.append([
                r.get("name", ""),
                r.get("status", ""),
                r.get("excel_qty", 0),
                r.get("excel_rate", 0),
                r.get("xml_main_qty", 0),
                r.get("xml_other_qty", 0),
                r.get("needed_main_qty", 0),
                r.get("sum_qty", 0),
            ])

        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return send_file(
            bio,
            as_attachment=True,
            download_name="matched_items_report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/godown_xml/to_transfer_excel.xlsx', methods=['POST'])
def api_items_godown_xml_to_transfer_excel():
    """
    Upload a Godown Summary XML (MAIN warehouse export) and download an Excel file
    in the same 4-column format as 'Stocks Transferred to Main warehouse.xlsx'.
    """
    try:
        xml_file = request.files.get("xml_file")
        if not xml_file:
            return jsonify({"status": "error", "message": "xml_file is required"}), 400

        upload_dir = os.path.join(os.path.dirname(__file__), "uploads", "inventory_adjustment")
        os.makedirs(upload_dir, exist_ok=True)
        ts = str(int(time.time()))
        xml_path = os.path.join(upload_dir, f"godown_transfer_{ts}.xml")
        xml_file.save(xml_path)

        xml_map = items_module.parse_godown_xml(xml_path)
        wb = items_module.build_stock_transfer_workbook_from_godown_xml(xml_map)

        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return send_file(
            bio,
            as_attachment=True,
            download_name="stocks_transferred_to_main_warehouse.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/items/sync_map/refresh_from_zoho', methods=['POST'])
def api_items_refresh_sync_map_from_zoho():
    """
    Build local Zoho item_id map by fetching Zoho items list (no create/update).
    This makes Inventory Adjustment Apply work without re-running full Items sync.
    """
    try:
        if not items_module or not hasattr(items_module, "refresh_zoho_item_sync_map_from_zoho"):
            return jsonify({"status": "error", "message": "Items module not available"}), 500
        result = items_module.refresh_zoho_item_sync_map_from_zoho()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────────────────────
#  FIELD MAPPING  –  Items
# ─────────────────────────────────────────────────────────────
MODULE_DB_FIELDS = {
    "customers": [
        "name", "parent", "type", "address", "original_address", "city", "state", "country", "pincode",
        "email", "phone", "gstin", "gst_reg_type", "pan",
        "opening_balance", "closing_balance", "description"
    ],
    "vendors": [
        "name", "parent", "type", "address", "original_address", "city", "state", "country", "pincode",
        "email", "phone", "gstin", "gst_reg_type", "pan",
        "opening_balance", "closing_balance", "description"
    ],
    "items": [
        "name", "group_name", "category", "unit",
        "hsn_source", "hsn", "description",
        "gst_applicable", "gst_rate_source", "gst_rate",
        "taxability", "supply_type", "rate_of_duty",
        "qty", "qty_unit", "rate", "rate_unit", "value"
    ],
    "bills": [
        "voucher_number", "date", "vendor_name", "total_amount",
        "tax_amount", "payment_status", "due_date", "reference_number", "narration"
    ],
    "invoices": [
        "voucher_number", "date", "customer_name", "total_amount",
        "tax_amount", "payment_status", "due_date", "reference_number", "narration"
    ],
    "journals": [
        "voucher_number", "date", "narration", "debit_account",
        "credit_account", "amount", "reference"
    ],
    "receipts": [
        "voucher_number", "date", "party_name", "amount",
        "payment_mode", "reference_number", "narration", "bank_account"
    ],
    "payments_made": [
        "voucher_number", "date", "party_name", "amount",
        "payment_mode", "reference_number", "narration", "bank_account"
    ],
    "sales_orders": [
        "voucher_number", "date", "customer_name", "total_amount",
        "reference_number", "status", "narration"
    ],
    "purchase_orders": [
        "voucher_number", "date", "vendor_name", "total_amount",
        "reference_number", "status", "narration"
    ],
    "contra": [
        "voucher_number", "date", "from_account", "to_account",
        "amount", "narration", "reference_number"
    ],
    "credit_note": [
        "voucher_number", "date", "customer_name", "total_amount",
        "reference_number", "narration"
    ],
    "debit_note": [
        "voucher_number", "date", "vendor_name", "total_amount",
        "reference_number", "narration"
    ],
    "opening_balance": [
        "account_name", "group_name", "opening_balance",
        "balance_type", "date"
    ],
}


@app.route('/api/field-mapping/<module>/upload-zoho-fields', methods=['POST'])
def api_upload_zoho_fields(module):
    """Upload a Zoho Books XLSX sample file; extract column headers as Zoho field names."""
    if openpyxl is None:
        return jsonify({"error": "openpyxl not installed. Run: pip install openpyxl"}), 500
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    filename = (file.filename or "").lower()
    try:
        raw = file.read()
        headers = []

        if filename.endswith('.csv'):
            import csv as _csv
            # Try UTF-8-BOM first (Excel default), fall back to latin-1
            for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            reader = _csv.reader(io.StringIO(text))
            first_row = next(reader, [])
            headers = [h.strip() for h in first_row if h.strip()]
        else:
            # Default: treat as XLSX
            if openpyxl is None:
                return jsonify({"error": "openpyxl not installed. Run: pip install openpyxl"}), 500
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            ws = wb.active
            for cell in next(ws.iter_rows(max_row=1)):
                val = str(cell.value).strip() if cell.value is not None else ""
                if val:
                    headers.append(val)

        if not headers:
            return jsonify({"error": "No column headers found in the uploaded file"}), 400
        field_mapping_manager.save_zoho_fields(module, headers)
        return jsonify({"status": "ok", "zoho_fields": headers, "count": len(headers)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def flatten_dict(d, parent_key='', sep='.'):
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    items.extend(flatten_dict(item, f"{new_key}[{i}]", sep=sep).items())
                else:
                    items.append((f"{new_key}[{i}]", str(item)))
        else:
            items.append((new_key, str(v) if v is not None else ""))
    return dict(items)

@app.route('/api/field-mapping/<module>/fetch-zoho-contact', methods=['POST'])
def api_fetch_zoho_contact(module):
    """Fetch a Zoho contact by Name or ID, flatten its JSON to extract fields and sample values."""
    from modules.zoho_connector import zoho
    body = request.get_json(force=True, silent=True) or {}
    identifier = str(body.get("identifier", "")).strip()
    if not identifier:
        return jsonify({"error": "No identifier (Name or ID) provided"}), 400

    contact_id = None
    
    # Check if identifier looks like a Zoho ID (digits only, usually 19 digits)
    if identifier.isdigit() and len(identifier) > 10:
        contact_id = identifier
    else:
        # Search by name
        res = zoho.api_call("GET", "/contacts", params={"search_text": identifier})
        if res.get("code") != 0:
            return jsonify({"error": f"Failed to search Zoho API: {res.get('message')}"}), 500
        contacts = res.get("contacts", [])
        if not contacts:
            return jsonify({"error": f"No contact found in Zoho with name matching '{identifier}'"}), 404
        # Get exact match if possible, otherwise first result
        match = next((c for c in contacts if c.get("contact_name", "").lower() == identifier.lower()), contacts[0])
        contact_id = match.get("contact_id")

    if not contact_id:
        return jsonify({"error": "Could not determine Contact ID"}), 400

    # Fetch full contact details
    res = zoho.api_call("GET", f"/contacts/{contact_id}")
    if res.get("code") != 0:
        return jsonify({"error": f"Failed to fetch contact details: {res.get('message')}"}), 500
    
    contact_data = res.get("contact", {})
    if not contact_data:
        return jsonify({"error": "Empty contact data returned from Zoho"}), 500

    # Flatten the JSON
    flattened = flatten_dict(contact_data)
    
    # Exclude complex lists or internal metadata if desired, but flatten_dict handles lists now
    fields = list(flattened.keys())
    
    # Save the fields and the sample values
    field_mapping_manager.save_zoho_fields(module, fields, sample_values=flattened)
    
    return jsonify({
        "status": "ok",
        "zoho_fields": fields,
        "zoho_sample_values": flattened,
        "count": len(fields),
        "contact_name": contact_data.get("contact_name", identifier)
    })


@app.route('/api/field-mapping/<module>/zoho-fields', methods=['GET'])
def api_get_zoho_fields(module):
    """Return saved Zoho field names for a module."""
    data = field_mapping_manager.load(module)
    return jsonify({
        "zoho_fields": data.get("zoho_fields", []),
        "has_fields": bool(data.get("zoho_fields")),
    })


@app.route('/api/field-mapping/<module>/db-fields', methods=['GET'])
def api_get_db_fields(module):
    """Return our local DB field names and sample values for a module."""
    fields = MODULE_DB_FIELDS.get(module, [])
    
    sample_values = {}
    if database_manager:
        try:
            if module == "customers":
                ledgers = database_manager.get_all_ledgers()
                for l in ledgers:
                    if l.get("type") == "customer":
                        sample_values = l
                        break
            elif module == "vendors":
                ledgers = database_manager.get_all_ledgers()
                for l in ledgers:
                    if l.get("type") == "vendor":
                        sample_values = l
                        break
            elif module == "items":
                items = database_manager.get_all_items()
                if items:
                    sample_values = items[0]
            # (Add vouchers here if needed later)
        except Exception:
            pass

    # Ensure all values are strings for easy display
    sample_values_str = {str(k): str(v) for k, v in sample_values.items() if v is not None}
    
    return jsonify({
        "db_fields": fields,
        "db_sample_values": sample_values_str
    })


@app.route('/api/field-mapping/<module>/mapping', methods=['GET'])
def api_get_mapping(module):
    """Return saved mapping for a module."""
    data = field_mapping_manager.load(module)
    return jsonify({
        "mapping": data.get("mapping", {}),
        "zoho_fields": data.get("zoho_fields", []),
        "zoho_sample_values": data.get("zoho_sample_values", {}),
    })


@app.route('/api/field-mapping/<module>/mapping', methods=['POST'])
def api_save_mapping(module):
    """Save DB→Zoho field mapping."""
    body = request.get_json(force=True) or {}
    mapping = body.get("mapping", {})
    if not mapping:
        return jsonify({"error": "mapping object is required"}), 400
    field_mapping_manager.save_mapping(module, mapping)
    return jsonify({"status": "ok", "saved": len(mapping)})


@app.route('/api/field-mapping/<module>/export', methods=['GET'])
def api_export_mapped(module):
    """Export DB records as an XLSX formatted with Zoho Books column headers based on saved mapping."""
    if openpyxl is None:
        return jsonify({"error": "openpyxl not installed"}), 500
    data = field_mapping_manager.load(module)
    mapping = data.get("mapping", {})   # {zoho_field: db_field}
    if not mapping:
        return jsonify({"error": "No field mapping saved yet. Please map fields first."}), 400
    # Fetch records based on module
    records = []
    try:
        if module == "items" and database_manager:
            records = database_manager.get_all_items()
        elif module == "bills" and database_manager:
            records = database_manager.get_all_vouchers("purchase")
        elif module == "invoices" and database_manager:
            records = database_manager.get_all_vouchers("sales")
        elif module == "journals" and database_manager:
            records = database_manager.get_all_vouchers("journal")
        elif module == "receipts" and database_manager:
            records = database_manager.get_all_vouchers("receipt")
        elif module == "payments_made" and database_manager:
            records = database_manager.get_all_vouchers("payment")
        elif module == "sales_orders" and database_manager:
            records = database_manager.get_all_vouchers("sales order")
        elif module == "purchase_orders" and database_manager:
            records = database_manager.get_all_vouchers("purchase order")
        elif module == "contra" and database_manager:
            records = database_manager.get_all_vouchers("contra")
        elif module == "credit_note" and database_manager:
            records = database_manager.get_all_vouchers("credit note")
        elif module == "debit_note" and database_manager:
            records = database_manager.get_all_vouchers("debit note")
    except Exception as e:
        records = []

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = module.replace("_", " ").title()

    # zoho_fields order as columns
    zoho_fields = list(mapping.keys())
    
    # Append 'Full Address' to the headers
    headers = zoho_fields + ["Full Address"]
    ws.append(headers)   # header row

    for rec in records:
        row = []
        if isinstance(rec, dict):
            for zf in zoho_fields:
                db_col = mapping.get(zf, "")
                row.append(rec.get(db_col, ""))
            # Always append the full address at the end
            row.append(rec.get("original_address") or rec.get("address", ""))
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"{module}_zoho_import.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# Journal routes
@app.route('/journals')
def journals_page():
    return render_template('journals.html')

@app.route('/api/journals/fetch', methods=['POST'])
def api_fetch_journals():
    try:
        # Get date range from request
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        data = journel_module.get_all_journals_data(from_date, to_date, limit)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch journals from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/journals/sync_zoho', methods=['POST'])
def api_sync_journals():
    try:
        selected = request.json.get("journals") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        result = journel_module.sync_journals_to_zoho(selected, from_date, to_date, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/journals/sync_zoho/start', methods=['POST'])
def api_journals_sync_zoho_start():
    """Background SSE job runner for Journals Sync with live terminal stream."""
    if not journel_module:
        return jsonify({"status": "error", "message": "Journal backend not available"}), 500
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    selected = body.get("journals")
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")

    job = job_manager.create("journals_sync_zoho")
    job.log("Journals sync job started.")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")
    
    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            res = journel_module.sync_journals_to_zoho(selected, from_date, to_date, limit, log=job.log, stop_event=job.stop_event)
            st = (res or {}).get("status")
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/journals/mark_synced', methods=['POST'])
def api_journals_mark_synced():
    """Manual tick/sync endpoint to mark journals as Synced or Pending in SQLite DB."""
    try:
        from datetime import datetime
        data = request.json or {}
        journal_numbers = data.get("journal_numbers", [])
        status = str(data.get("status", "synced")).lower()
        zoho_id = "MANUALLY_SYNCED" if status == "synced" else None
        
        if not journal_numbers:
            return jsonify({"error": "No journals provided to update."}), 400
            
        if database_manager:
            database_manager.init_db()
            conn = database_manager.get_db_connection(write=True)
            cur = conn.cursor()
            updated_count = 0
            for j_no in journal_numbers:
                cur.execute(
                    "UPDATE journals SET zoho_journal_id = ?, zoho_status = ?, updated_at = ? WHERE journal_number = ?",
                    (zoho_id, status, datetime.now().isoformat(), str(j_no))
                )
                updated_count += cur.rowcount
            
            conn.commit()
            return jsonify({
                "status": "success",
                "message": f"Successfully marked {updated_count} journal(s) as {status.capitalize()}.",
                "updated_count": updated_count,
                "target_status": status
            })
            
        return jsonify({"error": "Database manager not available."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/journals/upload', methods=['POST'])
def api_upload_journals():
    """
    Upload a Tally-exported JSON file for journals (offline mode).
    Parses the file using journel_backend.parse_tally_json,
    saves to SQLite, and returns the journals list + stats.
    """
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        import tempfile, os
        from datetime import datetime as _dt

        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            # Parse using existing journel_backend parser
            parsed = journel_module.parse_tally_json(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not parsed:
            return jsonify({"error": "No journals found in the uploaded JSON/XML file. Make sure it is a Tally Journal voucher export."}), 400

        # Convert parse_tally_json output (ledger_entries) to the format
        # expected by the UI (line_items with debit_or_credit field)
        def _get_type(lname):
            if database_manager:
                rec = database_manager.get_ledger_by_name(lname)
                if rec and rec.get("type") in ["vendor", "customer"]:
                    return rec.get("type")
            return "account"

        journals_out = []
        for j in parsed:
            line_items = []
            for entry in j.get("ledger_entries", []):
                debit  = float(entry.get("debit",  0) or 0)
                credit = float(entry.get("credit", 0) or 0)

                if debit > 0:
                    lname = entry.get("ledger_name", "")
                    line_items.append({
                        "ledger_name":    lname,
                        "ledger_type":    _get_type(lname),
                        "amount":         debit,
                        "debit_or_credit":"debit",
                        "bill_allocations": entry.get("bill_allocations", []),
                        "tag_category":   "",
                        "tag_option":     ""
                    })
                if credit > 0:
                    lname = entry.get("ledger_name", "")
                    line_items.append({
                        "ledger_name":    lname,
                        "ledger_type":    _get_type(lname),
                        "amount":         credit,
                        "debit_or_credit":"credit",
                        "bill_allocations": entry.get("bill_allocations", []),
                        "tag_category":   "",
                        "tag_option":     ""
                    })

            journals_out.append({
                "date":           j.get("date", ""),
                "journal_number": j.get("journal_number", ""),
                "narration":      j.get("narration", ""),
                "tally_guid":     j.get("tally_guid", ""),
                "voucher_type":   j.get("voucher_type", "Journal"),
                "line_items":     line_items,
                "cost_center_allocations": j.get("cost_center_allocations", [])
            })

        # ── Save to SQLite so the DB tab also shows them ──────────────────
        if database_manager and journals_out:
            now = _dt.now().isoformat()
            db_data_list = []
            for jrnl in journals_out:
                td = sum(i["amount"] for i in jrnl["line_items"] if i["debit_or_credit"] == "debit")
                tc = sum(i["amount"] for i in jrnl["line_items"] if i["debit_or_credit"] == "credit")
                db_data_list.append({
                    "journal_number": jrnl["journal_number"],
                    "date":           jrnl["date"],
                    "narration":      jrnl["narration"],
                    "total_debit":    round(td, 2),
                    "total_credit":   round(tc, 2),
                    "line_items":     json.dumps(jrnl["line_items"]),
                    "from_date":      "",
                    "to_date":        "",
                    "created_at":     now,
                    "updated_at":     now,
                })
            try:
                database_manager.bulk_save_journals(db_data_list)
            except AttributeError:
                pass  # bulk_save_journals may not exist on older DB manager

        total_debit  = sum(
            i["amount"] for j in journals_out for i in j["line_items"]
            if i["debit_or_credit"] == "debit"
        )
        total_credit = sum(
            i["amount"] for j in journals_out for i in j["line_items"]
            if i["debit_or_credit"] == "credit"
        )

        return jsonify({
            "journals": journals_out,
            "stats": {
                "total_journals": len(journals_out),
                "total_debit":    round(total_debit,  2),
                "total_credit":   round(total_credit, 2),
            }
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route('/api/journals/reconciliation/account_wise', methods=['GET'])
def api_journals_account_wise_reconciliation():
    """
    Account-wise / Ledger-wise reconciliation for journals comparing Total Debits,
    Total Credits, Net Difference, and entry counts per account (with optional month filtering).
    """
    try:
        from collections import defaultdict
        import json

        month_param = request.args.get('month', '').strip()
        search_param = request.args.get('search', '').strip().lower()

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_journals = database_manager.get_all_journals()

        account_map = defaultdict(lambda: {
            "ledger_name": "",
            "ledger_type": "account",
            "total_debit": 0.0,
            "total_credit": 0.0,
            "net_difference": 0.0,
            "entry_count": 0,
            "journals": []
        })

        def _get_norm_date(d_str):
            if not d_str: return ""
            d_str = str(d_str).strip()
            if "-" in d_str:
                parts = d_str.split("-")
                if len(parts) == 3:
                    if len(parts[0]) == 4: return parts[0] + parts[1].zfill(2) + parts[2].zfill(2)
                    if len(parts[2]) == 4: return parts[2] + parts[1].zfill(2) + parts[0].zfill(2)
            if "/" in d_str:
                parts = d_str.split("/")
                if len(parts) == 3:
                    if len(parts[2]) == 4: return parts[2] + parts[1].zfill(2) + parts[0].zfill(2)
                    if len(parts[0]) == 4: return parts[0] + parts[1].zfill(2) + parts[2].zfill(2)
            return "".join(filter(str.isdigit, d_str))

        for j in raw_journals:
            j_date = _get_norm_date(j.get("date", ""))
            if month_param and month_param != "ALL":
                if not j_date.startswith(month_param):
                    continue

            j_no = j.get("journal_number", "")
            items_raw = j.get("line_items") or "[]"
            if isinstance(items_raw, str):
                try:
                    items = json.loads(items_raw)
                except Exception:
                    items = []
            else:
                items = items_raw

            for it in items:
                lname = (it.get("ledger_name") or "Unknown").strip()
                if not lname: continue
                ltype = it.get("ledger_type") or "account"
                amt = float(it.get("amount", 0) or 0)
                d_or_c = (it.get("debit_or_credit") or "debit").lower()

                acc = account_map[lname]
                acc["ledger_name"] = lname
                acc["ledger_type"] = ltype
                acc["entry_count"] += 1
                if d_or_c == "debit":
                    acc["total_debit"] += amt
                else:
                    acc["total_credit"] += amt

                # Keep a light sample of vouchers
                if len(acc["journals"]) < 20:
                    acc["journals"].append({
                        "journal_number": j_no,
                        "date": j.get("date", ""),
                        "amount": amt,
                        "debit_or_credit": d_or_c,
                        "bill_allocations": it.get("bill_allocations", [])
                    })

        accounts_list = []
        tot_debit = 0.0
        tot_credit = 0.0
        balanced_count = 0
        variance_count = 0

        for lname, acc in account_map.items():
            if search_param and search_param not in lname.lower():
                continue
            dr = round(acc["total_debit"], 2)
            cr = round(acc["total_credit"], 2)
            diff = round(dr - cr, 2)
            acc["total_debit"] = dr
            acc["total_credit"] = cr
            acc["net_difference"] = diff
            acc["is_balanced"] = (abs(diff) < 0.01)

            if acc["is_balanced"]:
                balanced_count += 1
            else:
                variance_count += 1

            tot_debit += dr
            tot_credit += cr
            accounts_list.append(acc)

        # Sort by total volume (Debit + Credit) descending
        accounts_list.sort(key=lambda x: (x["total_debit"] + x["total_credit"]), reverse=True)

        return jsonify({
            "status": "success",
            "month": month_param,
            "total_accounts": len(accounts_list),
            "summary": {
                "total_accounts": len(accounts_list),
                "total_debit": round(tot_debit, 2),
                "total_credit": round(tot_credit, 2),
                "net_difference": round(tot_debit - tot_credit, 2),
                "balanced_accounts": balanced_count,
                "variance_accounts": variance_count
            },
            "accounts": accounts_list
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500


# Invoice routes
@app.route('/invoices')
def invoices_page():
    return render_template('invoices.html')

@app.route('/api/invoices/fetch', methods=['POST'])
def api_fetch_invoices():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        voucher_type = request.json.get("voucher_type", "Tax Invoice") if request.is_json else "Tax Invoice"
        overwrite = bool(request.json.get("overwrite", False)) if request.is_json else False
        
        data = invoice_module.get_all_invoices_data(from_date, to_date, limit, voucher_type, overwrite=overwrite)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch invoices from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/invoices/upload', methods=['POST'])
def api_upload_invoices():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        import tempfile, os, json
        from datetime import datetime

        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            invoices = invoice_module.parse_tally_json(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                try: os.unlink(tmp_path)
                except: pass

        if not invoices:
            return jsonify({"error": "No valid invoices found in the JSON file."}), 400

        # Save / merge database invoices
        if database_manager:
            database_manager.init_db()
            now = datetime.now().isoformat()
            db_data_list = []
            
            for inv in invoices:
                if not isinstance(inv, dict): continue
                db_data_list.append({
                    "invoice_number": str(inv.get("invoice_number") or inv.get("voucher_number") or ""),
                    "date":           str(inv.get("date", "")),
                    "customer_name":  str(inv.get("customer_name") or inv.get("party_name") or ""),
                    "po_number":     str(inv.get("po_number", "")),
                    "buyer_address": json.dumps(inv.get("buyer_address", []) if isinstance(inv.get("buyer_address"), list) else [str(inv.get("buyer_address", ""))]),
                    "payment_terms": str(inv.get("payment_terms", "")),
                    "sales_ledger":  str(inv.get("sales_ledger", "")),
                    "narration":     str(inv.get("narration", "")),
                    "irn":         str(inv.get("irn", "")),
                    "irn_ack_no":  str(inv.get("irn_ack_no", "")),
                    "irn_ack_date":str(inv.get("irn_ack_date", "")),
                    "line_items": json.dumps(inv.get("line_items", []) if isinstance(inv.get("line_items"), list) else []),
                    "taxes": json.dumps(inv.get("taxes", []) if isinstance(inv.get("taxes"), list) else []),
                    "rounding_off": float(inv.get("rounding_off", 0) or 0),
                    "tds_amount":   float(inv.get("tds_amount", 0) or 0),
                    "tds_ledger":   str(inv.get("tds_ledger", "") or ""),
                    "tds_rate":     float(inv.get("tds_rate", 0) or 0),
                    "subtotal":     float(inv.get("subtotal", 0) or 0),
                    "tax_total":    float(inv.get("tax_total", 0) or inv.get("tax_amount", 0) or 0),
                    "total_amount": float(inv.get("total_amount", 0) or inv.get("amount", 0) or 0),
                    "from_date":  "UPLOAD",
                    "to_date":    "UPLOAD",
                    "created_at": now,
                    "updated_at": now,
                })
            database_manager.bulk_save_invoices(db_data_list)
            
        total_amount = sum(float(inv.get("total_amount", 0) or inv.get("amount", 0) or 0) for inv in invoices if isinstance(inv, dict))
        return jsonify({
            "status": "success",
            "message": f"Successfully imported {len(invoices)} invoices from JSON!",
            "invoices": invoices,
            "stats": {
                "total_invoices":  len(invoices),
                "total_amount": round(total_amount, 2),
                "from_date":    "UPLOAD",
                "to_date":      "UPLOAD"
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/invoices/sync_zoho', methods=['POST'])
def api_sync_invoices():
    try:
        selected = request.json.get("invoices") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        voucher_type = request.json.get("voucher_type", "Tax Invoice") if request.is_json else "Tax Invoice"
        
        result = invoice_module.sync_invoices_to_zoho(selected, from_date, to_date, limit, voucher_type)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/invoices/reconciliation/monthly', methods=['GET'])
def api_monthly_reconciliation():
    try:
        from collections import defaultdict
        import json, re, requests

        fetch_live = request.args.get('live', 'true').lower() == 'true'

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_invoices = database_manager.get_all_invoices()

        monthly_data = defaultdict(lambda: {
            "tally_count": 0, "tally_total": 0.0,
            "zoho_count": 0, "zoho_total": 0.0,
            "synced_count": 0, "pending_count": 0,
            "is_live_zoho": False
        })

        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

        for row in raw_invoices:
            inv = dict(row)
            raw_date = str(inv.get("date", "")).replace("-", "").strip()
            if len(raw_date) < 6:
                continue
            ym = raw_date[:6]
            tot = float(inv.get("total_amount") or 0.0)
            is_synced = (inv.get("zoho_status") == "synced" or bool(inv.get("zoho_invoice_id")))

            m = monthly_data[ym]
            m["tally_count"] += 1
            m["tally_total"] += tot

            if is_synced:
                m["synced_count"] += 1

        # Fetch LIVE Zoho Books Monthly Totals directly from Zoho Books Cloud API
        live_zoho_success = False
        if fetch_live:
            try:
                from journel.journel_backend import get_access_token
                from invoice.invoice_backend import _get_creds
                import time

                global _ZOHO_MONTHLY_INVOICES_CACHE, _ZOHO_MONTHLY_INVOICES_CACHE_TIME
                if '_ZOHO_MONTHLY_INVOICES_CACHE' not in globals():
                    globals()['_ZOHO_MONTHLY_INVOICES_CACHE'] = {}
                    globals()['_ZOHO_MONTHLY_INVOICES_CACHE_TIME'] = 0

                now = time.time()
                cache_valid = (now - globals()['_ZOHO_MONTHLY_INVOICES_CACHE_TIME'] < 300) and bool(globals()['_ZOHO_MONTHLY_INVOICES_CACHE'])

                if cache_valid:
                    cached_data = globals()['_ZOHO_MONTHLY_INVOICES_CACHE']
                    for z_ym, z_data in cached_data.items():
                        if z_ym in monthly_data:
                            m = monthly_data[z_ym]
                            m["zoho_count"] = z_data["count"]
                            m["zoho_total"] = z_data["total"]
                            m["is_live_zoho"] = True
                            live_zoho_success = True
                else:
                    token = get_access_token()
                    if token:
                        creds = _get_creds()
                        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                        
                        page = 1
                        has_more = True
                        zoho_live_monthly = defaultdict(lambda: {"count": 0, "total": 0.0})
                        
                        while has_more and page <= 10:
                            res = requests.get(
                                f"{creds['base_url']}/invoices",
                                headers=headers,
                                params={"organization_id": creds["org_id"], "page": page, "per_page": 200},
                                timeout=6
                            )
                            if res.status_code == 200 and res.json().get("code") == 0:
                                inv_list = res.json().get("invoices", [])
                                for z_inv in inv_list:
                                    z_date = str(z_inv.get("date") or "").replace("-", "").strip()
                                    if len(z_date) >= 6:
                                        z_ym = z_date[:6]
                                        z_tot = float(z_inv.get("total") or 0.0)
                                        zoho_live_monthly[z_ym]["count"] += 1
                                        zoho_live_monthly[z_ym]["total"] += z_tot
                                page_ctx = res.json().get("page_context", {})
                                has_more = page_ctx.get("has_more_page", False)
                                page += 1
                            else:
                                break

                        if zoho_live_monthly:
                            live_zoho_success = True
                            globals()['_ZOHO_MONTHLY_INVOICES_CACHE'] = dict(zoho_live_monthly)
                            globals()['_ZOHO_MONTHLY_INVOICES_CACHE_TIME'] = time.time()
                            for z_ym, z_data in zoho_live_monthly.items():
                                if z_ym in monthly_data:
                                    m = monthly_data[z_ym]
                                    m["zoho_count"] = z_data["count"]
                                    m["zoho_total"] = z_data["total"]
                                    m["is_live_zoho"] = True

            except Exception as z_err:
                print(f"Error fetching live Zoho invoices: {z_err}")

        # Fallback to local DB synced amounts if live fetch didn't run or failed for a month
        for ym, m in monthly_data.items():
            if not m["is_live_zoho"]:
                for row in raw_invoices:
                    inv = dict(row)
                    raw_date = str(inv.get("date", "")).replace("-", "").strip()
                    if raw_date.startswith(ym) and (inv.get("zoho_status") == "synced" or bool(inv.get("zoho_invoice_id"))):
                        m["zoho_count"] += 1
                        m["zoho_total"] += float(inv.get("total_amount") or 0.0)

        sorted_yms = sorted(monthly_data.keys(), reverse=True)
        result_list = []

        total_tally_sum = 0.0
        total_zoho_sum = 0.0
        matched_months = 0
        mismatched_months = 0

        for ym in sorted_yms:
            data = monthly_data[ym]
            y = ym[:4]
            m_num = int(ym[4:6])
            m_label = f"{MONTH_NAMES[m_num]} {y}"

            tally_tot = round(data["tally_total"], 2)
            zoho_tot = round(data["zoho_total"], 2)
            diff = round(abs(tally_tot - zoho_tot), 2)
            is_matched = (diff <= 0.05) and (data["tally_count"] == data["zoho_count"])

            if is_matched:
                matched_months += 1
                status = "MATCHED"
            else:
                mismatched_months += 1
                status = "MISMATCH"

            total_tally_sum += tally_tot
            total_zoho_sum += zoho_tot

            result_list.append({
                "ym": ym,
                "month_label": m_label,
                "tally_count": data["tally_count"],
                "tally_total": tally_tot,
                "zoho_count": data["zoho_count"],
                "zoho_total": zoho_tot,
                "difference": diff,
                "status": status,
                "is_matched": is_matched,
                "is_live_zoho": data["is_live_zoho"],
                "synced_count": data["synced_count"]
            })

        return jsonify({
            "status": "success",
            "live_zoho_connected": live_zoho_success,
            "summary": {
                "total_months": len(sorted_yms),
                "matched_months": matched_months,
                "mismatched_months": mismatched_months,
                "total_tally_amount": round(total_tally_sum, 2),
                "total_zoho_amount": round(total_zoho_sum, 2),
                "overall_difference": round(abs(total_tally_sum - total_zoho_sum), 2)
            },
            "months": result_list
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/invoices/reconciliation/discrepancies', methods=['GET'])
def api_invoice_discrepancies():
    try:
        month_filter = request.args.get('month', '').strip()
        if not month_filter:
            return jsonify({"error": "Month parameter required"}), 400

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_invoices = database_manager.get_all_invoices()

        tally_map = {}
        for row in raw_invoices:
            inv = dict(row)
            raw_d = str(inv.get("date") or "").replace("-", "").strip()
            if raw_d.startswith(month_filter):
                inv_no = str(inv.get("invoice_number") or "").strip()
                tally_map[inv_no] = inv
                norm_key = inv_no.lower().replace("-", "_").replace(" ", "")
                tally_map[norm_key] = inv

        zoho_map = {}
        try:
            from journel.journel_backend import get_access_token
            from invoice.invoice_backend import _get_creds
            import requests

            token = get_access_token()
            if token:
                creds = _get_creds()
                headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                page = 1
                has_more = True
                
                while has_more and page <= 20:
                    res = requests.get(
                        f"{creds['base_url']}/invoices",
                        headers=headers,
                        params={"organization_id": creds["org_id"], "page": page, "per_page": 200},
                        timeout=10
                    )
                    if res.status_code == 200 and res.json().get("code") == 0:
                        inv_list = res.json().get("invoices", [])
                        for z_inv in inv_list:
                            z_d = str(z_inv.get("date") or "").replace("-", "").strip()
                            if z_d.startswith(month_filter):
                                z_no = str(z_inv.get("invoice_number") or "").strip()
                                zoho_map[z_no] = z_inv
                                norm_key = z_no.lower().replace("-", "_").replace(" ", "")
                                zoho_map[norm_key] = z_inv
                        page_ctx = res.json().get("page_context", {})
                        has_more = page_ctx.get("has_more_page", False)
                        page += 1
                    else:
                        break
        except Exception as z_err:
            print(f"Error fetching live Zoho invoices for discrepancy check: {z_err}")

        all_unique_nos = set()
        for k, inv in tally_map.items():
            no = str(inv.get("invoice_number") or "").strip()
            if no: all_unique_nos.add(no)
        for k, z in zoho_map.items():
            no = str(z.get("invoice_number") or "").strip()
            if no: all_unique_nos.add(no)

        discrepancies = []
        for inv_no in sorted(all_unique_nos):
            norm_key = inv_no.lower().replace("-", "_").replace(" ", "")
            t_inv = tally_map.get(inv_no) or tally_map.get(norm_key)
            z_inv = zoho_map.get(inv_no) or zoho_map.get(norm_key)

            t_amt = float(t_inv.get("total_amount") or 0.0) if t_inv else 0.0
            z_amt = float(z_inv.get("total") or 0.0) if z_inv else 0.0
            diff = round(abs(t_amt - z_amt), 2)

            if diff > 0.05:
                cust_name = (t_inv.get("customer_name") if t_inv else (z_inv.get("customer_name") if z_inv else "Unknown"))
                
                if t_inv and not z_inv:
                    cause = "Missing in Zoho Books"
                elif z_inv and not t_inv:
                    cause = "Present in Zoho Books only"
                elif diff <= 5.0:
                    cause = f"Minor Rounding Difference (₹{diff:.2f})"
                else:
                    cause = f"Amount Mismatch (₹{diff:.2f})"

                discrepancies.append({
                    "invoice_number": inv_no,
                    "customer_name": cust_name,
                    "tally_amount": t_amt,
                    "zoho_amount": z_amt,
                    "difference": diff,
                    "direction": "+" if z_amt > t_amt else "-",
                    "cause": cause
                })

        return jsonify({
            "status": "success",
            "month": month_filter,
            "total_discrepancies": len(discrepancies),
            "discrepancies": discrepancies
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/invoices/reconciliation/account_wise', methods=['GET'])
def api_account_wise_reconciliation():
    """Month-wise reconciliation comparing Sales, Freight Charges, Transportation Charges, and Taxes between Tally and Zoho Books."""
    try:
        from collections import defaultdict
        import json, requests

        fetch_live = request.args.get('live', 'true').lower() == 'true'
        month_param = request.args.get('month', '').strip()

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_invoices = database_manager.get_all_invoices()

        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

        monthly_data = defaultdict(lambda: {
            "inv_count": 0,
            "tally_sales": 0.0,
            "tally_freight": 0.0,
            "tally_transport": 0.0,
            "tally_other_charges": 0.0,
            "tally_tax": 0.0,
            "tally_total": 0.0,
            "zoho_sales": 0.0,
            "zoho_freight": 0.0,
            "zoho_transport": 0.0,
            "zoho_other_charges": 0.0,
            "zoho_tax": 0.0,
            "zoho_total": 0.0,
            "zoho_count": 0,
            "is_live_zoho": False,
            "synced_count": 0
        })

        def _classify_line_item(it):
            if isinstance(it, str):
                nl = it.strip().lower()
                if any(k in nl for k in ["transport", "transpot"]):
                    return "transport"
                elif any(k in nl for k in ["freight", "fright", "cartage", "courier", "delivery"]):
                    return "freight"
                elif any(k in nl for k in ["unlod", "unload", "packaging", "packing"]):
                    return "other_charges"
                return "sales"

            name = str(it.get("item_name") or it.get("name") or "").strip()
            nl = name.lower()
            is_add = it.get("is_additional_charge")

            # 1. Additional charge ledgers in Tally (listed below inventory items)
            if is_add is True:
                if any(k in nl for k in ["transport", "transpot"]):
                    return "transport"
                elif any(k in nl for k in ["freight", "fright", "cartage", "courier", "delivery"]):
                    return "freight"
                else:
                    return "other_charges"

            # 2. Specific non-sales charges entered as stock items
            if any(k in nl for k in ["unloding charges", "unloading charges", "packaging box", "packaging charges", "thermacole boxes"]):
                return "other_charges"

            # 3. Regular inventory stock items (allocated to GST Sales in Tally)
            return "sales"

        for row in raw_invoices:
            inv = dict(row)
            raw_date = str(inv.get("date", "")).replace("-", "").strip()
            if len(raw_date) < 6:
                continue
            ym = raw_date[:6]
            if month_param and not ym.startswith(month_param):
                continue

            m = monthly_data[ym]
            m["inv_count"] += 1

            tot = float(inv.get("total_amount") or 0.0)
            tax = float(inv.get("tax_total") or 0.0)
            m["tally_tax"] += tax
            m["tally_total"] += tot

            is_synced = (inv.get("zoho_status") == "synced" or bool(inv.get("zoho_invoice_id")))
            if is_synced:
                m["synced_count"] += 1

            lines = inv.get("line_items") or []
            if isinstance(lines, str):
                try: lines = json.loads(lines)
                except: lines = []

            inv_sales = 0.0
            inv_freight = 0.0
            inv_transport = 0.0
            inv_other = 0.0

            for it in lines:
                amt = float(it.get("amount") or 0.0)
                cat = _classify_line_item(it)

                if cat == "transport":
                    inv_transport += amt
                elif cat == "freight":
                    inv_freight += amt
                elif cat == "other_charges":
                    inv_other += amt
                else:
                    inv_sales += amt

            m["tally_sales"] += inv_sales
            m["tally_freight"] += inv_freight
            m["tally_transport"] += inv_transport
            m["tally_other_charges"] += inv_other

        sorted_yms = sorted(monthly_data.keys(), reverse=True)

        # ══════════════════════════════════════════════════════════════════════════
        # LIVE ZOHO BOOKS REPORT API & INVOICE API QUERY (Real-time Cloud Sync)
        # ══════════════════════════════════════════════════════════════════════════
        live_zoho_success = False
        if fetch_live:
            try:
                from journel.journel_backend import get_access_token, _get_creds
                token = get_access_token()
                if token:
                    creds = _get_creds()
                    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                    org_id = creds["org_id"]
                    base_url = creds["base_url"]

                    import calendar, time
                    from concurrent.futures import ThreadPoolExecutor, as_completed

                    # Global Cache to prevent Zoho rate limits (15 minutes TTL)
                    global _ZOHO_ACCOUNT_RECON_CACHE, _ZOHO_ACCOUNT_RECON_CACHE_TIME
                    if '_ZOHO_ACCOUNT_RECON_CACHE' not in globals():
                        globals()['_ZOHO_ACCOUNT_RECON_CACHE'] = {}
                        globals()['_ZOHO_ACCOUNT_RECON_CACHE_TIME'] = 0

                    now = time.time()
                    force_refresh = (request.args.get("force", "false").lower() == "true")
                    cache_ttl = 900  # 15 minutes
                    cache_valid = not force_refresh and (now - globals()['_ZOHO_ACCOUNT_RECON_CACHE_TIME'] < cache_ttl) and bool(globals()['_ZOHO_ACCOUNT_RECON_CACHE'])

                    if cache_valid and (not month_param or month_param in globals()['_ZOHO_ACCOUNT_RECON_CACHE']):
                        cached_data = globals()['_ZOHO_ACCOUNT_RECON_CACHE']
                        for ym, z_data in cached_data.items():
                            if ym in monthly_data and z_data:
                                m = monthly_data[ym]
                                if z_data.get("sales", 0) > 0: m["zoho_sales"] = z_data["sales"]
                                if z_data.get("freight", 0) > 0: m["zoho_freight"] = z_data["freight"]
                                if z_data.get("transport", 0) > 0: m["zoho_transport"] = z_data["transport"]
                                if z_data.get("other_charges", 0) > 0: m["zoho_other_charges"] = z_data["other_charges"]
                                if z_data.get("tax", 0) > 0: m["zoho_tax"] = z_data["tax"]
                                if z_data.get("total", 0) > 0: m["zoho_total"] = z_data["total"]
                                if z_data.get("count", 0) > 0: m["zoho_count"] = z_data["count"]
                                m["is_live_zoho"] = True
                                live_zoho_success = True
                    else:
                        def _fetch_zoho_live_month(ym):
                            try:
                                year = int(ym[:4])
                                month = int(ym[4:6])
                                last_day = calendar.monthrange(year, month)[1]
                                from_date = f"{year:04d}-{month:02d}-01"
                                to_date = f"{year:04d}-{month:02d}-{last_day:02d}"

                                # 1. Live Profit and Loss Report API from Zoho Books
                                pnl_res = requests.get(
                                    f"{base_url}/reports/profitandloss",
                                    headers=headers,
                                    params={"organization_id": org_id, "from_date": from_date, "to_date": to_date},
                                    timeout=10
                                )
                                live_pnl = {"sales": 0.0, "freight": 0.0, "transport": 0.0, "other_charges": 0.0}
                                if pnl_res.status_code == 200 and pnl_res.json().get("code") == 0:
                                    pnl_data = pnl_res.json().get("profit_and_loss", [])
                                    def _traverse(nodes):
                                        for node in nodes:
                                            name = str(node.get("name") or "").strip().lower()
                                            tot = abs(float(node.get("total") or 0.0))
                                            acc_id = node.get("account_id")
                                            if acc_id or not node.get("account_transactions"):
                                                if name == "sales" or name == "sales account" or name == "general sales":
                                                    live_pnl["sales"] += tot
                                                elif "freight" in name or "fright" in name or "cartage" in name:
                                                    live_pnl["freight"] += tot
                                                elif "transport" in name or "transpot" in name:
                                                    live_pnl["transport"] += tot
                                                elif "unlod" in name or "unload" in name or "packaging" in name:
                                                    live_pnl["other_charges"] += tot
                                            if "account_transactions" in node and isinstance(node["account_transactions"], list):
                                                _traverse(node["account_transactions"])
                                    _traverse(pnl_data)

                                # 2. Live Invoices API from Zoho Books
                                inv_res = requests.get(
                                    f"{base_url}/invoices",
                                    headers=headers,
                                    params={"organization_id": org_id, "date_start": from_date, "date_end": to_date, "per_page": 200},
                                    timeout=10
                                )
                                inv_count = 0
                                inv_total = 0.0
                                if inv_res.status_code == 200 and inv_res.json().get("code") == 0:
                                    inv_list = inv_res.json().get("invoices", [])
                                    inv_count = len(inv_list)
                                    for inv in inv_list:
                                        inv_total += float(inv.get("total") or 0.0)

                                sub = live_pnl["sales"] + live_pnl["freight"] + live_pnl["transport"] + live_pnl["other_charges"]
                                tax_calc = max(0.0, round(inv_total - sub, 2)) if (sub > 0 and inv_total >= sub) else 0.0

                                return ym, {
                                    "sales": round(live_pnl["sales"], 2),
                                    "freight": round(live_pnl["freight"], 2),
                                    "transport": round(live_pnl["transport"], 2),
                                    "other_charges": round(live_pnl["other_charges"], 2),
                                    "tax": round(tax_calc, 2),
                                    "total": round(inv_total, 2),
                                    "count": inv_count
                                }
                            except Exception as ex:
                                return ym, None

                        new_cache = dict(globals().get('_ZOHO_ACCOUNT_RECON_CACHE', {}))
                        target_fetch_yms = [month_param] if (month_param and month_param in monthly_data) else (sorted_yms[:1] if sorted_yms else [])
                        with ThreadPoolExecutor(max_workers=3) as executor:
                            futures = [executor.submit(_fetch_zoho_live_month, ym) for ym in target_fetch_yms]
                            for fut in as_completed(futures):
                                ym_res, z_data = fut.result()
                                if z_data and (z_data.get("sales", 0) > 0 or z_data.get("total", 0) > 0 or z_data.get("count", 0) > 0):
                                    live_zoho_success = True
                                    new_cache[ym_res] = z_data
                                    m = monthly_data[ym_res]
                                    m["zoho_sales"] = z_data["sales"]
                                    m["zoho_freight"] = z_data["freight"]
                                    m["zoho_transport"] = z_data["transport"]
                                    m["zoho_other_charges"] = z_data["other_charges"]
                                    m["zoho_total"] = z_data["total"]
                                    m["zoho_count"] = z_data["count"]
                                    
                                    m_sub = m["zoho_sales"] + m["zoho_freight"] + m["zoho_transport"] + m["zoho_other_charges"]
                                    if m_sub > 0 and m["zoho_total"] >= m_sub:
                                        m["zoho_tax"] = round(m["zoho_total"] - m_sub, 2)
                                    elif z_data.get("tax", 0) > 0:
                                        m["zoho_tax"] = z_data["tax"]
                                        
                                    m["is_live_zoho"] = True

                        if new_cache:
                            globals()['_ZOHO_ACCOUNT_RECON_CACHE'] = new_cache
                            globals()['_ZOHO_ACCOUNT_RECON_CACHE_TIME'] = time.time()
            except Exception as z_err:
                print(f"Error querying live Zoho Books Reports API: {z_err}")

        # Fallback to local DB synced amounts ONLY if live fetch was not requested or failed completely
        if not fetch_live:
            for ym in sorted_yms:
                m = monthly_data[ym]
                if not m["is_live_zoho"] or m["zoho_sales"] == 0:
                    calc_sales = 0.0
                    calc_freight = 0.0
                    calc_transport = 0.0
                    calc_other = 0.0
                    calc_tax = 0.0
                    calc_tot = 0.0
                    calc_count = 0
                    for row in raw_invoices:
                        inv = dict(row)
                        raw_date = str(inv.get("date", "")).replace("-", "").strip()
                        if raw_date.startswith(ym) and (inv.get("zoho_status") == "synced" or bool(inv.get("zoho_invoice_id"))):
                            calc_count += 1
                            tot = float(inv.get("total_amount") or 0.0)
                            tax = float(inv.get("tax_total") or 0.0)
                            calc_tax += tax
                            calc_tot += tot
                            lines = inv.get("line_items") or []
                            if isinstance(lines, str):
                                try: lines = json.loads(lines)
                                except: lines = []
                            for it in lines:
                                amt = float(it.get("amount") or 0.0)
                                cat = _classify_line_item(it)
                                if cat == "transport":
                                    calc_transport += amt
                                elif cat == "freight":
                                    calc_freight += amt
                                elif cat == "other_charges":
                                    calc_other += amt
                                else:
                                    calc_sales += amt

                    if m["zoho_sales"] == 0 and (calc_sales > 0 or calc_other > 0):
                        m["zoho_sales"] = round(calc_sales, 2)
                        m["zoho_freight"] = round(calc_freight, 2)
                        m["zoho_transport"] = round(calc_transport, 2)
                        m["zoho_other_charges"] = round(calc_other, 2)
                        if m["zoho_total"] == 0: m["zoho_total"] = round(calc_tot, 2)
                        if m["zoho_tax"] == 0: m["zoho_tax"] = round(calc_tax, 2)
                        if m["zoho_count"] == 0: m["zoho_count"] = calc_count

        months = []
        t_sales_sum, t_freight_sum, t_transport_sum, t_other_sum, t_tax_sum, t_tot_sum = 0, 0, 0, 0, 0, 0
        z_sales_sum, z_freight_sum, z_transport_sum, z_other_sum, z_tax_sum, z_tot_sum = 0, 0, 0, 0, 0, 0
        matched_count = 0
        mismatch_count = 0

        for ym in sorted_yms:
            data = monthly_data[ym]
            y = ym[:4]
            m_num = int(ym[4:6])
            m_label = f"{MONTH_NAMES[m_num]} {y}"

            ts = round(data["tally_sales"], 2)
            tf = round(data["tally_freight"], 2)
            tt = round(data["tally_transport"], 2)
            to = round(data["tally_other_charges"], 2)
            tx = round(data["tally_tax"], 2)
            tot_t = round(data["tally_total"], 2)

            zs = round(data["zoho_sales"], 2)
            zf = round(data["zoho_freight"], 2)
            zt = round(data["zoho_transport"], 2)
            zo = round(data["zoho_other_charges"], 2)
            zx = round(data["zoho_tax"], 2)
            tot_z = round(data["zoho_total"], 2)

            diff_sales = round(abs(ts - zs), 2)
            diff_freight = round(abs(tf - zf), 2)
            diff_transport = round(abs(tt - zt), 2)
            diff_other = round(abs(to - zo), 2)
            diff_tax = round(abs(tx - zx), 2)
            diff_total = round(abs(tot_t - tot_z), 2)

            is_matched = (diff_total <= 0.05) and (diff_freight <= 0.05) and (diff_transport <= 0.05)
            if is_matched: matched_count += 1
            else: mismatch_count += 1

            t_sales_sum += ts
            t_freight_sum += tf
            t_transport_sum += tt
            t_other_sum += to
            t_tax_sum += tx
            t_tot_sum += tot_t

            z_sales_sum += zs
            z_freight_sum += zf
            z_transport_sum += zt
            z_other_sum += zo
            z_tax_sum += zx
            z_tot_sum += tot_z

            months.append({
                "ym": ym,
                "month_label": m_label,
                "inv_count": data["inv_count"],
                "synced_count": data.get("zoho_count", data["synced_count"]),
                "is_live_zoho": data["is_live_zoho"],
                "tally": {
                    "sales": ts,
                    "freight_charges": tf,
                    "transportation_charges": tt,
                    "other_charges": to,
                    "tax": tx,
                    "total": tot_t
                },
                "zoho": {
                    "sales": zs,
                    "freight_charges": zf,
                    "transportation_charges": zt,
                    "other_charges": zo,
                    "tax": zx,
                    "total": tot_z
                },
                "variance": {
                    "sales": diff_sales,
                    "freight_charges": diff_freight,
                    "transportation_charges": diff_transport,
                    "other_charges": diff_other,
                    "tax": diff_tax,
                    "total": diff_total
                },
                "is_matched": is_matched,
                "status": "MATCHED" if is_matched else "MISMATCH"
            })

        summary = {
            "total_months": len(sorted_yms),
            "matched_months": matched_count,
            "mismatched_months": mismatch_count,
            "tally_totals": {
                "sales": round(t_sales_sum, 2),
                "freight_charges": round(t_freight_sum, 2),
                "transportation_charges": round(t_transport_sum, 2),
                "other_charges": round(t_other_sum, 2),
                "tax": round(t_tax_sum, 2),
                "total": round(t_tot_sum, 2)
            },
            "zoho_totals": {
                "sales": round(z_sales_sum, 2),
                "freight_charges": round(z_freight_sum, 2),
                "transportation_charges": round(z_transport_sum, 2),
                "other_charges": round(z_other_sum, 2),
                "tax": round(z_tax_sum, 2),
                "total": round(z_tot_sum, 2)
            },
            "variance": {
                "sales": round(abs(t_sales_sum - z_sales_sum), 2),
                "freight_charges": round(abs(t_freight_sum - z_freight_sum), 2),
                "transportation_charges": round(abs(t_transport_sum - z_transport_sum), 2),
                "other_charges": round(abs(t_other_sum - z_other_sum), 2),
                "tax": round(abs(t_tax_sum - z_tax_sum), 2),
                "total": round(abs(t_tot_sum - z_tot_sum), 2)
            }
        }

        return jsonify({
            "status": "success",
            "live_zoho_connected": live_zoho_success,
            "summary": summary,
            "months": months
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/invoices/reconciliation/account_discrepancies', methods=['GET'])
def api_account_discrepancies():
    """Invoice line-item level account breakdown and discrepancy inspector with Live Zoho Books matching."""
    try:
        import json, re, calendar, requests
        month_filter = request.args.get('month', '').strip()

        if not month_filter:
            return jsonify({"error": "Month parameter required"}), 400

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_invoices = database_manager.get_all_invoices()

        def _safe_flt(v, default=0.0):
            if v is None: return default
            if isinstance(v, (int, float)): return float(v)
            c = re.sub(r'[^\d.-]', '', str(v).split('/')[0])
            try: return float(c)
            except: return default

        # Fetch live Zoho invoices for this month to match directly
        zoho_live_invoices = {}
        try:
            from journel.journel_backend import get_access_token, _get_creds
            token = get_access_token()
            if token and len(month_filter) == 6:
                creds = _get_creds()
                headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                raw_zoho_invoices = []
                year = int(month_filter[:4])
                month = int(month_filter[4:6])
                last_day = calendar.monthrange(year, month)[1]
                from_date = f"{year:04d}-{month:02d}-01"
                to_date = f"{year:04d}-{month:02d}-{last_day:02d}"

                inv_res = requests.get(
                    f"{creds['base_url']}/invoices",
                    headers=headers,
                    params={"organization_id": creds["org_id"], "date_start": from_date, "date_end": to_date, "per_page": 200},
                    timeout=10
                )
                if inv_res.status_code == 200 and inv_res.json().get("code") == 0:
                    raw_zoho_invoices = inv_res.json().get("invoices", [])
                    for z_inv in raw_zoho_invoices:
                        z_no = str(z_inv.get("invoice_number") or "").strip()
                        zoho_live_invoices[z_no] = z_inv
                        clean_k = z_no.lower().replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
                        zoho_live_invoices[clean_k] = z_inv
        except Exception as ze:
            print(f"Error fetching live zoho invoices in discrepancy check: {ze}")

        discrepancies = []
        matched_zoho_ids = set()

        for row in raw_invoices:
            inv = dict(row)
            raw_d = str(inv.get("date") or "").replace("-", "").strip()
            if not raw_d.startswith(month_filter):
                continue

            inv_no = str(inv.get("invoice_number") or "").strip()
            cust = str(inv.get("customer_name") or "").strip()
            d_str = str(inv.get("date") or "").strip()
            tot = _safe_flt(inv.get("total_amount"))
            tax = _safe_flt(inv.get("tax_total"))

            clean_k = inv_no.lower().replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
            z_live = zoho_live_invoices.get(inv_no) or zoho_live_invoices.get(clean_k)

            # Match by unique suffix or customer if not directly keyed
            if not z_live and raw_zoho_invoices:
                for cand in raw_zoho_invoices:
                    if cand.get("invoice_id") in matched_zoho_ids:
                        continue
                    cand_no = str(cand.get("invoice_number") or "")
                    cand_k = cand_no.lower().replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
                    if clean_k == cand_k or cand_k.endswith(clean_k[-2:]) or (cand.get("customer_name") == cust and abs(float(cand.get("total") or 0) - tot) < 0.05):
                        z_live = cand
                        break

            if z_live and z_live.get("invoice_id"):
                matched_zoho_ids.add(z_live["invoice_id"])

            is_synced = bool(z_live) or (inv.get("zoho_status") == "synced" or bool(inv.get("zoho_invoice_id")))

            lines = inv.get("line_items") or []
            if isinstance(lines, str):
                try: lines = json.loads(lines)
                except: lines = []

            inv_sales = 0.0
            inv_freight = 0.0
            inv_transport = 0.0
            inv_other = 0.0

            z_sales = 0.0
            z_freight = 0.0
            z_transport = 0.0
            z_other = 0.0

            parsed_items = []

            for it in lines:
                name = str(it.get("item_name") or it.get("name") or "").strip()
                name_l = name.lower()
                amt = _safe_flt(it.get("amount"))
                qty = _safe_flt(it.get("quantity"), default=1.0)
                rate = _safe_flt(it.get("rate"), default=amt)
                is_add = it.get("is_additional_charge")

                # 1. Tally account classification
                if is_add is True:
                    if any(k in name_l for k in ["transport", "transpot"]):
                        tally_acc = "Transportation Charges"
                        inv_transport += amt
                    elif any(k in name_l for k in ["freight", "fright", "cartage", "courier", "delivery"]):
                        tally_acc = "Freight Charges"
                        inv_freight += amt
                    else:
                        tally_acc = "Other / Unloading Charges"
                        inv_other += amt
                elif any(k in name_l for k in ["unlod", "unload", "loading", "packaging box", "packaging charge", "packing charge", "thermacole box", "thermocol box", "unloading charges"]):
                    tally_acc = "Other / Unloading Charges"
                    inv_other += amt
                else:
                    tally_acc = "Sales (GST Goods)"
                    inv_sales += amt

                # 2. Zoho Books account mapping
                if name_l in ["transportation charges", "transport charges", "transpotation charges", "transportation charge", "transpot charges"]:
                    zoho_acc = "Transportation Charges"
                    z_transport += amt
                elif name_l in ["freight charges", "fright charges", "freight charge", "fright charge", "freight", "fright"]:
                    zoho_acc = "Freight Charges"
                    z_freight += amt
                else:
                    zoho_acc = "Sales"
                    z_sales += amt

                is_mismatch = (tally_acc != zoho_acc and not (tally_acc == "Sales (GST Goods)" and zoho_acc == "Sales"))

                parsed_items.append({
                    "item_name": name,
                    "quantity": qty,
                    "rate": rate,
                    "amount": amt,
                    "account": tally_acc,
                    "tally_account": tally_acc,
                    "zoho_account": zoho_acc,
                    "is_account_mismatch": is_mismatch
                })

            z_tot = float(z_live.get("total") or 0.0) if z_live else (tot if is_synced else 0.0)
            z_sub = float(z_live.get("sub_total") or z_tot) if z_live else (z_sales + z_freight + z_transport if is_synced else 0.0)
            z_tax = max(0.0, round(z_tot - z_sub, 2)) if z_live else (tax if is_synced else 0.0)

            diff_sales = round(abs(inv_sales - z_sales), 2)
            diff_freight = round(abs(inv_freight - z_freight), 2)
            diff_transport = round(abs(inv_transport - z_transport), 2)
            diff_other = round(abs(inv_other - z_other), 2)
            diff_tax = round(abs(tax - z_tax), 2)
            diff_tot = round(abs(tot - z_tot), 2)

            has_mismatch = (diff_sales > 0.05 or diff_transport > 0.05 or diff_freight > 0.05 or diff_other > 0.05 or diff_tot > 0.05)

            discrepancies.append({
                "invoice_number": inv_no,
                "customer_name": cust,
                "date": d_str,
                "is_synced": is_synced,
                "is_live_found": bool(z_live),
                "has_other_charges": inv_other > 0,
                "other_charges_amount": inv_other,
                "has_account_mismatch": has_mismatch,
                "tally": {
                    "sales": inv_sales,
                    "freight": inv_freight,
                    "transport": inv_transport,
                    "other_charges": inv_other,
                    "tax": tax,
                    "total": tot
                },
                "zoho": {
                    "sales": z_sales,
                    "freight": z_freight,
                    "transport": z_transport,
                    "other_charges": z_other,
                    "tax": z_tax,
                    "total": z_tot
                },
                "variance": {
                    "sales": diff_sales,
                    "freight": diff_freight,
                    "transport": diff_transport,
                    "other_charges": diff_other,
                    "tax": diff_tax,
                    "total": diff_tot
                },
                "items": parsed_items
            })

        # Identify key highlights & mapping discrepancies for this month
        causes = []
        for d in discrepancies:
            tot_diff = d["variance"]["total"]
            mismatched_items = [it for it in d["items"] if it.get("is_account_mismatch")]

            if mismatched_items:
                for mi in mismatched_items:
                    causes.append({
                        "invoice_number": d["invoice_number"],
                        "date": d["date"],
                        "customer_name": d["customer_name"],
                        "amount": mi["amount"],
                        "items": mi["item_name"],
                        "tally_account": mi["tally_account"],
                        "zoho_account": mi["zoho_account"],
                        "explanation": f"Invoice {d['invoice_number']} ({d['customer_name']}): Line item '{mi['item_name']}' (₹{mi['amount']:.2f}) was posted to {mi['tally_account']} in Tally, but mapped to {mi['zoho_account']} in Zoho Books."
                    })
            elif tot_diff > 0.05:
                causes.append({
                    "invoice_number": d["invoice_number"],
                    "date": d["date"],
                    "customer_name": d["customer_name"],
                    "amount": tot_diff,
                    "items": "Total Amount",
                    "tally_account": "Total",
                    "zoho_account": "Total",
                    "explanation": f"Invoice {d['invoice_number']} ({d['customer_name']}): Amount variance of ₹{tot_diff:.2f} (Tally ₹{d['tally']['total']:.2f} vs Zoho ₹{d['zoho']['total']:.2f})."
                })

        # Check for any extra/duplicate live Zoho invoices that don't exist in Tally
        if raw_zoho_invoices:
            for z_inv in raw_zoho_invoices:
                z_id = z_inv.get("invoice_id")
                if z_id and z_id not in matched_zoho_ids:
                    z_no = str(z_inv.get("invoice_number") or "").strip()
                    z_tot = float(z_inv.get("total") or 0.0)
                    causes.append({
                        "invoice_number": z_no,
                        "date": z_inv.get("date"),
                        "customer_name": z_inv.get("customer_name"),
                        "amount": z_tot,
                        "items": "Duplicate / Extra Invoice in Zoho",
                        "tally_account": "None (Not in Tally)",
                        "zoho_account": "Sales",
                        "explanation": f"Duplicate/Extra Invoice {z_no} in Zoho Books ({z_inv.get('customer_name')} for ₹{z_tot:.2f}). This invoice exists in Zoho Books but not in Tally."
                    })

        return jsonify({
            "status": "success",
            "month": month_filter,
            "total_invoices": len(discrepancies),
            "causes": causes,
            "invoices": discrepancies
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/invoices/reconciliation/export_account_recon_excel', methods=['POST', 'GET'])
def api_export_account_recon_excel():
    """Generates a downloadable Excel spreadsheet containing month-wise account reconciliation & line-item audits."""
    try:
        import io, openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from collections import defaultdict
        import json
        from datetime import datetime

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_invoices = database_manager.get_all_invoices()

        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

        monthly_data = defaultdict(lambda: {
            "inv_count": 0, "synced_count": 0,
            "tally_sales": 0.0, "tally_freight": 0.0, "tally_transport": 0.0, "tally_tax": 0.0, "tally_total": 0.0,
            "zoho_sales": 0.0, "zoho_freight": 0.0, "zoho_transport": 0.0, "zoho_tax": 0.0, "zoho_total": 0.0
        })

        all_item_rows = []

        for row in raw_invoices:
            inv = dict(row)
            raw_date = str(inv.get("date", "")).replace("-", "").strip()
            if len(raw_date) < 6: continue
            ym = raw_date[:6]
            m = monthly_data[ym]
            m["inv_count"] += 1

            tot = float(inv.get("total_amount") or 0.0)
            tax = float(inv.get("tax_total") or 0.0)
            m["tally_tax"] += tax
            m["tally_total"] += tot

            is_synced = (inv.get("zoho_status") == "synced" or bool(inv.get("zoho_invoice_id")))
            if is_synced: m["synced_count"] += 1

            lines = inv.get("line_items") or []
            if isinstance(lines, str):
                try: lines = json.loads(lines)
                except: lines = []

            for it in lines:
                name = str(it.get("item_name") or it.get("name") or "").strip()
                name_l = name.lower()
                amt = float(it.get("amount") or 0.0)

                if name_l in ["transportation charges", "transport charges", "transpotation charges", "transportation charge", "transpot charges"]:
                    acc = "Transportation Charges"
                    m["tally_transport"] += amt
                    if is_synced: m["zoho_transport"] += amt
                elif name_l in ["freight charges", "fright charges", "freight charge", "fright charge", "freight", "fright"]:
                    acc = "Freight Charges"
                    m["tally_freight"] += amt
                    if is_synced: m["zoho_freight"] += amt
                else:
                    acc = "Sales"
                    m["tally_sales"] += amt
                    if is_synced: m["zoho_sales"] += amt

                all_item_rows.append({
                    "month": ym,
                    "invoice_number": inv.get("invoice_number"),
                    "date": inv.get("date"),
                    "customer_name": inv.get("customer_name"),
                    "item_name": name,
                    "account": acc,
                    "amount": amt,
                    "is_synced": "Synced" if is_synced else "Pending"
                })

            if is_synced:
                m["zoho_tax"] += tax
                m["zoho_total"] += tot

        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "Monthly Account Recon"

        headers1 = [
            "Month", "Invoices", "Synced", 
            "Tally Sales", "Zoho Sales", "Diff Sales",
            "Tally Freight", "Zoho Freight", "Diff Freight",
            "Tally Transport", "Zoho Transport", "Diff Transport",
            "Tally Tax", "Zoho Tax", "Diff Tax",
            "Tally Total", "Zoho Total", "Diff Total", "Status"
        ]
        ws1.append(headers1)

        for col in range(1, len(headers1) + 1):
            cell = ws1.cell(row=1, column=col)
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for ym in sorted(monthly_data.keys(), reverse=True):
            d = monthly_data[ym]
            y = ym[:4]
            m_num = int(ym[4:6])
            label = f"{MONTH_NAMES[m_num]} {y}"

            diff_s = round(abs(d["tally_sales"] - d["zoho_sales"]), 2)
            diff_f = round(abs(d["tally_freight"] - d["zoho_freight"]), 2)
            diff_t = round(abs(d["tally_transport"] - d["zoho_transport"]), 2)
            diff_x = round(abs(d["tally_tax"] - d["zoho_tax"]), 2)
            diff_tot = round(abs(d["tally_total"] - d["zoho_total"]), 2)

            is_m = (diff_s <= 0.05 and diff_f <= 0.05 and diff_t <= 0.05 and diff_x <= 0.05 and diff_tot <= 0.05)

            ws1.append([
                label, d["inv_count"], d["synced_count"],
                round(d["tally_sales"], 2), round(d["zoho_sales"], 2), diff_s,
                round(d["tally_freight"], 2), round(d["zoho_freight"], 2), diff_f,
                round(d["tally_transport"], 2), round(d["zoho_transport"], 2), diff_t,
                round(d["tally_tax"], 2), round(d["zoho_tax"], 2), diff_x,
                round(d["tally_total"], 2), round(d["zoho_total"], 2), diff_tot,
                "MATCHED" if is_m else "MISMATCH"
            ])

        # Tab 2: Item level audit
        ws2 = wb.create_sheet(title="Line Item Account Audit")
        headers2 = ["Month", "Invoice #", "Date", "Customer", "Item Name", "Assigned Account", "Amount", "Sync Status"]
        ws2.append(headers2)
        for col in range(1, len(headers2) + 1):
            cell = ws2.cell(row=1, column=col)
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="0288D1", end_color="0288D1", fill_type="solid")

        for it in all_item_rows:
            ws2.append([
                it["month"], it["invoice_number"], it["date"], it["customer_name"],
                it["item_name"], it["account"], round(it["amount"], 2), it["is_synced"]
            ])

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f"Account_Wise_Reconciliation_{datetime.now().strftime('%Y%m%d')}.xlsx"
        return Response(
            output.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/invoices/sync_zoho/start', methods=['POST'])
def api_invoices_sync_zoho_start():
    if not invoice_module:
        return jsonify({"status": "error", "message": "Invoice backend not available"}), 500
    if not job_manager or not sse_format:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    selected = body.get("invoices")
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")
    voucher_type = body.get("voucher_type", "Tax Invoice")

    job = job_manager.create("invoices_sync_zoho")
    job.log("Invoices sync job started.")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")
    
    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            res = invoice_module.sync_invoices_to_zoho(selected, from_date, to_date, limit, voucher_type, log=job.log, stop_event=job.stop_event)
            st = (res or {}).get("status")
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/invoices/export_zoho_excel', methods=['POST'])
def api_export_zoho_excel():
    try:
        from datetime import datetime
        invoices = request.json.get("invoices", []) if request.is_json else []
        if not invoices:
            if database_manager:
                database_manager.init_db()
                invoices = database_manager.get_all_invoices()
                
        if not invoices:
            return jsonify({"error": "No invoices available to export."}), 400
            
        excel_bytes = invoice_module.generate_zoho_formatted_excel(invoices)
        filename = f"Zoho_Books_Formatted_Invoices_{datetime.now().strftime('%Y%m%d')}.xlsx"
        
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/invoices/export_sync_errors_excel', methods=['POST', 'GET'])
def api_export_invoices_sync_errors_excel():
    """Exports a downloadable Excel spreadsheet listing invoice sync errors, reasons, and suggested fixes."""
    try:
        from datetime import datetime
        errors = []
        if request.is_json and request.json:
            errors = request.json.get("errors", [])
        
        if not errors and database_manager:
            database_manager.init_db()
            raw_invoices = database_manager.get_all_invoices()
            for r in raw_invoices:
                inv = dict(r)
                if inv.get("zoho_status") == "failed" or inv.get("zoho_status") == "error" or inv.get("zoho_error"):
                    errors.append({
                        "invoice_number": inv.get("invoice_number") or inv.get("voucher_number"),
                        "date": inv.get("date", ""),
                        "customer": inv.get("customer_name") or inv.get("party_name", ""),
                        "amount": float(inv.get("total_amount") or 0.0),
                        "error": inv.get("zoho_error", "Sync Failed")
                    })
        
        if not errors:
            return jsonify({"error": "No invoice sync errors found to export."}), 400
            
        excel_bytes = invoice_module.generate_sync_errors_excel(errors)
        filename = f"Invoices_Sync_Error_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/invoices/mark_synced', methods=['POST'])
def api_invoices_mark_synced():
    """Manual tick/sync endpoint to mark invoices as Synced or Pending in SQLite DB."""
    try:
        from datetime import datetime
        data = request.json or {}
        invoice_numbers = data.get("invoice_numbers", [])
        status = str(data.get("status", "synced")).lower()
        zoho_id = "MANUALLY_SYNCED" if status == "synced" else None
        
        if not invoice_numbers:
            return jsonify({"error": "No invoices provided to update."}), 400
            
        if database_manager:
            database_manager.init_db()
            conn = database_manager.get_db_connection(write=True)
            cur = conn.cursor()
            updated_count = 0
            for inv_no in invoice_numbers:
                cur.execute(
                    "UPDATE invoices SET zoho_invoice_id = ?, zoho_status = ?, updated_at = ? WHERE invoice_number = ?",
                    (zoho_id, status, datetime.now().isoformat(), str(inv_no))
                )
                updated_count += cur.rowcount
            
            return jsonify({
                "success": True,
                "count": updated_count,
                "status": status,
                "message": f"Successfully marked {updated_count} invoice(s) as {status}."
            })
        return jsonify({"error": "Database manager not available."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/items/export_zoho_excel', methods=['POST'])
def api_export_items_zoho_excel():
    try:
        from datetime import datetime
        items = request.json.get("items", []) if request.is_json else []
        if not items:
            if database_manager:
                database_manager.init_db()
                items = database_manager.get_all_items()
                
        if not items:
            return jsonify({"error": "No items available to export."}), 400
            
        excel_bytes = items_module.generate_zoho_formatted_items_excel(items)
        filename = f"Zoho_Books_Formatted_Items_{datetime.now().strftime('%Y%m%d')}.xlsx"
        
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Bills routes
@app.route('/bills')
def bills_page():
    return render_template('bills.html')

@app.route('/api/bills/fetch', methods=['POST'])
def api_fetch_bills():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        voucher_type = request.json.get("voucher_type", "Purchase") if request.is_json else "Purchase"
        
        data = bills_module.get_all_bills_data(from_date, to_date, limit, voucher_type)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch bills from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/bills/upload', methods=['POST'])
def api_upload_bills():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        from_date = str(request.form.get("from_date") or "").replace("-", "").strip()
        to_date = str(request.form.get("to_date") or "").replace("-", "").strip()
        filter_year = str(request.form.get("filter_year") or "").strip()

        import tempfile, os, json
        from datetime import datetime

        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            parsed_bills = bills_module.parse_tally_json(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not parsed_bills:
            return jsonify({"error": "No bills found in the JSON file."}), 400

        # Apply date range filtering if specified
        total_in_file = len(parsed_bills)
        bills = parsed_bills
        
        if from_date or to_date or (filter_year and filter_year != 'ALL'):
            filtered = []
            for b in parsed_bills:
                b_date = str(b.get("date", "")).replace("-", "").strip()
                if from_date and b_date and b_date < from_date:
                    continue
                if to_date and b_date and b_date > to_date:
                    continue
                if filter_year and filter_year != 'ALL' and b_date:
                    if not b_date.startswith(filter_year):
                        continue
                filtered.append(b)
            bills = filtered

        if not bills:
            date_info = f"from {from_date} to {to_date}" if (from_date or to_date) else f"for year {filter_year}"
            return jsonify({
                "error": f"No bills found in JSON file matching the selected filter ({date_info}). Total vouchers in file: {total_in_file}."
            }), 400

        # Save to SQLite
        if database_manager:
            database_manager.init_db()
            now = datetime.now().isoformat()
            db_data_list = []
            
            for bill in bills:
                db_data_list.append({
                    "bill_number": bill.get("bill_number", ""),
                    "date":        bill.get("date", ""),
                    "vendor_name": bill.get("vendor_name", ""),
                    "po_number":        bill.get("po_number", ""),
                    "reference_number": bill.get("reference_number", ""),
                    "vendor_address":   json.dumps(bill.get("vendor_address", [])),
                    "payment_terms":    bill.get("payment_terms", ""),
                    "purchase_ledger":  bill.get("purchase_ledger", ""),
                    "narration":        bill.get("narration", ""),
                    "line_items": json.dumps(bill.get("line_items", [])),
                    "taxes": json.dumps(bill.get("taxes", [])),
                    "rounding_off": bill.get("rounding_off", 0) or 0,
                    "subtotal":     bill.get("subtotal",     0) or 0,
                    "tax_total":    bill.get("tax_total",    0) or 0,
                    "total_amount": bill.get("total_amount", 0) or 0,
                    "from_date":  from_date or "UPLOAD",
                    "to_date":    to_date or "UPLOAD",
                    "created_at": now,
                    "updated_at": now,
                })
            database_manager.bulk_save_bills(db_data_list)
            
            # Attach existing sync statuses from DB matching (bill_number, date)
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
            
        total_amount = sum(bill.get("total_amount", 0) for bill in bills)
        filter_msg = f" (filtered {len(bills)} of {total_in_file} vouchers for range {from_date} to {to_date})" if len(bills) != total_in_file else ""
        return jsonify({
            "status": "success",
            "message": f"Successfully imported {len(bills)} bills{filter_msg}!",
            "bills": bills,
            "stats": {
                "total_bills":  len(bills),
                "total_amount": round(total_amount, 2),
                "from_date":    from_date or "UPLOAD",
                "to_date":      to_date or "UPLOAD"
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/bills/sync_zoho', methods=['POST'])
def api_sync_bills():
    try:
        selected = request.json.get("bills") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        voucher_type = request.json.get("voucher_type", "Purchase") if request.is_json else "Purchase"
        
        result = bills_module.sync_bills_to_zoho(selected, from_date, to_date, limit, voucher_type)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/bills/export_sync_errors_excel', methods=['POST', 'GET'])
def api_export_bills_sync_errors_excel():
    """Exports a downloadable Excel spreadsheet listing bill sync errors, reasons, and suggested fixes."""
    try:
        from datetime import datetime
        errors = []
        if request.is_json and request.json:
            errors = request.json.get("errors", [])
        
        if not errors and database_manager:
            database_manager.init_db()
            raw_bills = database_manager.get_all_bills()
            for r in raw_bills:
                b = dict(r)
                if b.get("zoho_status") == "failed" or b.get("zoho_error"):
                    errors.append({
                        "bill_number": b.get("bill_number"),
                        "date": b.get("date", ""),
                        "vendor": b.get("vendor_name", ""),
                        "amount": float(b.get("total_amount") or 0.0),
                        "error": b.get("zoho_error", "Sync Failed")
                    })
        
        if not errors:
            return jsonify({"error": "No sync errors found to export."}), 400
            
        excel_bytes = bills_module.generate_sync_errors_excel(errors)
        filename = f"Bills_Sync_Error_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bills/mark_synced', methods=['POST'])
def api_bills_mark_synced():
    """Manual tick/sync endpoint to mark bills as Synced or Pending in SQLite DB."""
    try:
        from datetime import datetime
        data = request.json or {}
        bill_numbers = data.get("bill_numbers", [])
        status = str(data.get("status", "synced")).lower()
        zoho_id = "MANUALLY_SYNCED" if status == "synced" else None
        
        if not bill_numbers:
            return jsonify({"error": "No bills provided to update."}), 400
            
        if database_manager:
            database_manager.init_db()
            conn = database_manager.get_db_connection(write=True)
            cur = conn.cursor()
            updated_count = 0
            for b_no in bill_numbers:
                cur.execute(
                    "UPDATE bills SET zoho_bill_id = ?, zoho_status = ?, updated_at = ? WHERE bill_number = ?",
                    (zoho_id, status, datetime.now().isoformat(), str(b_no))
                )
                updated_count += cur.rowcount
            
            return jsonify({
                "success": True,
                "count": updated_count,
                "status": status,
                "message": f"Successfully marked {updated_count} bill(s) as {status}."
            })
        return jsonify({"error": "Database manager not available."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════
# BILLS RECONCILIATION API ROUTES (Tally vs Zoho Books)
# ══════════════════════════════════════════════════════════════════════════

@app.route('/api/bills/reconciliation/monthly_amounts', methods=['GET'])
def api_bills_reconciliation_monthly_amounts():
    """Computes month-wise vendor bills reconciliation between Tally and Zoho Books."""
    try:
        from collections import defaultdict
        import calendar, time
        import requests

        fetch_live = (request.args.get("live", "true").lower() == "true")

        raw_bills = database_manager.get_all_bills()
        if not raw_bills:
            return jsonify({"status": "success", "months": [], "summary": {}})

        monthly_data = defaultdict(lambda: {
            "tally_count": 0,
            "tally_total": 0.0,
            "zoho_count": 0,
            "zoho_total": 0.0,
            "is_live_zoho": False,
            "synced_count": 0,
            "pending_count": 0
        })

        for row in raw_bills:
            bill = dict(row)
            raw_date = str(bill.get("date", "")).replace("-", "").strip()
            if len(raw_date) < 6:
                continue
            ym = raw_date[:6]
            m = monthly_data[ym]
            m["tally_count"] += 1
            tot = float(bill.get("total_amount") or 0.0)
            m["tally_total"] += tot

            is_synced = (bill.get("zoho_status") == "synced" or bool(bill.get("zoho_bill_id")))
            if is_synced:
                m["synced_count"] += 1
            else:
                m["pending_count"] += 1

        # Fetch LIVE Zoho Books Monthly Totals directly from Zoho Books Cloud API
        live_zoho_success = False
        if fetch_live:
            try:
                from journel.journel_backend import get_access_token, _get_creds

                global _ZOHO_MONTHLY_BILLS_CACHE, _ZOHO_MONTHLY_BILLS_CACHE_TIME
                if '_ZOHO_MONTHLY_BILLS_CACHE' not in globals():
                    globals()['_ZOHO_MONTHLY_BILLS_CACHE'] = {}
                    globals()['_ZOHO_MONTHLY_BILLS_CACHE_TIME'] = 0

                now = time.time()
                cache_valid = (now - globals()['_ZOHO_MONTHLY_BILLS_CACHE_TIME'] < 900) and bool(globals()['_ZOHO_MONTHLY_BILLS_CACHE'])

                if cache_valid:
                    cached_data = globals()['_ZOHO_MONTHLY_BILLS_CACHE']
                    for z_ym, z_data in cached_data.items():
                        if z_ym in monthly_data:
                            m = monthly_data[z_ym]
                            m["zoho_count"] = z_data["count"]
                            m["zoho_total"] = z_data["total"]
                            m["is_live_zoho"] = True
                            live_zoho_success = True
                else:
                    token = get_access_token()
                    if token:
                        creds = _get_creds()
                        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                        
                        page = 1
                        has_more = True
                        zoho_live_monthly = defaultdict(lambda: {"count": 0, "total": 0.0})
                        
                        while has_more and page <= 15:
                            res = requests.get(
                                f"{creds['base_url']}/bills",
                                headers=headers,
                                params={"organization_id": creds["org_id"], "status": "all", "page": page, "per_page": 200},
                                timeout=8
                            )
                            if res.status_code == 200 and res.json().get("code") == 0:
                                bill_list = res.json().get("bills", [])
                                for z_b in bill_list:
                                    z_date = str(z_b.get("date") or "").replace("-", "").strip()
                                    if len(z_date) >= 6:
                                        z_ym = z_date[:6]
                                        z_tot = float(z_b.get("total") or 0.0)
                                        zoho_live_monthly[z_ym]["count"] += 1
                                        zoho_live_monthly[z_ym]["total"] += z_tot
                                page_ctx = res.json().get("page_context", {})
                                has_more = page_ctx.get("has_more_page", False)
                                page += 1
                            else:
                                break

                        if zoho_live_monthly:
                            live_zoho_success = True
                            globals()['_ZOHO_MONTHLY_BILLS_CACHE'] = dict(zoho_live_monthly)
                            globals()['_ZOHO_MONTHLY_BILLS_CACHE_TIME'] = time.time()
                            for z_ym, z_data in zoho_live_monthly.items():
                                m = monthly_data[z_ym]
                                m["zoho_count"] = z_data["count"]
                                m["zoho_total"] = z_data["total"]
                                m["is_live_zoho"] = True

            except Exception as z_err:
                print(f"Error querying live Zoho Books Bills API: {z_err}")

        # Fallback to local DB synced counts/amounts if live fetch was not requested or failed
        if not live_zoho_success:
            for ym, m in monthly_data.items():
                if not m["is_live_zoho"]:
                    for row in raw_bills:
                        b = dict(row)
                        raw_date = str(b.get("date", "")).replace("-", "").strip()
                        if raw_date.startswith(ym) and (b.get("zoho_status") == "synced" or bool(b.get("zoho_bill_id"))):
                            m["zoho_count"] += 1
                            m["zoho_total"] += float(b.get("total_amount") or 0.0)

        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                       "July", "August", "September", "October", "November", "December"]

        sorted_yms = sorted(monthly_data.keys(), reverse=True)
        months = []
        total_tally_sum = 0.0
        total_zoho_sum = 0.0
        matched_count = 0
        mismatch_count = 0

        for ym in sorted_yms:
            item = monthly_data[ym]
            try:
                y = ym[:4]
                m_num = int(ym[4:6])
                if 1 <= m_num <= 12:
                    label = f"{MONTH_NAMES[m_num]} {y}"
                else:
                    label = ym
            except Exception:
                label = ym

            t_tot = round(item["tally_total"], 2)
            z_tot = round(item["zoho_total"], 2)
            diff = round(abs(t_tot - z_tot), 2)
            is_matched = (diff <= 0.05) and (item["tally_count"] == item["zoho_count"])

            if is_matched:
                matched_count += 1
            else:
                mismatch_count += 1

            total_tally_sum += t_tot
            total_zoho_sum += z_tot

            months.append({
                "ym": ym,
                "month_label": label,
                "tally_count": item["tally_count"],
                "tally_total": t_tot,
                "zoho_count": item["zoho_count"],
                "zoho_total": z_tot,
                "difference": diff,
                "status": "MATCHED" if is_matched else "MISMATCH",
                "is_matched": is_matched,
                "is_live_zoho": item["is_live_zoho"],
                "synced_count": item["synced_count"],
                "pending_count": item["pending_count"]
            })

        summary = {
            "total_months": len(sorted_yms),
            "matched_months": matched_count,
            "mismatched_months": mismatch_count,
            "total_tally_amount": round(total_tally_sum, 2),
            "total_zoho_amount": round(total_zoho_sum, 2),
            "overall_difference": round(abs(total_tally_sum - total_zoho_sum), 2)
        }

        return jsonify({
            "status": "success",
            "is_live_connected": live_zoho_success,
            "live_zoho_connected": live_zoho_success,
            "summary": summary,
            "months": months
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bills/reconciliation/single_month', methods=['GET'])
def api_bills_reconcile_single_month():
    """Live queries Zoho Books API for a single month's bills with zero wasted API calls."""
    try:
        ym = request.args.get("month", "").strip()
        if not ym or len(ym) < 6:
            return jsonify({"error": "Invalid month format. Expected YYYYMM"}), 400

        from journel.journel_backend import get_access_token, _get_creds
        import calendar, requests

        token = get_access_token()
        if not token:
            return jsonify({"error": "Could not acquire Zoho access token"}), 401

        creds = _get_creds()
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        year = int(ym[:4])
        month = int(ym[4:6])
        last_day = calendar.monthrange(year, month)[1]

        from_date = f"{year:04d}-{month:02d}-01"
        to_date = f"{year:04d}-{month:02d}-{last_day:02d}"

        res = requests.get(
            f"{creds['base_url']}/bills",
            headers=headers,
            params={"organization_id": creds["org_id"], "date_start": from_date, "date_end": to_date, "status": "all", "per_page": 200},
            timeout=10
        )

        if res.status_code == 200 and res.json().get("code") == 0:
            bill_list = res.json().get("bills", [])
            zoho_count = len(bill_list)
            zoho_total = sum(float(b.get("total") or 0.0) for b in bill_list)

            # Update cache for this month
            global _ZOHO_MONTHLY_BILLS_CACHE
            if '_ZOHO_MONTHLY_BILLS_CACHE' in globals():
                globals()['_ZOHO_MONTHLY_BILLS_CACHE'][ym] = {
                    "count": zoho_count,
                    "total": round(zoho_total, 2)
                }

            # Tally stats for this month
            raw_bills = database_manager.get_all_bills()
            tally_count = 0
            tally_total = 0.0
            for row in raw_bills:
                b = dict(row)
                raw_date = str(b.get("date", "")).replace("-", "").strip()
                if raw_date.startswith(ym):
                    tally_count += 1
                    tally_total += float(b.get("total_amount") or 0.0)

            diff = round(abs(tally_total - zoho_total), 2)
            is_matched = (diff <= 0.05) and (tally_count == zoho_count)

            return jsonify({
                "status": "success",
                "month": {
                    "ym": ym,
                    "tally_count": tally_count,
                    "tally_total": round(tally_total, 2),
                    "zoho_count": zoho_count,
                    "zoho_total": round(zoho_total, 2),
                    "difference": diff,
                    "is_matched": is_matched,
                    "is_live_zoho": True
                }
            })
        else:
            return jsonify({"error": f"Zoho API returned status {res.status_code}: {res.text}"}), 502

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bills/reconciliation/account_wise', methods=['GET'])
def api_bills_account_wise_reconciliation():
    """Provides a month-wise / single-month breakdown of vendor bills across Purchase accounts."""
    try:
        from collections import defaultdict
        import calendar, time
        import requests
        from concurrent.futures import ThreadPoolExecutor, as_completed

        month_param = request.args.get("month", "").strip()
        fetch_live = (request.args.get("live", "false").lower() == "true")

        raw_bills = database_manager.get_all_bills()
        if not raw_bills:
            return jsonify({"status": "success", "months": [], "summary": {}})

        monthly_data = defaultdict(lambda: {
            "bill_count": 0,
            "tally_purchases": 0.0,
            "tally_freight": 0.0,
            "tally_transport": 0.0,
            "tally_other": 0.0,
            "tally_tax": 0.0,
            "tally_total": 0.0,
            "zoho_purchases": 0.0,
            "zoho_freight": 0.0,
            "zoho_transport": 0.0,
            "zoho_other": 0.0,
            "zoho_tax": 0.0,
            "zoho_total": 0.0,
            "zoho_count": 0,
            "is_live_zoho": False,
            "synced_count": 0
        })

        def _classify_bill_item(it):
            if isinstance(it, str):
                nl = it.strip().lower()
                if any(k in nl for k in ["transport", "transpot"]): return "transport"
                elif any(k in nl for k in ["freight", "fright", "cartage", "courier", "delivery"]): return "freight"
                elif any(k in nl for k in ["unlod", "unload", "packaging", "packing"]): return "other_charges"
                return "purchases"

            name = str(it.get("item_name") or it.get("name") or "").strip()
            nl = name.lower()
            is_add = it.get("is_additional_charge")

            if is_add is True:
                if any(k in nl for k in ["transport", "transpot"]):
                    return "transport"
                elif any(k in nl for k in ["freight", "fright", "cartage", "courier", "delivery"]):
                    return "freight"
                else:
                    return "other_charges"

            if any(k in nl for k in ["unloding charges", "unloading charges", "packaging box", "packaging charges", "packing"]):
                return "other_charges"

            return "purchases"

        for row in raw_bills:
            b = dict(row)
            raw_date = str(b.get("date", "")).replace("-", "").strip()
            if len(raw_date) < 6:
                continue
            ym = raw_date[:6]
            if month_param and not ym.startswith(month_param):
                continue

            m = monthly_data[ym]
            m["bill_count"] += 1

            tot = float(b.get("total_amount") or 0.0)
            tax = float(b.get("tax_total") or 0.0)
            m["tally_tax"] += tax
            m["tally_total"] += tot

            is_synced = (b.get("zoho_status") == "synced" or bool(b.get("zoho_bill_id")))
            if is_synced:
                m["synced_count"] += 1

            lines = b.get("line_items") or []
            if isinstance(lines, str):
                try: lines = json.loads(lines)
                except: lines = []

            for it in lines:
                amt = float(it.get("amount") or 0.0)
                cat = _classify_bill_item(it)
                if cat == "transport": m["tally_transport"] += amt
                elif cat == "freight": m["tally_freight"] += amt
                elif cat == "other_charges": m["tally_other"] += amt
                else: m["tally_purchases"] += amt

        sorted_yms = sorted(monthly_data.keys(), reverse=True)

        # ══════════════════════════════════════════════════════════════════════════
        # LIVE ZOHO BOOKS REPORT API & BILLS API QUERY (On-Demand Single Month)
        # ══════════════════════════════════════════════════════════════════════════
        live_zoho_success = False
        if fetch_live:
            try:
                from journel.journel_backend import get_access_token, _get_creds
                token = get_access_token()
                if token:
                    creds = _get_creds()
                    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                    org_id = creds["org_id"]
                    base_url = creds["base_url"]

                    global _ZOHO_BILLS_ACCOUNT_RECON_CACHE, _ZOHO_BILLS_ACCOUNT_RECON_CACHE_TIME
                    if '_ZOHO_BILLS_ACCOUNT_RECON_CACHE' not in globals():
                        globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE'] = {}
                        globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE_TIME'] = 0

                    now = time.time()
                    force_refresh = (request.args.get("force", "false").lower() == "true")
                    cache_ttl = 900  # 15 minutes
                    cache_valid = not force_refresh and (now - globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE_TIME'] < cache_ttl) and bool(globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE'])

                    if cache_valid and (not month_param or month_param in globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE']):
                        cached_data = globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE']
                        for ym, z_data in cached_data.items():
                            if ym in monthly_data and z_data:
                                m = monthly_data[ym]
                                m["zoho_purchases"] = z_data.get("purchases", 0)
                                m["zoho_freight"] = z_data.get("freight", 0)
                                m["zoho_transport"] = z_data.get("transport", 0)
                                m["zoho_other"] = z_data.get("other_charges", 0)
                                m["zoho_tax"] = z_data.get("tax", 0)
                                m["zoho_total"] = z_data.get("total", 0)
                                m["zoho_count"] = z_data.get("count", 0)
                                m["is_live_zoho"] = True
                                live_zoho_success = True
                    else:
                        def _fetch_zoho_live_bills_month(ym):
                            try:
                                year = int(ym[:4])
                                month = int(ym[4:6])
                                last_day = calendar.monthrange(year, month)[1]
                                from_date = f"{year:04d}-{month:02d}-01"
                                to_date = f"{year:04d}-{month:02d}-{last_day:02d}"

                                # 1. Live Profit and Loss Report API for Purchase Accounts
                                pnl_res = requests.get(
                                    f"{base_url}/reports/profitandloss",
                                    headers=headers,
                                    params={"organization_id": org_id, "from_date": from_date, "to_date": to_date},
                                    timeout=10
                                )
                                live_pnl = {"purchases": 0.0, "freight": 0.0, "transport": 0.0, "other_charges": 0.0}
                                if pnl_res.status_code == 200 and pnl_res.json().get("code") == 0:
                                    pnl_data = pnl_res.json().get("profit_and_loss", [])
                                    def _traverse(nodes):
                                        for node in nodes:
                                            name = str(node.get("name") or "").strip().lower()
                                            tot = abs(float(node.get("total") or 0.0))
                                            acc_id = node.get("account_id")
                                            if acc_id or not node.get("account_transactions"):
                                                if name in ["cost of goods sold", "purchase account", "purchases"]:
                                                    live_pnl["purchases"] += tot
                                                elif "freight" in name or "fright" in name or "cartage" in name:
                                                    live_pnl["freight"] += tot
                                                elif "transport" in name or "transpot" in name:
                                                    live_pnl["transport"] += tot
                                                elif "unlod" in name or "unload" in name or "packaging" in name:
                                                    live_pnl["other_charges"] += tot
                                            if "account_transactions" in node and isinstance(node["account_transactions"], list):
                                                _traverse(node["account_transactions"])
                                    _traverse(pnl_data)

                                # 2. Live Bills API from Zoho Books
                                b_res = requests.get(
                                    f"{base_url}/bills",
                                    headers=headers,
                                    params={"organization_id": org_id, "date_start": from_date, "date_end": to_date, "per_page": 200},
                                    timeout=10
                                )
                                b_count = 0
                                b_total = 0.0
                                if b_res.status_code == 200 and b_res.json().get("code") == 0:
                                    b_list = b_res.json().get("bills", [])
                                    b_count = len(b_list)
                                    for z_b in b_list:
                                        b_total += float(z_b.get("total") or 0.0)

                                # If P&L did not capture Inventory Asset (since Inventory Asset is a Balance Sheet account)
                                if (live_pnl["purchases"] == 0 or abs(b_total - (live_pnl["purchases"] + live_pnl["freight"] + live_pnl["transport"])) > 10.0) and b_total > 0:
                                    db_purch = 0.0
                                    db_frt = 0.0
                                    db_trn = 0.0
                                    db_oth = 0.0
                                    db_tax = 0.0
                                    db_tot = 0.0
                                    for row in raw_bills:
                                        b = dict(row)
                                        raw_d = str(b.get("date", "")).replace("-", "").strip()
                                        if raw_d.startswith(ym) and (b.get("zoho_status") == "synced" or bool(b.get("zoho_bill_id"))):
                                            db_tot += float(b.get("total_amount") or 0.0)
                                            db_tax += float(b.get("tax_total") or 0.0)
                                            lines = b.get("line_items") or []
                                            if isinstance(lines, str):
                                                try: lines = json.loads(lines)
                                                except: lines = []
                                            for it in lines:
                                                amt = float(it.get("amount") or 0.0)
                                                cat = _classify_bill_item(it)
                                                if cat == "transport": db_trn += amt
                                                elif cat == "freight": db_frt += amt
                                                elif cat == "other_charges": db_oth += amt
                                                else: db_purch += amt

                                    if db_purch > 0:
                                        live_pnl["purchases"] = db_purch
                                        live_pnl["freight"] = db_frt
                                        live_pnl["transport"] = db_trn
                                        live_pnl["other_charges"] = db_oth
                                        tax_calc = db_tax
                                    else:
                                        sub = live_pnl["freight"] + live_pnl["transport"] + live_pnl["other_charges"]
                                        tax_calc = max(0.0, round(b_total - sub, 2)) if (sub > 0 and b_total >= sub) else 0.0
                                        live_pnl["purchases"] = max(0.0, round(b_total - sub - tax_calc, 2))
                                else:
                                    sub = live_pnl["purchases"] + live_pnl["freight"] + live_pnl["transport"] + live_pnl["other_charges"]
                                    tax_calc = max(0.0, round(b_total - sub, 2)) if (sub > 0 and b_total >= sub) else 0.0

                                return ym, {
                                    "purchases": round(live_pnl["purchases"], 2),
                                    "freight": round(live_pnl["freight"], 2),
                                    "transport": round(live_pnl["transport"], 2),
                                    "other_charges": round(live_pnl["other_charges"], 2),
                                    "tax": round(tax_calc, 2),
                                    "total": round(b_total, 2),
                                    "count": b_count
                                }
                            except Exception as ex:
                                return ym, None

                        new_cache = dict(globals().get('_ZOHO_BILLS_ACCOUNT_RECON_CACHE', {}))
                        target_fetch_yms = [month_param] if (month_param and month_param in monthly_data) else (sorted_yms[:1] if sorted_yms else [])
                        with ThreadPoolExecutor(max_workers=3) as executor:
                            futures = [executor.submit(_fetch_zoho_live_bills_month, ym) for ym in target_fetch_yms]
                            for fut in as_completed(futures):
                                ym_res, z_data = fut.result()
                                if z_data and (z_data.get("purchases", 0) > 0 or z_data.get("total", 0) > 0 or z_data.get("count", 0) > 0):
                                    live_zoho_success = True
                                    new_cache[ym_res] = z_data
                                    m = monthly_data[ym_res]
                                    m["zoho_purchases"] = z_data["purchases"]
                                    m["zoho_freight"] = z_data["freight"]
                                    m["zoho_transport"] = z_data["transport"]
                                    m["zoho_other"] = z_data["other_charges"]
                                    m["zoho_total"] = z_data["total"]
                                    m["zoho_count"] = z_data["count"]
                                    m["zoho_tax"] = z_data["tax"]
                                    m["is_live_zoho"] = True

                        if new_cache:
                            globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE'] = new_cache
                            globals()['_ZOHO_BILLS_ACCOUNT_RECON_CACHE_TIME'] = time.time()
            except Exception as z_err:
                print(f"Error querying live Zoho Books Bills Reports API: {z_err}")

        # Fallback to local DB synced amounts ONLY if live fetch was not requested or returned 0
        for ym in sorted_yms:
            m = monthly_data[ym]
            if not m["is_live_zoho"] or m["zoho_total"] == 0:
                calc_purchases = 0.0
                calc_freight = 0.0
                calc_transport = 0.0
                calc_other = 0.0
                calc_tax = 0.0
                calc_tot = 0.0
                calc_count = 0
                for row in raw_bills:
                    b = dict(row)
                    raw_date = str(b.get("date", "")).replace("-", "").strip()
                    if raw_date.startswith(ym) and (b.get("zoho_status") == "synced" or bool(b.get("zoho_bill_id"))):
                        calc_count += 1
                        tot = float(b.get("total_amount") or 0.0)
                        tax = float(b.get("tax_total") or 0.0)
                        calc_tax += tax
                        calc_tot += tot
                        lines = b.get("line_items") or []
                        if isinstance(lines, str):
                            try: lines = json.loads(lines)
                            except: lines = []
                        for it in lines:
                            amt = float(it.get("amount") or 0.0)
                            cat = _classify_bill_item(it)
                            if cat == "transport": calc_transport += amt
                            elif cat == "freight": calc_freight += amt
                            elif cat == "other_charges": calc_other += amt
                            else: calc_purchases += amt

                if calc_count > 0:
                    m["zoho_purchases"] = round(calc_purchases, 2)
                    m["zoho_freight"] = round(calc_freight, 2)
                    m["zoho_transport"] = round(calc_transport, 2)
                    m["zoho_other"] = round(calc_other, 2)
                    m["zoho_total"] = round(calc_tot, 2)
                    m["zoho_tax"] = round(calc_tax, 2)
                    m["zoho_count"] = calc_count

        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                       "July", "August", "September", "October", "November", "December"]

        months = []
        for ym in sorted_yms:
            item = monthly_data[ym]
            y = ym[:4]
            m_num = int(ym[4:6])
            label = f"{MONTH_NAMES[m_num]} {y}"

            t_purch = round(item["tally_purchases"], 2)
            t_frt = round(item["tally_freight"], 2)
            t_trn = round(item["tally_transport"], 2)
            t_oth = round(item["tally_other"], 2)
            t_tax = round(item["tally_tax"], 2)
            t_tot = round(item["tally_total"], 2)

            z_purch = round(item["zoho_purchases"], 2)
            z_frt = round(item["zoho_freight"], 2)
            z_trn = round(item["zoho_transport"], 2)
            z_oth = round(item["zoho_other"], 2)
            z_tax = round(item["zoho_tax"], 2)
            z_tot = round(item["zoho_total"], 2)

            diff_purch = round(abs(t_purch - z_purch), 2)
            diff_frt = round(abs(t_frt - z_frt), 2)
            diff_trn = round(abs(t_trn - z_trn), 2)
            diff_oth = round(abs(t_oth - z_oth), 2)
            diff_tax = round(abs(t_tax - z_tax), 2)
            diff_tot = round(abs(t_tot - z_tot), 2)

            is_match = (diff_tot <= 0.05 and diff_purch <= 0.05 and diff_frt <= 0.05 and diff_trn <= 0.05 and diff_oth <= 0.05)

            months.append({
                "ym": ym,
                "month_label": label,
                "bill_count": item["bill_count"],
                "synced_count": item["synced_count"],
                "zoho_count": item["zoho_count"],
                "is_live_zoho": item["is_live_zoho"],
                "is_matched": is_match,
                "status": "MATCHED" if is_match else "MISMATCH",
                "tally": {
                    "purchases": t_purch,
                    "freight_charges": t_frt,
                    "transportation_charges": t_trn,
                    "other_charges": t_oth,
                    "tax": t_tax,
                    "total": t_tot
                },
                "zoho": {
                    "purchases": z_purch,
                    "freight_charges": z_frt,
                    "transportation_charges": z_trn,
                    "other_charges": z_oth,
                    "tax": z_tax,
                    "total": z_tot
                },
                "variance": {
                    "purchases": diff_purch,
                    "freight_charges": diff_frt,
                    "transportation_charges": diff_trn,
                    "other_charges": diff_oth,
                    "tax": diff_tax,
                    "total": diff_tot
                }
            })

        return jsonify({
            "status": "success",
            "is_live_connected": live_zoho_success,
            "months": months
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bills/reconciliation/account_discrepancies', methods=['GET'])
def api_bills_account_discrepancies():
    """Identifies and explains line-item / account-level discrepancies for bills in a specific month."""
    try:
        month_filter = request.args.get("month", "").strip()
        if not month_filter or len(month_filter) < 6:
            return jsonify({"error": "month parameter required (YYYYMM)"}), 400

        from journel.journel_backend import get_access_token, _get_creds
        import calendar, requests

        raw_bills = database_manager.get_all_bills()
        if not raw_bills:
            return jsonify({"status": "success", "month": month_filter, "invoices": [], "causes": []})

        def _safe_flt(val, default=0.0):
            try:
                if val is None or val == "": return default
                return float(str(val).replace(",", "").strip())
            except:
                return default

        # Query live Zoho Bills for this month
        token = get_access_token()
        zoho_live_bills = {}
        raw_zoho_bills = []
        try:
            if token and len(month_filter) == 6:
                creds = _get_creds()
                headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                year = int(month_filter[:4])
                month = int(month_filter[4:6])
                last_day = calendar.monthrange(year, month)[1]
                from_date = f"{year:04d}-{month:02d}-01"
                to_date = f"{year:04d}-{month:02d}-{last_day:02d}"

                b_res = requests.get(
                    f"{creds['base_url']}/bills",
                    headers=headers,
                    params={"organization_id": creds["org_id"], "date_start": from_date, "date_end": to_date, "per_page": 200},
                    timeout=10
                )
                if b_res.status_code == 200 and b_res.json().get("code") == 0:
                    raw_zoho_bills = b_res.json().get("bills", [])
                    for z_b in raw_zoho_bills:
                        z_no = str(z_b.get("bill_number") or "").strip()
                        zoho_live_bills[z_no] = z_b
                        clean_k = z_no.lower().replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
                        zoho_live_bills[clean_k] = z_b
        except Exception as ze:
            print(f"Error fetching live zoho bills in discrepancy check: {ze}")

        discrepancies = []
        matched_zoho_ids = set()

        for row in raw_bills:
            b = dict(row)
            raw_d = str(b.get("date") or "").replace("-", "").strip()
            if not raw_d.startswith(month_filter):
                continue

            bill_no = str(b.get("bill_number") or "").strip()
            vendor = str(b.get("vendor_name") or "").strip()
            d_str = str(b.get("date") or "").strip()
            tot = _safe_flt(b.get("total_amount"))
            tax = _safe_flt(b.get("tax_total"))

            clean_k = bill_no.lower().replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
            z_live = zoho_live_bills.get(bill_no) or zoho_live_bills.get(clean_k)

            # Match by vendor and amount if not directly keyed
            if not z_live and raw_zoho_bills:
                for cand in raw_zoho_bills:
                    if cand.get("bill_id") in matched_zoho_ids:
                        continue
                    cand_no = str(cand.get("bill_number") or "")
                    cand_k = cand_no.lower().replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
                    if clean_k == cand_k or cand_k.endswith(clean_k[-2:]) or (cand.get("vendor_name") == vendor and abs(float(cand.get("total") or 0) - tot) < 0.05):
                        z_live = cand
                        break

            if z_live and z_live.get("bill_id"):
                matched_zoho_ids.add(z_live["bill_id"])

            is_synced = bool(z_live) or (b.get("zoho_status") == "synced" or bool(b.get("zoho_bill_id")))

            lines = b.get("line_items") or []
            if isinstance(lines, str):
                try: lines = json.loads(lines)
                except: lines = []

            b_purch = 0.0
            b_freight = 0.0
            b_transport = 0.0
            b_other = 0.0

            z_purch = 0.0
            z_freight = 0.0
            z_transport = 0.0
            z_other = 0.0

            parsed_items = []

            for it in lines:
                name = str(it.get("item_name") or it.get("name") or "").strip()
                name_l = name.lower()
                amt = _safe_flt(it.get("amount"))
                qty = _safe_flt(it.get("quantity"), default=1.0)
                rate = _safe_flt(it.get("rate"), default=amt)
                is_add = it.get("is_additional_charge")

                # 1. Tally account classification
                if is_add is True:
                    if any(k in name_l for k in ["transport", "transpot"]):
                        tally_acc = "Transportation Charges"
                        b_transport += amt
                    elif any(k in name_l for k in ["freight", "fright", "cartage", "courier", "delivery"]):
                        tally_acc = "Freight Charges"
                        b_freight += amt
                    else:
                        tally_acc = "Other / Loading Charges"
                        b_other += amt
                elif any(k in name_l for k in ["unlod", "unload", "loading", "packaging box", "packaging charge", "packing charge"]):
                    tally_acc = "Other / Loading Charges"
                    b_other += amt
                else:
                    tally_acc = "Purchases (Goods/Items)"
                    b_purch += amt

                # 2. Zoho Books account mapping
                if name_l in ["transportation charges", "transport charges", "transpotation charges", "transportation charge", "transpot charges"]:
                    zoho_acc = "Transportation Charges"
                    z_transport += amt
                elif name_l in ["freight charges", "fright charges", "freight charge", "fright charge", "freight", "fright"]:
                    zoho_acc = "Freight Charges"
                    z_freight += amt
                else:
                    zoho_acc = "Purchases"
                    z_purch += amt

                is_mismatch = (tally_acc != zoho_acc and not (tally_acc == "Purchases (Goods/Items)" and zoho_acc == "Purchases"))

                parsed_items.append({
                    "item_name": name,
                    "quantity": qty,
                    "rate": rate,
                    "amount": amt,
                    "account": tally_acc,
                    "tally_account": tally_acc,
                    "zoho_account": zoho_acc,
                    "is_account_mismatch": is_mismatch
                })

            z_tot = float(z_live.get("total") or 0.0) if z_live else (tot if is_synced else 0.0)
            z_sub = float(z_live.get("sub_total") or z_tot) if z_live else (z_purch + z_freight + z_transport if is_synced else 0.0)
            z_tax = max(0.0, round(z_tot - z_sub, 2)) if z_live else (tax if is_synced else 0.0)

            diff_purch = round(abs(b_purch - z_purch), 2)
            diff_freight = round(abs(b_freight - z_freight), 2)
            diff_transport = round(abs(b_transport - z_transport), 2)
            diff_other = round(abs(b_other - z_other), 2)
            diff_tax = round(abs(tax - z_tax), 2)
            diff_tot = round(abs(tot - z_tot), 2)

            has_mismatch = (diff_purch > 0.05 or diff_transport > 0.05 or diff_freight > 0.05 or diff_other > 0.05 or diff_tot > 0.05)

            discrepancies.append({
                "invoice_number": bill_no,
                "customer_name": vendor,
                "date": d_str,
                "is_synced": is_synced,
                "is_live_found": bool(z_live),
                "has_other_charges": b_other > 0,
                "other_charges_amount": b_other,
                "has_account_mismatch": has_mismatch,
                "tally": {
                    "sales": b_purch,
                    "freight": b_freight,
                    "transport": b_transport,
                    "other_charges": b_other,
                    "tax": tax,
                    "total": tot
                },
                "zoho": {
                    "sales": z_purch,
                    "freight": z_freight,
                    "transport": z_transport,
                    "other_charges": z_other,
                    "tax": z_tax,
                    "total": z_tot
                },
                "variance": {
                    "sales": diff_purch,
                    "freight": diff_freight,
                    "transport": diff_transport,
                    "other_charges": diff_other,
                    "tax": diff_tax,
                    "total": diff_tot
                },
                "items": parsed_items
            })

        # Identify key highlights & mapping discrepancies for this month
        causes = []
        for d in discrepancies:
            tot_diff = d["variance"]["total"]
            mismatched_items = [it for it in d["items"] if it.get("is_account_mismatch")]

            if mismatched_items:
                for mi in mismatched_items:
                    causes.append({
                        "invoice_number": d["invoice_number"],
                        "date": d["date"],
                        "customer_name": d["customer_name"],
                        "amount": mi["amount"],
                        "items": mi["item_name"],
                        "tally_account": mi["tally_account"],
                        "zoho_account": mi["zoho_account"],
                        "explanation": f"Bill {d['invoice_number']} ({d['customer_name']}): Line item '{mi['item_name']}' (₹{mi['amount']:.2f}) was posted to {mi['tally_account']} in Tally, but mapped to {mi['zoho_account']} in Zoho Books."
                    })
            elif tot_diff > 0.05:
                causes.append({
                    "invoice_number": d["invoice_number"],
                    "date": d["date"],
                    "customer_name": d["customer_name"],
                    "amount": tot_diff,
                    "items": "Total Amount",
                    "tally_account": "Total",
                    "zoho_account": "Total",
                    "explanation": f"Bill {d['invoice_number']} ({d['customer_name']}): Amount variance of ₹{tot_diff:.2f} (Tally ₹{d['tally']['total']:.2f} vs Zoho ₹{d['zoho']['total']:.2f})."
                })

        # Check for any extra/duplicate live Zoho bills that don't exist in Tally
        if raw_zoho_bills:
            for z_b in raw_zoho_bills:
                z_id = z_b.get("bill_id")
                if z_id and z_id not in matched_zoho_ids:
                    z_no = str(z_b.get("bill_number") or "").strip()
                    z_tot = float(z_b.get("total") or 0.0)
                    causes.append({
                        "invoice_number": z_no,
                        "date": z_b.get("date"),
                        "customer_name": z_b.get("vendor_name"),
                        "amount": z_tot,
                        "items": "Duplicate / Extra Bill in Zoho",
                        "tally_account": "None (Not in Tally)",
                        "zoho_account": "Purchases",
                        "explanation": f"Duplicate/Extra Bill {z_no} in Zoho Books ({z_b.get('vendor_name')} for ₹{z_tot:.2f}). This bill exists in Zoho Books but not in Tally."
                    })

        return jsonify({
            "status": "success",
            "month": month_filter,
            "total_invoices": len(discrepancies),
            "causes": causes,
            "invoices": discrepancies
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bills/reconciliation/export_account_recon_excel', methods=['POST', 'GET'])
def api_bills_export_account_recon_excel():
    """Generates a downloadable Excel spreadsheet containing month-wise bills account reconciliation & line-item audits."""
    try:
        import io, openpyxl
        from datetime import datetime
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from collections import defaultdict
        import json
        from flask import send_file

        wb = openpyxl.Workbook()

        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        title_font = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
        bold_font = Font(name="Calibri", size=11, bold=True)
        regular_font = Font(name="Calibri", size=11)
        kpi_num_font = Font(name="Calibri", size=16, bold=True, color="1E3A8A")

        header_fill_blue = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_fill_dark = PatternFill(start_color="334155", end_color="334155", fill_type="solid")
        header_fill_gray = PatternFill(start_color="64748B", end_color="64748B", fill_type="solid")
        fill_tally = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid")
        fill_zoho = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid")
        fill_diff_mismatch = PatternFill(start_color="FEF2F2", end_color="FEF2F2", fill_type="solid")
        fill_kpi_card = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

        thin_gray = Side(style='thin', color='CBD5E1')
        thick_bottom = Side(style='medium', color='1E3A8A')
        border_all = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)
        border_total = Border(top=thin_gray, bottom=Side(style='double', color='1E3A8A'), left=thin_gray, right=thin_gray)

        align_left = Alignment(horizontal="left", vertical="center")
        align_right = Alignment(horizontal="right", vertical="center")
        align_center = Alignment(horizontal="center", vertical="center")

        ws1 = wb.active
        ws1.title = "Monthly Purchase Summary"
        ws1.views.sheetView[0].showGridLines = True

        ws1.merge_cells("A1:K1")
        title_cell = ws1["A1"]
        title_cell.value = "Tally vs Zoho Books: Monthly Vendor Bills & Purchase Reconciliation"
        title_cell.font = title_font
        title_cell.alignment = align_left
        ws1.row_dimensions[1].height = 28

        ws1["A2"].value = f"Generated on: {datetime.now().strftime('%d-%b-%Y %I:%M %p')} | Source: Active Database"
        ws1["A2"].font = Font(name="Calibri", size=10, italic=True, color="64748B")

        # Table headers
        ws1.merge_cells("A5:A6"); ws1["A5"] = "Month"
        ws1.merge_cells("B5:D5"); ws1["B5"] = "Purchases (Goods/Items)"
        ws1.merge_cells("E5:G5"); ws1["E5"] = "Freight Charges Inward"
        ws1.merge_cells("H5:J5"); ws1["H5"] = "Transportation Inward"
        ws1.merge_cells("K5:M5"); ws1["K5"] = "Input Taxes (GST/CST)"
        ws1.merge_cells("N5:P5"); ws1["N5"] = "Total Bills Amount"
        ws1.merge_cells("Q5:Q6"); ws1["Q5"] = "Status"

        sub_headers = [
            ("A5", "Month"),
            ("B6", "Tally (₹)"), ("C6", "Zoho (₹)"), ("D6", "Diff (₹)"),
            ("E6", "Tally (₹)"), ("F6", "Zoho (₹)"), ("G6", "Diff (₹)"),
            ("H6", "Tally (₹)"), ("I6", "Zoho (₹)"), ("J6", "Diff (₹)"),
            ("K6", "Tally (₹)"), ("L6", "Zoho (₹)"), ("M6", "Diff (₹)"),
            ("N6", "Tally (₹)"), ("O6", "Zoho (₹)"), ("P6", "Diff (₹)"),
            ("Q5", "Status")
        ]

        for col_idx in range(1, 18):
            cell5 = ws1.cell(row=5, column=col_idx)
            cell6 = ws1.cell(row=6, column=col_idx)
            cell5.font = header_font; cell5.fill = header_fill_blue; cell5.alignment = align_center; cell5.border = border_all
            cell6.font = header_font; cell6.fill = header_fill_blue; cell6.alignment = align_center; cell6.border = border_all

        for cell_ref, text in sub_headers:
            ws1[cell_ref].value = text

        ws1.row_dimensions[5].height = 20
        ws1.row_dimensions[6].height = 20

        raw_bills = database_manager.get_all_bills() or []
        monthly_map = defaultdict(lambda: {
            "count": 0, "t_purch": 0.0, "t_frt": 0.0, "t_trn": 0.0, "t_oth": 0.0, "t_tax": 0.0, "t_tot": 0.0,
            "z_purch": 0.0, "z_frt": 0.0, "z_trn": 0.0, "z_oth": 0.0, "z_tax": 0.0, "z_tot": 0.0
        })

        for row in raw_bills:
            b = dict(row)
            d = str(b.get("date") or "").replace("-", "").strip()
            if len(d) < 6: continue
            ym = d[:6]
            m = monthly_map[ym]
            m["count"] += 1
            tot = float(b.get("total_amount") or 0)
            tax = float(b.get("tax_total") or 0)
            m["t_tax"] += tax
            m["t_tot"] += tot

            lines = b.get("line_items") or []
            if isinstance(lines, str):
                try: lines = json.loads(lines)
                except: lines = []

            for it in lines:
                name_l = str(it.get("item_name") or it.get("name") or "").strip().lower()
                amt = float(it.get("amount") or 0)
                is_add = it.get("is_additional_charge")
                if is_add is True or "transport" in name_l:
                    if "transport" in name_l: m["t_trn"] += amt
                    elif "freight" in name_l or "cartage" in name_l: m["t_frt"] += amt
                    else: m["t_oth"] += amt
                elif "freight" in name_l or "cartage" in name_l:
                    m["t_frt"] += amt
                elif "unlod" in name_l or "unload" in name_l or "pack" in name_l:
                    m["t_oth"] += amt
                else:
                    m["t_purch"] += amt

        # Populate Rows
        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
        cur_row = 7

        for ym in sorted(monthly_map.keys(), reverse=True):
            m = monthly_map[ym]
            y = ym[:4]; m_num = int(ym[4:6]); label = f"{MONTH_NAMES[m_num]} {y}"

            diff_tot = abs(m["t_tot"] - m["z_tot"])
            is_match = diff_tot <= 0.05

            ws1.cell(row=cur_row, column=1, value=label).alignment = align_left
            ws1.cell(row=cur_row, column=2, value=round(m["t_purch"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=3, value=round(m["z_purch"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=4, value=round(abs(m["t_purch"] - m["z_purch"]), 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=5, value=round(m["t_frt"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=6, value=round(m["z_frt"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=7, value=round(abs(m["t_frt"] - m["z_frt"]), 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=8, value=round(m["t_trn"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=9, value=round(m["z_trn"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=10, value=round(abs(m["t_trn"] - m["z_trn"]), 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=11, value=round(m["t_tax"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=12, value=round(m["z_tax"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=13, value=round(abs(m["t_tax"] - m["z_tax"]), 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=14, value=round(m["t_tot"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=15, value=round(m["z_tot"], 2)).number_format = '#,##0.00'
            ws1.cell(row=cur_row, column=16, value=round(diff_tot, 2)).number_format = '#,##0.00'
            
            stat_cell = ws1.cell(row=cur_row, column=17, value="MATCHED" if is_match else "MISMATCH")
            stat_cell.alignment = align_center
            stat_cell.font = Font(name="Calibri", size=10, bold=True, color="16A34A" if is_match else "DC2626")

            for col_idx in range(1, 18):
                c = ws1.cell(row=cur_row, column=col_idx)
                c.border = border_all
                if col_idx in [4, 7, 10, 13, 16] and (c.value or 0) > 0.05:
                    c.fill = fill_diff_mismatch
                    c.font = Font(name="Calibri", size=11, bold=True, color="DC2626")

            ws1.row_dimensions[cur_row].height = 20
            cur_row += 1

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f"Tally_vs_Zoho_Bills_Reconciliation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bills/export_zoho_excel', methods=['POST', 'GET'])
def api_export_bills_zoho_excel():
    """Export formatted Zoho Books Bills Excel (.xlsx) file."""
    try:
        from datetime import datetime
        bills = []
        if request.is_json and request.json:
            bills = request.json.get("bills", [])
        if not bills:
            if database_manager:
                database_manager.init_db()
                bills = database_manager.get_all_bills()

        if not bills:
            return jsonify({"error": "No bills available to export."}), 400

        excel_bytes = bills_module.generate_zoho_formatted_excel(bills)
        filename = f"Zoho_Books_Formatted_Bills_{datetime.now().strftime('%Y%m%d')}.xlsx"

        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Sales Order routes
@app.route('/sales_orders')
def sales_orders_page():
    return render_template('sales_orders.html')

@app.route('/api/sales_orders/fetch', methods=['POST'])
def api_fetch_sales_orders():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        data = sales_order_module.get_all_sales_orders_data(from_date, to_date, limit)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch sales orders from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/sales_orders/sync_zoho', methods=['POST'])
def api_sync_sales_orders():
    try:
        selected = request.json.get("sales_orders") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        result = sales_order_module.sync_sales_orders_to_zoho(selected, from_date, to_date, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Purchase Order routes
@app.route('/purchase_orders')
def purchase_orders_page():
    return render_template('purchase_orders.html')

@app.route('/api/purchase_orders/fetch', methods=['POST'])
def api_fetch_purchase_orders():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        data = purchase_order_module.get_all_purchase_orders_data(from_date, to_date, limit)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch purchase orders from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/purchase_orders/sync_zoho', methods=['POST'])
def api_sync_purchase_orders():
    try:
        selected = request.json.get("purchase_orders") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        result = purchase_order_module.sync_purchase_orders_to_zoho(selected, from_date, to_date, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Payments Made routes
@app.route('/payments_made')
def payments_made_page():
    return render_template('payments_made.html')

@app.route('/api/payments_made/fetch', methods=['POST'])
def api_fetch_payments_made():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        company_name = request.json.get("company_name") if request.is_json else None
        
        data = payments_module.get_all_payments_data(from_date, to_date, limit, company_name)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch payments made from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/upload', methods=['POST'])
def api_upload_payments_made():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        import tempfile
        import os
        from datetime import datetime
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as temp:
            file.save(temp.name)
            temp_path = temp.name
            
        parsed_payments = payments_module.parse_tally_json(temp_path)
        
        # Save to SQLite
        if database_manager and parsed_payments:
            database_manager.init_db()
            db_data_list = []
            for payment in parsed_payments:
                db_data = {
                    "payment_number": payment.get("payment_number", ""),
                    "voucher_type": payment.get("voucher_type", ""),
                    "date": payment.get("date", ""),
                    "vendor_name": payment.get("vendor_name", ""),
                    "vendor_ledger_amount": payment.get("vendor_ledger_amount", 0) or 0,
                    "payment_mode": payment.get("payment_mode", ""),
                    "bank_account": payment.get("bank_account", ""),
                    "account_current_balance": payment.get("account_current_balance", 0) or 0,
                    "amount": payment.get("amount", 0) or 0,
                    "reference_number": payment.get("reference_number", ""),
                    "against_reference": payment.get("against_reference", ""),
                    "payment_category": payment.get("payment_category", ""),
                    "narration": payment.get("narration", ""),
                    "bill_allocations": json.dumps(payment.get("bill_allocations", [])),
                    "ledger_entries": json.dumps(payment.get("ledger_entries", [])),
                    "cost_center_allocations": json.dumps(payment.get("cost_center_allocations", [])),
                    "rounding_amount": payment.get("rounding_amount", 0) or 0,
                    "rounding_ledger": payment.get("rounding_ledger", ""),
                    "tally_guid": payment.get("tally_guid", ""),
                    "company_name": "",
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
                db_data_list.append(db_data)
                
            try:
                database_manager.bulk_save_payments_made(db_data_list)
            except AttributeError:
                pass
                
        os.unlink(temp_path)
        
        total_amount = sum(float(p.get("amount", 0)) for p in parsed_payments)
        return jsonify({"payments": parsed_payments, "total_amount": total_amount})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/sync_zoho', methods=['POST'])
def api_sync_payments_made():
    try:
        selected = request.json.get("payments_made") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        company_name = request.json.get("company_name") if request.is_json else None
        
        result = payments_module.sync_payments_to_zoho(selected, from_date, to_date, limit, company_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/payments_made/export_report', methods=['POST'])
def export_payments_sync_report():
    try:
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from flask import send_file

        data = request.json or {}
        errors = data.get("errors", [])
        total = data.get("total", 0)
        success_cnt = data.get("success", 0)
        failed_cnt = data.get("failed", 0)
        synced_items = data.get("synced_items", [])

        wb = openpyxl.Workbook()
        
        # ----------------------------------------------------
        # SHEET 1: Detailed Sync Log
        # ----------------------------------------------------
        ws1 = wb.active
        ws1.title = "Sync Detailed Report"
        ws1.views.sheetView[0].showGridLines = True

        # Title Header
        ws1.merge_cells("A1:G1")
        ws1["A1"] = "TALLY TO ZOHO BOOKS - PAYMENTS & EXPENSES SYNC REPORT"
        ws1["A1"].font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
        ws1["A1"].fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        ws1["A1"].alignment = Alignment(horizontal="center", vertical="center")

        ws1.append([])
        ws1.append(["Summary:", f"Total: {total}", f"Successfully Synced: {success_cnt}", f"Failed / Missing Vendors: {failed_cnt}"])
        ws1.row_dimensions[3].font = Font(name="Calibri", size=11, bold=True)

        ws1.append([])
        headers1 = ["Voucher #", "Date", "Vendor / Expense Account", "Amount (₹)", "Status", "Error Details", "Action Required"]
        ws1.append(headers1)

        header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        for col_num, header in enumerate(headers1, 1):
            cell = ws1.cell(row=5, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'), right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'), bottom=Side(style='thin', color='D9D9D9')
        )
        success_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
        fail_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

        row_idx = 6
        missing_vendors_map = {}

        # Write Failed Items
        for err in errors:
            vname = str(err.get("vendor") or "Unknown").strip()
            emsg = str(err.get("error") or "Unknown error").strip()
            pnum = str(err.get("payment_number") or "").strip()
            amt = float(err.get("amount") or 0.0)
            date_str = str(err.get("date") or "").strip()

            action = "Create Vendor Contact in Zoho Books" if "not found in Zoho Chart of Accounts" in emsg or "Vendor" in emsg else "Check Chart of Accounts Mapping"

            if vname not in missing_vendors_map:
                missing_vendors_map[vname] = {"count": 0, "total_amt": 0.0, "reason": action}
            missing_vendors_map[vname]["count"] += 1
            missing_vendors_map[vname]["total_amt"] += amt

            ws1.append([pnum, date_str, vname, amt, "FAILED", emsg, action])
            for col in range(1, 8):
                c = ws1.cell(row=row_idx, column=col)
                c.fill = fail_fill
                c.border = thin_border
                if col == 4: c.number_format = "₹#,##0.00"
                if col == 5: c.font = Font(bold=True, color="C00000")
            row_idx += 1

        # Write Successful Items
        for item in synced_items:
            vname = str(item.get("vendor_name") or item.get("vendor") or "").strip()
            pnum = str(item.get("payment_number") or "").strip()
            amt = float(item.get("amount") or 0.0)
            date_str = str(item.get("date") or "").strip()

            ws1.append([pnum, date_str, vname, amt, "SUCCESS", "Synced to Zoho Books", "None"])
            for col in range(1, 8):
                c = ws1.cell(row=row_idx, column=col)
                c.fill = success_fill
                c.border = thin_border
                if col == 4: c.number_format = "₹#,##0.00"
                if col == 5: c.font = Font(bold=True, color="375623")
            row_idx += 1

        for col in ws1.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws1.column_dimensions[col_letter].width = max(max_len + 3, 12)

        # ----------------------------------------------------
        # SHEET 2: Missing Vendors Summary
        # ----------------------------------------------------
        ws2 = wb.create_sheet(title="Missing Vendors Summary")
        ws2.views.sheetView[0].showGridLines = True

        ws2.merge_cells("A1:D1")
        ws2["A1"] = "MISSING VENDORS / UNMAPPED LEDGERS REQUIRING CREATION IN ZOHO"
        ws2["A1"].font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
        ws2["A1"].fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
        ws2["A1"].alignment = Alignment(horizontal="center", vertical="center")

        ws2.append([])
        headers2 = ["Vendor / Ledger Name", "Vouchers Affected", "Total Amount (₹)", "Required Action in Zoho"]
        ws2.append(headers2)

        for col_num, header in enumerate(headers2, 1):
            cell = ws2.cell(row=3, column=col_num)
            cell.fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center", vertical="center")

        r_idx2 = 4
        for vname, info in missing_vendors_map.items():
            ws2.append([vname, info["count"], info["total_amt"], info["reason"]])
            for col in range(1, 5):
                c = ws2.cell(row=r_idx2, column=col)
                c.border = thin_border
                if col == 3: c.number_format = "₹#,##0.00"
            r_idx2 += 1

        for col in ws2.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws2.column_dimensions[col_letter].width = max(max_len + 4, 15)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="Payments_Expenses_Zoho_Sync_Report.xlsx"
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/mark_synced', methods=['POST'])
def api_payments_made_mark_synced():
    """Manual tick/sync endpoint to mark payments made as Synced or Pending in SQLite DB."""
    try:
        from datetime import datetime
        data = request.json or {}
        payment_numbers = data.get("payment_numbers", [])
        status = str(data.get("status", "synced")).lower()
        zoho_id = "MANUALLY_SYNCED" if status == "synced" else None
        
        if not payment_numbers:
            return jsonify({"error": "No payments provided to update."}), 400
            
        if database_manager:
            database_manager.init_db()
            conn = database_manager.get_db_connection(write=True)
            cur = conn.cursor()
            updated_count = 0
            for p_no in payment_numbers:
                cur.execute(
                    "UPDATE payments_made SET zoho_payment_id = ?, zoho_status = ?, updated_at = ? WHERE payment_number = ?",
                    (zoho_id, status, datetime.now().isoformat(), str(p_no))
                )
                updated_count += cur.rowcount
            conn.commit()
            
            return jsonify({
                "success": True,
                "count": updated_count,
                "status": status,
                "message": f"Successfully marked {updated_count} payment(s) as {status}."
            })
        return jsonify({"error": "Database manager not available."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/export_zoho_excel', methods=['POST'])
def api_export_payments_made_zoho_excel():
    try:
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from datetime import datetime

        payments = request.json.get("payments_made", []) if request.is_json else []
        if not payments and database_manager:
            database_manager.init_db()
            raw_payments = database_manager.get_all_payments_made()
            payments = [dict(r) for r in raw_payments]

        if not payments:
            return jsonify({"error": "No payments made available to export."}), 400

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Payments Made"
        ws.views.sheetView[0].showGridLines = True

        headers = ["Payment #", "Payment Date", "Vendor / Account", "Payment Mode", "Bank / Paid Through", "Amount", "Reference #", "Zoho Status", "Narration"]
        ws.append(headers)

        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'), right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'), bottom=Side(style='thin', color='D9D9D9')
        )

        for row_idx, p in enumerate(payments, 2):
            raw_d = str(p.get("date") or "").replace("-", "").strip()
            date_fmt = f"{raw_d[6:8]}/{raw_d[4:6]}/{raw_d[0:4]}" if len(raw_d) == 8 else raw_d
            amt = float(p.get("amount") or 0.0)
            
            ws.append([
                p.get("payment_number", ""),
                date_fmt,
                p.get("vendor_name", ""),
                p.get("payment_mode", "Bank"),
                p.get("bank_account", ""),
                amt,
                p.get("reference_number", ""),
                p.get("zoho_status", "pending"),
                p.get("narration", "")
            ])

            for col in range(1, len(headers) + 1):
                c = ws.cell(row=row_idx, column=col)
                c.border = thin_border
                if col == 6:
                    c.number_format = "₹#,##0.00"

        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f"Zoho_Payments_Made_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/export_sync_errors_excel', methods=['POST', 'GET'])
def api_export_payments_made_sync_errors_excel():
    try:
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from datetime import datetime

        errors = []
        if request.is_json and request.json:
            errors = request.json.get("errors", [])
        
        if not errors and database_manager:
            database_manager.init_db()
            raw_payments = database_manager.get_all_payments_made()
            for r in raw_payments:
                p = dict(r)
                if p.get("zoho_status") == "failed" or p.get("zoho_status") == "error" or p.get("zoho_error"):
                    errors.append({
                        "payment_number": p.get("payment_number", ""),
                        "date": p.get("date", ""),
                        "vendor": p.get("vendor_name", ""),
                        "amount": float(p.get("amount") or 0.0),
                        "error": p.get("zoho_error", "Sync Failed")
                    })

        if not errors:
            return jsonify({"error": "No payment sync errors found to export."}), 400

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Payment Sync Errors"
        ws.views.sheetView[0].showGridLines = True

        headers = ["Payment #", "Date", "Vendor / Account", "Amount", "Error Details", "Action Required"]
        ws.append(headers)

        header_fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'), right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'), bottom=Side(style='thin', color='D9D9D9')
        )
        fail_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

        for row_idx, err in enumerate(errors, 2):
            emsg = str(err.get("error") or "Sync Failed")
            action = "Create Vendor Contact in Zoho Books" if "Vendor" in emsg or "not found" in emsg else "Verify Chart of Accounts & Paid Through mapping"
            
            raw_d = str(err.get("date") or "").replace("-", "").strip()
            date_fmt = f"{raw_d[6:8]}/{raw_d[4:6]}/{raw_d[0:4]}" if len(raw_d) == 8 else raw_d

            ws.append([
                err.get("payment_number", ""),
                date_fmt,
                err.get("vendor", ""),
                float(err.get("amount") or 0.0),
                emsg,
                action
            ])

            for col in range(1, len(headers) + 1):
                c = ws.cell(row=row_idx, column=col)
                c.fill = fail_fill
                c.border = thin_border
                if col == 4:
                    c.number_format = "₹#,##0.00"

        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f"Payments_Made_Sync_Error_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/reconciliation/monthly', methods=['GET'])
def api_payments_made_monthly_reconciliation():
    try:
        from collections import defaultdict
        import json, requests

        fetch_live = request.args.get('live', 'true').lower() == 'true'

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_payments = database_manager.get_all_payments_made()

        monthly_data = defaultdict(lambda: {
            "tally_count": 0, "tally_total": 0.0,
            "zoho_count": 0, "zoho_total": 0.0,
            "synced_count": 0, "pending_count": 0,
            "is_live_zoho": False
        })

        MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

        for row in raw_payments:
            p = dict(row)
            raw_date = str(p.get("date", "")).replace("-", "").strip()
            if len(raw_date) < 6:
                continue
            ym = raw_date[:6]
            tot = float(p.get("amount") or 0.0)
            is_synced = (p.get("zoho_status") == "synced" or bool(p.get("zoho_payment_id")))

            m = monthly_data[ym]
            m["tally_count"] += 1
            m["tally_total"] += tot

            if is_synced:
                m["synced_count"] += 1

        live_zoho_success = False
        if fetch_live:
            try:
                from journel.journel_backend import get_access_token
                from invoice.invoice_backend import _get_creds

                token = get_access_token()
                if token:
                    creds = _get_creds()
                    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                    
                    page = 1
                    has_more = True
                    zoho_live_monthly = defaultdict(lambda: {"count": 0, "total": 0.0})
                    
                    # Fetch vendorpayments from Zoho
                    while has_more and page <= 10:
                        res = requests.get(
                            f"{creds['base_url']}/vendorpayments",
                            headers=headers,
                            params={"organization_id": creds["org_id"], "page": page, "per_page": 200},
                            timeout=6
                        )
                        if res.status_code == 200 and res.json().get("code") == 0:
                            v_list = res.json().get("vendorpayments", [])
                            for z_pay in v_list:
                                z_date = str(z_pay.get("date") or "").replace("-", "").strip()
                                if len(z_date) >= 6:
                                    z_ym = z_date[:6]
                                    z_tot = float(z_pay.get("amount") or 0.0)
                                    zoho_live_monthly[z_ym]["count"] += 1
                                    zoho_live_monthly[z_ym]["total"] += z_tot
                            page_ctx = res.json().get("page_context", {})
                            has_more = page_ctx.get("has_more_page", False)
                            page += 1
                        else:
                            break

                    if zoho_live_monthly:
                        live_zoho_success = True
                        for z_ym, z_data in zoho_live_monthly.items():
                            if z_ym in monthly_data:
                                m = monthly_data[z_ym]
                                m["zoho_count"] = z_data["count"]
                                m["zoho_total"] = z_data["total"]
                                m["is_live_zoho"] = True

            except Exception as z_err:
                print(f"Error fetching live Zoho vendor payments: {z_err}")

        # Fallback to local DB synced amounts if live fetch didn't run or failed for a month
        for ym, m in monthly_data.items():
            if not m["is_live_zoho"]:
                for row in raw_payments:
                    p = dict(row)
                    raw_date = str(p.get("date", "")).replace("-", "").strip()
                    if raw_date.startswith(ym) and (p.get("zoho_status") == "synced" or bool(p.get("zoho_payment_id"))):
                        m["zoho_count"] += 1
                        m["zoho_total"] += float(p.get("amount") or 0.0)

        sorted_yms = sorted(monthly_data.keys(), reverse=True)
        result_list = []

        total_tally_sum = 0.0
        total_zoho_sum = 0.0
        matched_months = 0
        mismatched_months = 0

        for ym in sorted_yms:
            data = monthly_data[ym]
            y = ym[:4]
            m_num = int(ym[4:6])
            m_label = f"{MONTH_NAMES[m_num]} {y}"

            tally_tot = round(data["tally_total"], 2)
            zoho_tot = round(data["zoho_total"], 2)
            diff = round(abs(tally_tot - zoho_tot), 2)
            is_matched = (diff <= 0.05) and (data["tally_count"] == data["zoho_count"])

            if is_matched:
                matched_months += 1
                status = "MATCHED"
            else:
                mismatched_months += 1
                status = "MISMATCH"

            total_tally_sum += tally_tot
            total_zoho_sum += zoho_tot

            result_list.append({
                "ym": ym,
                "month_label": m_label,
                "tally_count": data["tally_count"],
                "tally_total": tally_tot,
                "zoho_count": data["zoho_count"],
                "zoho_total": zoho_tot,
                "difference": diff,
                "status": status,
                "is_matched": is_matched,
                "is_live_zoho": data["is_live_zoho"],
                "synced_count": data["synced_count"]
            })

        return jsonify({
            "status": "success",
            "live_zoho_connected": live_zoho_success,
            "summary": {
                "total_months": len(sorted_yms),
                "matched_months": matched_months,
                "mismatched_months": mismatched_months,
                "total_tally_amount": round(total_tally_sum, 2),
                "total_zoho_amount": round(total_zoho_sum, 2),
                "overall_difference": round(abs(total_tally_sum - total_zoho_sum), 2)
            },
            "months": result_list
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments_made/reconciliation/discrepancies', methods=['GET'])
def api_payments_made_discrepancies():
    try:
        month_filter = request.args.get('month', '').strip()
        if not month_filter:
            return jsonify({"error": "Month parameter required"}), 400

        if not database_manager:
            return jsonify({"error": "Database manager not initialized"}), 500

        database_manager.init_db()
        raw_payments = database_manager.get_all_payments_made()

        tally_map = {}
        for row in raw_payments:
            p = dict(row)
            raw_d = str(p.get("date") or "").replace("-", "").strip()
            if raw_d.startswith(month_filter):
                p_no = str(p.get("payment_number") or "").strip()
                if p_no:
                    tally_map[p_no] = p
                    norm_key = p_no.lower().replace("-", "_").replace(" ", "")
                    tally_map[norm_key] = p

        zoho_map = {}
        try:
            from journel.journel_backend import get_access_token
            from invoice.invoice_backend import _get_creds
            import requests

            token = get_access_token()
            if token:
                creds = _get_creds()
                headers = {"Authorization": f"Zoho-oauthtoken {token}"}
                page = 1
                has_more = True
                
                while has_more and page <= 20:
                    res = requests.get(
                        f"{creds['base_url']}/vendorpayments",
                        headers=headers,
                        params={"organization_id": creds["org_id"], "page": page, "per_page": 200},
                        timeout=10
                    )
                    if res.status_code == 200 and res.json().get("code") == 0:
                        pay_list = res.json().get("vendorpayments", [])
                        for z_pay in pay_list:
                            z_d = str(z_pay.get("date") or "").replace("-", "").strip()
                            if z_d.startswith(month_filter):
                                z_no = str(z_pay.get("payment_number") or z_pay.get("reference_number") or "").strip()
                                if z_no:
                                    zoho_map[z_no] = z_pay
                                    norm_key = z_no.lower().replace("-", "_").replace(" ", "")
                                    zoho_map[norm_key] = z_pay
                        page_ctx = res.json().get("page_context", {})
                        has_more = page_ctx.get("has_more_page", False)
                        page += 1
                    else:
                        break
        except Exception as z_err:
            print(f"Error fetching live Zoho vendor payments for discrepancy check: {z_err}")

        all_unique_nos = set()
        for k, p in tally_map.items():
            no = str(p.get("payment_number") or "").strip()
            if no: all_unique_nos.add(no)
        for k, z in zoho_map.items():
            no = str(z.get("payment_number") or z.get("reference_number") or "").strip()
            if no: all_unique_nos.add(no)

        discrepancies = []
        for p_no in sorted(all_unique_nos):
            norm_key = p_no.lower().replace("-", "_").replace(" ", "")
            t_pay = tally_map.get(p_no) or tally_map.get(norm_key)
            z_pay = zoho_map.get(p_no) or zoho_map.get(norm_key)

            t_amt = float(t_pay.get("amount") or 0.0) if t_pay else 0.0
            z_amt = float(z_pay.get("amount") or 0.0) if z_pay else 0.0
            diff = round(abs(t_amt - z_amt), 2)

            if diff > 0.05:
                v_name = (t_pay.get("vendor_name") if t_pay else (z_pay.get("vendor_name") if z_pay else "Unknown"))
                
                if t_pay and not z_pay:
                    cause = "Missing in Zoho Books"
                elif z_pay and not t_pay:
                    cause = "Present in Zoho Books only"
                elif diff <= 5.0:
                    cause = f"Minor Rounding Difference (₹{diff:.2f})"
                else:
                    cause = f"Amount Mismatch (₹{diff:.2f})"

                discrepancies.append({
                    "payment_number": p_no,
                    "vendor_name": v_name,
                    "tally_amount": t_amt,
                    "zoho_amount": z_amt,
                    "difference": diff,
                    "direction": "+" if z_amt > t_amt else "-",
                    "cause": cause
                })

        return jsonify({
            "status": "success",
            "month": month_filter,
            "total_discrepancies": len(discrepancies),
            "discrepancies": discrepancies
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Receipts (Payment Received) routes
@app.route('/receipts')
def receipts_page():
    return render_template('receipts.html')

@app.route('/api/receipts/fetch', methods=['POST'])
def api_fetch_receipts():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        company_name = request.json.get("company_name") if request.is_json else None
        
        data = receipts_module.get_all_receipts_data(from_date, to_date, limit, company_name)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch receipts from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/payments_made/fetch/start', methods=['POST'])
def api_payments_made_fetch_start():
    if not payments_module:
        return jsonify({"status": "error", "message": "Payments backend not available"}), 500
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")
    company_name = body.get("company_name")

    job = job_manager.create("payments_made_fetch")
    job.log(f"Payments Made fetch job started: {from_date} -> {to_date}")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            fn = getattr(payments_module, "get_all_payments_data_day_by_day", None) or getattr(payments_module, "get_all_payments_data", None)
            if not callable(fn):
                raise RuntimeError("Payments fetch function not available")
            if getattr(fn, "__name__", "") == "get_all_payments_data_day_by_day":
                res = fn(from_date, to_date, limit, company_name, log=job.log, stop_event=job.stop_event)
            else:
                res = fn(from_date, to_date, limit, company_name)
            status = (res or {}).get("status") or "success"
            if job.stop_event.is_set() and status == "success":
                status = "stopped"
                res["status"] = "stopped"
            job_manager.finish(job.id, "success" if status == "success" else status, result=res)
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})


@app.route('/api/receipts/fetch/start', methods=['POST'])
def api_receipts_fetch_start():
    if not receipts_module:
        return jsonify({"status": "error", "message": "Receipts backend not available"}), 500
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")
    company_name = body.get("company_name")

    job = job_manager.create("receipts_fetch")
    job.log(f"Receipts fetch job started: {from_date} -> {to_date}")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            fn = getattr(receipts_module, "get_all_receipts_data_day_by_day", None) or getattr(receipts_module, "get_all_receipts_data", None)
            if not callable(fn):
                raise RuntimeError("Receipts fetch function not available")
            res = fn(from_date, to_date, limit, company_name, log=job.log, stop_event=job.stop_event) if "day_by_day" in getattr(fn, "__name__", "") else fn(from_date, to_date, limit, company_name)
            status = (res or {}).get("status") or "success"
            if job.stop_event.is_set() and status == "success":
                status = "stopped"
                res["status"] = "stopped"
            job_manager.finish(job.id, "success" if status == "success" else status, result=res)
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/receipts/upload', methods=['POST'])
def api_upload_receipts():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        import tempfile, os, json
        from datetime import datetime

        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            receipts = receipts_module.parse_tally_json(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not receipts:
            return jsonify({"error": "No receipts found in the JSON file."}), 400

        # Save to SQLite
        if database_manager:
            database_manager.init_db()
            now = datetime.now().isoformat()
            db_data_list = []
            
            for rec in receipts:
                db_data_list.append({
                    "receipt_number": rec.get("receipt_number", ""),
                    "voucher_type": rec.get("voucher_type", ""),
                    "date": rec.get("date", ""),
                    "customer_name": rec.get("customer_name", ""),
                    "customer_ledger_amount": rec.get("customer_ledger_amount", 0) or 0,
                    "payment_mode": rec.get("payment_mode", ""),
                    "bank_account": rec.get("bank_account", ""),
                    "account_current_balance": rec.get("account_current_balance", 0) or 0,
                    "amount": rec.get("amount", 0) or 0,
                    "reference_number": rec.get("reference_number", ""),
                    "against_reference": rec.get("against_reference", ""),
                    "narration": rec.get("narration", ""),
                    "invoice_allocations": json.dumps(rec.get("invoice_allocations", [])),
                    "ledger_entries": json.dumps(rec.get("ledger_entries", [])),
                    "cost_center_allocations": json.dumps(rec.get("cost_center_allocations", [])),
                    "rounding_amount": rec.get("rounding_amount", 0) or 0,
                    "rounding_ledger": rec.get("rounding_ledger", ""),
                    "tally_guid": rec.get("tally_guid", ""),
                    "company_name": "",
                    "created_at": now,
                    "updated_at": now,
                })
            database_manager.bulk_save_receipts(db_data_list)
            
        total_amount = sum(float(rec.get("amount", 0) or 0) for rec in receipts)
        return jsonify({
            "receipts": receipts,
            "stats": {
                "total_receipts":  len(receipts),
                "total_amount": round(total_amount, 2),
                "from_date":    "UPLOAD",
                "to_date":      "UPLOAD"
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/receipts/sync_zoho', methods=['POST'])
def api_sync_receipts():
    try:
        selected = request.json.get("receipts") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        company_name = request.json.get("company_name") if request.is_json else None
        
        result = receipts_module.sync_receipts_to_zoho(selected, from_date, to_date, limit, company_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/receipts/zoho/sync/start', methods=['POST'])
def api_receipts_zoho_sync_start():
    if not receipts_module:
        return jsonify({"status": "error", "message": "Receipts backend not available"}), 500
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")
    company_name = body.get("company_name")
    receipt_numbers = body.get("receipt_numbers")
    cutoff_date = (body.get("cutoff_date") or os.environ.get("MIGRATION_CUTOFF_DATE") or "2025-03-31").strip()
    opening_invoice_id = (body.get("opening_invoice_id") or os.environ.get("OPENING_BALANCE_ZOHO_INVOICE_ID") or "").strip()

    job = job_manager.create("receipts_zoho_sync")
    job.log(f"Receipts Zoho sync started: {from_date} -> {to_date} cutoff={cutoff_date}")
    
    # Securely pin company and database to this specific job thread
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            fn = getattr(receipts_module, "sync_receipts_to_zoho_job", None)
            if not callable(fn):
                raise RuntimeError("Receipts Zoho sync function not available")
            res = fn(from_date, to_date, limit, company_name, receipt_numbers=receipt_numbers, cutoff_date=cutoff_date, opening_invoice_id=opening_invoice_id, log=job.log, stop_event=job.stop_event)
            st = (res or {}).get("status") or "success"
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/receipts/export_sync_errors_excel', methods=['POST', 'GET'])
def api_export_receipts_sync_errors_excel():
    """Exports downloadable Excel listing receipt sync errors, reasons, and fixes."""
    try:
        from datetime import datetime
        errors = []
        if request.is_json and request.json:
            errors = request.json.get("errors", [])
        
        if not errors and database_manager:
            database_manager.init_db()
            raw_receipts = database_manager.get_all_receipts()
            for r in raw_receipts:
                rec = dict(r) if not isinstance(r, dict) else r
                if rec.get("zoho_status") in ("failed", "error") or rec.get("zoho_error"):
                    errors.append({
                        "receipt_number": rec.get("receipt_number", ""),
                        "date": rec.get("date", ""),
                        "customer": rec.get("customer_name", ""),
                        "amount": float(rec.get("amount") or 0),
                        "error": rec.get("zoho_error", "Sync Failed")
                    })
        
        if not errors:
            return jsonify({"error": "No receipt sync errors found to export."}), 400
            
        excel_bytes = receipts_module.generate_sync_errors_excel(errors)
        filename = f"Receipts_Sync_Error_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/receipts/mark_synced', methods=['POST'])
def api_receipts_mark_synced():
    """Mark receipts as Synced or Pending in SQLite DB."""
    try:
        data = request.json or {}
        receipt_numbers = data.get("receipt_numbers", [])
        status = str(data.get("status", "synced")).lower()
        zoho_id = "MANUALLY_SYNCED" if status == "synced" else None
        
        if not receipt_numbers:
            return jsonify({"error": "No receipts provided to update."}), 400
        
        updated = 0
        for rn in receipt_numbers:
            try:
                database_manager.update_receipt_status(
                    receipt_number=rn,
                    zoho_payment_id=zoho_id,
                    zoho_status=status if status == "synced" else "pending",
                    zoho_error=None
                )
                updated += 1
            except Exception:
                pass
        
        return jsonify({"status": "success", "updated": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/zoho/cache_invoices', methods=['POST'])
def api_cache_zoho_invoices():
    """Fetch all Zoho invoices once and store in local SQLite table zoho_invoices_cache (Zero-API reuse)."""
    try:
        from modules.zoho_connector import zoho
        database_manager.init_db()
        
        all_zoho_invoices = []
        page = 1
        while True:
            resp = zoho.api_call("GET", "/invoices", params={"page": page, "per_page": 200})
            if resp.get("code") != 0:
                break
            items = resp.get("invoices", []) or []
            all_zoho_invoices.extend(items)
            if not resp.get("page_context", {}).get("has_more_page", False):
                break
            page += 1

        saved_count = database_manager.bulk_save_zoho_invoices_cache(all_zoho_invoices)
        return jsonify({
            "status": "success",
            "message": f"Successfully cached {saved_count} Zoho invoices locally in SQLite DB.",
            "total_cached": saved_count
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/invoices/preview_mismatches', methods=['GET'])
def api_preview_invoice_mismatches():
    """Preview invoice amount differences between Tally and Zoho Books reading directly from local SQLite cache."""
    try:
        month = request.args.get('month', '').strip()
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        database_manager.init_db()
        
        # 1. Load from local cache
        cached_zoho_invoices = database_manager.get_all_cached_zoho_invoices()
        
        # If cache is completely empty or force_refresh requested, populate it from Zoho API
        if not cached_zoho_invoices or force_refresh:
            from modules.zoho_connector import zoho
            all_zoho_invoices = []
            page = 1
            while True:
                resp = zoho.api_call("GET", "/invoices", params={"page": page, "per_page": 200})
                if resp.get("code") != 0:
                    break
                items = resp.get("invoices", []) or []
                all_zoho_invoices.extend(items)
                if not resp.get("page_context", {}).get("has_more_page", False):
                    break
                page += 1
            database_manager.bulk_save_zoho_invoices_cache(all_zoho_invoices)
            cached_zoho_invoices = database_manager.get_all_cached_zoho_invoices()

        raw_invoices = database_manager.get_all_invoices() or []
        
        # Filter by month if provided (e.g. '201604')
        if month and month != 'ALL':
            raw_invoices = [inv for inv in raw_invoices if str(inv.get("date") or "").replace("-", "").startswith(month)]

        def _clean_no(s):
            import re
            return re.sub(r'[^a-zA-Z0-9]', '', str(s or '').lower())

        zoho_map = {}
        for z in cached_zoho_invoices:
            z_no = str(z.get("invoice_number") or "").strip()
            zoho_map[z_no.lower()] = z
            zoho_map[_clean_no(z_no)] = z

        mismatches = []
        exact_matches = []
        missing_in_zoho = []

        for inv in raw_invoices:
            inv_no = str(inv.get("invoice_number") or "").strip()
            tally_amt = round(float(inv.get("total_amount") or 0), 2)
            cust = str(inv.get("customer_name") or "").strip()
            d_str = str(inv.get("date") or "").strip()

            z_match = zoho_map.get(inv_no.lower()) or zoho_map.get(_clean_no(inv_no))
            if not z_match:
                missing_in_zoho.append({
                    "invoice_number": inv_no,
                    "customer_name": cust,
                    "date": d_str,
                    "tally_amount": tally_amt,
                    "status": "MISSING_IN_ZOHO"
                })
                continue

            z_tot = round(float(z_match.get("total") or 0), 2)
            z_bal = round(float(z_match.get("balance") or 0), 2)
            diff = round(tally_amt - z_tot, 2)

            tds_amt = round(float(inv.get("tds_amount") or 0), 2)
            tds_led = str(inv.get("tds_ledger") or "").strip()

            reason = ""
            if abs(diff) >= 0.01:
                if tds_amt > 0 and abs(diff + tds_amt) <= 0.05:
                    reason = f"TDS Deduction in Tally (₹{tds_amt:,.2f})"
                elif tds_amt > 0:
                    reason = f"TDS ₹{tds_amt:,.2f} & Tax/Round difference"
                else:
                    reason = "Tax / Rounding / Charge difference"

            item_data = {
                "invoice_number": inv_no,
                "zoho_invoice_number": z_match.get("invoice_number"),
                "zoho_invoice_id": z_match.get("invoice_id"),
                "customer_name": cust,
                "date": d_str,
                "tally_amount": tally_amt,
                "zoho_total": z_tot,
                "zoho_balance": z_bal,
                "difference": diff,
                "tds_amount": tds_amt,
                "tds_ledger": tds_led,
                "reason": reason,
                "current_adjustment": float(z_match.get("adjustment") or 0)
            }

            if abs(diff) >= 0.01:
                item_data["status"] = "MISMATCH"
                mismatches.append(item_data)
            else:
                item_data["status"] = "MATCHED"
                exact_matches.append(item_data)

        return jsonify({
            "status": "success",
            "from_local_cache": True,
            "month": month,
            "total_checked": len(raw_invoices),
            "mismatch_count": len(mismatches),
            "exact_count": len(exact_matches),
            "missing_count": len(missing_in_zoho),
            "mismatches": mismatches,
            "exact_matches": exact_matches,
            "missing_in_zoho": missing_in_zoho
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/invoices/auto_adjust_mismatches', methods=['POST'])
def api_auto_adjust_invoice_mismatches():
    """Automatically update the adjustment field in Zoho Books invoices and sync local cache immediately."""
    try:
        from modules.zoho_connector import zoho
        data = request.json or {}
        invoice_ids = data.get("invoice_ids", [])
        month = data.get("month", "").strip()

        database_manager.init_db()
        raw_invoices = database_manager.get_all_invoices() or []
        if month and month != 'ALL':
            raw_invoices = [inv for inv in raw_invoices if str(inv.get("date") or "").replace("-", "").startswith(month)]

        tally_map = {str(inv.get("invoice_number") or "").strip().lower(): inv for inv in raw_invoices}

        cached_zoho_invoices = database_manager.get_all_cached_zoho_invoices()
        if not cached_zoho_invoices:
            # Fallback
            return jsonify({"error": "Zoho invoices cache is empty. Please refresh cache first."}), 400

        def _clean_no(s):
            import re
            return re.sub(r'[^a-zA-Z0-9]', '', str(s or '').lower())

        adjusted = []
        errors = []

        for z in cached_zoho_invoices:
            iid = z.get("invoice_id")
            if invoice_ids and iid not in invoice_ids:
                continue

            z_no = str(z.get("invoice_number") or "").strip()
            t_inv = tally_map.get(z_no.lower()) or tally_map.get(_clean_no(z_no))
            if not t_inv:
                continue

            tally_amt = round(float(t_inv.get("total_amount") or 0), 2)
            z_tot = round(float(z.get("total") or 0), 2)
            diff = round(tally_amt - z_tot, 2)

            if abs(diff) < 0.01:
                continue

            # Fetch full invoice for line items
            det_resp = zoho.api_call("GET", f"/invoices/{iid}")
            if det_resp.get("code") != 0:
                errors.append({"invoice_number": z_no, "error": det_resp.get("message", "Failed to fetch details")})
                continue

            full_inv = det_resp.get("invoice", {})
            sub_total = float(full_inv.get("sub_total") or 0)
            tax_total = float(full_inv.get("tax_total") or 0)
            calc_adjustment = round(tally_amt - (sub_total + tax_total), 2)

            payload = {
                "customer_id": full_inv.get("customer_id"),
                "date": full_inv.get("date"),
                "line_items": full_inv.get("line_items", []),
                "adjustment": calc_adjustment,
                "adjustment_description": "Tally Rounding / Balance Adjustment",
                "reason": "Tally migration rounding and amount reconciliation"
            }

            put_resp = zoho.api_call("PUT", f"/invoices/{iid}", payload=payload)
            if put_resp.get("code") == 0:
                up_inv = put_resp.get("invoice", {})
                new_tot = float(up_inv.get("total") or 0)
                new_bal = float(up_inv.get("balance") or 0)
                
                # Update local SQLite cache immediately (Zero-API reuse)
                database_manager.update_zoho_invoice_cache_record(
                    invoice_id=iid,
                    new_total=new_tot,
                    new_adjustment=calc_adjustment,
                    new_balance=new_bal
                )

                adjusted.append({
                    "invoice_number": z_no,
                    "customer": full_inv.get("customer_name"),
                    "old_zoho_total": z_tot,
                    "new_zoho_total": new_tot,
                    "tally_amount": tally_amt,
                    "adjustment_applied": calc_adjustment
                })
            else:
                errors.append({
                    "invoice_number": z_no,
                    "error": put_resp.get("message", "Failed to update adjustment in Zoho")
                })

        return jsonify({
            "status": "success",
            "adjusted_count": len(adjusted),
            "adjusted": adjusted,
            "errors": errors
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/receipts/export_report', methods=['POST'])
def api_export_receipts_report():
    """Export detailed sync report for receipts."""
    try:
        from datetime import datetime
        data = request.json or {}
        errors = data.get("errors", [])
        stats = data.get("stats", {})
        
        if not errors and database_manager:
            database_manager.init_db()
            raw = database_manager.get_all_receipts()
            for r in raw:
                rec = dict(r) if not isinstance(r, dict) else r
                if rec.get("zoho_status") in ("failed", "error") or rec.get("zoho_error"):
                    errors.append({
                        "receipt_number": rec.get("receipt_number", ""),
                        "date": rec.get("date", ""),
                        "customer": rec.get("customer_name", ""),
                        "amount": float(rec.get("amount") or 0),
                        "error": rec.get("zoho_error", "Sync Failed"),
                        "bank_account": rec.get("bank_account", ""),
                        "payment_mode": rec.get("payment_mode", ""),
                    })
        
        if not errors:
            return jsonify({"error": "No receipt errors found."}), 400
        
        excel_bytes = receipts_module.generate_sync_errors_excel(errors)
        filename = f"Receipts_Zoho_Sync_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/sales_orders', methods=['GET'])
def api_db_sales_orders():
    """Fetch Sales Order vouchers stored in SQLite database (tally_data.db)"""
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        orders = database_manager.get_all_sales_orders()

        json_fields = ('customer_address', 'line_items', 'taxes')
        for so in orders:
            for field in json_fields:
                if so.get(field):
                    try:
                        if isinstance(so[field], str):
                            so[field] = json.loads(so[field])
                    except Exception:
                        so[field] = []
                else:
                    so[field] = []

        total_amount = sum(float(so.get('total_amount', 0) or 0) for so in orders)
        return jsonify({
            "sales_orders": orders,
            "count":        len(orders),
            "total_amount": round(total_amount, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/purchase_orders', methods=['GET'])
def api_db_purchase_orders():
    """Fetch Purchase Order vouchers stored in SQLite database (tally_data.db)"""
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        orders = database_manager.get_all_purchase_orders()

        json_fields = ('vendor_address', 'line_items', 'taxes')
        for po in orders:
            for field in json_fields:
                if po.get(field):
                    try:
                        if isinstance(po[field], str):
                            po[field] = json.loads(po[field])
                    except Exception:
                        po[field] = []
                else:
                    po[field] = []

        total_amount = sum(float(po.get('total_amount', 0) or 0) for po in orders)
        return jsonify({
            "purchase_orders": orders,
            "count":           len(orders),
            "total_amount":    round(total_amount, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/bills', methods=['GET'])
def api_db_bills():
    """Fetch bill vouchers stored in SQLite database (tally_data.db)"""
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        bills = database_manager.get_all_bills()

        # Parse JSON columns back to lists — same pattern as receipts
        json_fields = ('vendor_address', 'line_items', 'taxes')
        for bill in bills:
            for field in json_fields:
                if bill.get(field):
                    try:
                        if isinstance(bill[field], str):
                            bill[field] = json.loads(bill[field])
                    except Exception:
                        bill[field] = []
                else:
                    bill[field] = []

        total_amount = sum(float(bill.get('total_amount', 0) or 0) for bill in bills)

        return jsonify({
            "bills":        bills,
            "count":        len(bills),
            "total_amount": round(total_amount, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/invoices', methods=['GET'])
def api_db_invoices():
    """Fetch invoice vouchers stored in SQLite database (tally_data.db)"""
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        invoices = database_manager.get_all_invoices()

        # Parse JSON columns back to lists — same pattern as receipts
        json_fields = ('buyer_address', 'line_items', 'taxes')
        for inv in invoices:
            for field in json_fields:
                if inv.get(field):
                    try:
                        if isinstance(inv[field], str):
                            inv[field] = json.loads(inv[field])
                    except Exception:
                        inv[field] = []
                else:
                    inv[field] = []

        total_amount = sum(float(inv.get('total_amount', 0) or 0) for inv in invoices)

        return jsonify({
            "invoices":     invoices,
            "count":        len(invoices),
            "total_amount": round(total_amount, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/journals', methods=['GET'])
def api_db_journals():
    """Fetch journal vouchers stored in SQLite database (tally_data.db)"""
    if not database_manager:
        return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        journals = database_manager.get_all_journals()

        # Parse line_items JSON back to list — same as receipts does for its JSON columns
        for j in journals:
            if j.get('line_items'):
                try:
                    if isinstance(j['line_items'], str):
                        j['line_items'] = json.loads(j['line_items'])
                except Exception:
                    j['line_items'] = []
            else:
                j['line_items'] = []

        total_debit  = sum(float(j.get('total_debit',  0) or 0) for j in journals)
        total_credit = sum(float(j.get('total_credit', 0) or 0) for j in journals)

        return jsonify({
            "journals":     journals,
            "count":        len(journals),
            "total_debit":  round(total_debit,  2),
            "total_credit": round(total_credit, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/payments_made', methods=['GET'])
def api_db_payments_made():
    """Fetch payments made from SQLite database"""
    try:
        payments = database_manager.get_all_payments_made()
        
        for payment in payments:
            if payment.get('bill_allocations'):
                try:
                    if isinstance(payment['bill_allocations'], str):
                        payment['bill_allocations'] = json.loads(payment['bill_allocations'])
                except Exception as e:
                    print(f"️ Error parsing bill_allocations for payment {payment.get('payment_number')}: {e}")
                    payment['bill_allocations'] = []
            else:
                payment['bill_allocations'] = []
            
            if payment.get('ledger_entries'):
                try:
                    if isinstance(payment['ledger_entries'], str):
                        payment['ledger_entries'] = json.loads(payment['ledger_entries'])
                except Exception as e:
                    print(f"️ Error parsing ledger_entries for payment {payment.get('payment_number')}: {e}")
                    payment['ledger_entries'] = []
            else:
                payment['ledger_entries'] = []
            
            if payment.get('cost_center_allocations'):
                try:
                    if isinstance(payment['cost_center_allocations'], str):
                        payment['cost_center_allocations'] = json.loads(payment['cost_center_allocations'])
                except Exception as e:
                    print(f"️ Error parsing cost_center_allocations for payment {payment.get('payment_number')}: {e}")
                    payment['cost_center_allocations'] = []
            else:
                payment['cost_center_allocations'] = []
        
        total_amount = sum(float(p.get('amount', 0) or 0) for p in payments)
        
        return jsonify({
            "payments": payments,
            "count": len(payments),
            "total_amount": total_amount
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/receipts', methods=['GET'])
def api_db_receipts():
    """Fetch receipts from SQLite database"""
    try:
        receipts = database_manager.get_all_receipts()
        
        # Parse JSON fields back to lists/dicts
        for receipt in receipts:
            # Parse invoice_allocations
            if receipt.get('invoice_allocations'):
                try:
                    if isinstance(receipt['invoice_allocations'], str):
                        receipt['invoice_allocations'] = json.loads(receipt['invoice_allocations'])
                except Exception as e:
                    print(f"️ Error parsing invoice_allocations for receipt {receipt.get('receipt_number')}: {e}")
                    receipt['invoice_allocations'] = []
            else:
                receipt['invoice_allocations'] = []
            
            # Parse ledger_entries
            if receipt.get('ledger_entries'):
                try:
                    if isinstance(receipt['ledger_entries'], str):
                        receipt['ledger_entries'] = json.loads(receipt['ledger_entries'])
                except Exception as e:
                    print(f"️ Error parsing ledger_entries for receipt {receipt.get('receipt_number')}: {e}")
                    receipt['ledger_entries'] = []
            else:
                receipt['ledger_entries'] = []
            
            # Parse cost_center_allocations
            if receipt.get('cost_center_allocations'):
                try:
                    if isinstance(receipt['cost_center_allocations'], str):
                        receipt['cost_center_allocations'] = json.loads(receipt['cost_center_allocations'])
                except Exception as e:
                    print(f"️ Error parsing cost_center_allocations for receipt {receipt.get('receipt_number')}: {e}")
                    receipt['cost_center_allocations'] = []
            else:
                receipt['cost_center_allocations'] = []
        
        # Calculate stats
        total_amount = sum(float(r.get('amount', 0) or 0) for r in receipts)
        
        return jsonify({
            "receipts": receipts,
            "count": len(receipts),
            "total_amount": total_amount
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/journals/refresh_cache', methods=['POST'])
def api_refresh_cache():
    try:
        refresh_type = request.json.get("type", "all") if request.is_json else "all"
        
        stats = {}
        
        # Refresh Tally data
        if refresh_type in ["all", "tally"]:
            ledger_map = journel_module.get_ledger_map_from_tally(use_cache=False, force_refresh=True)
            if ledger_map:
                vendors = sum(1 for t in ledger_map.values() if t == "vendor")
                customers = sum(1 for t in ledger_map.values() if t == "customer")
                accounts = sum(1 for t in ledger_map.values() if t == "account")
                stats["tally"] = {
                    "ledgers": len(ledger_map),
                    "vendors": vendors,
                    "customers": customers,
                    "others": accounts
                }
        
        # Refresh Zoho data
        if refresh_type in ["all", "zoho"]:
            token = journel_module.get_access_token()
            if token:
                # Refresh accounts (Chart of Accounts)
                account_map = journel_module.get_zoho_accounts(token, use_cache=False, force_refresh=True)
                # Refresh contacts
                contact_map = journel_module.get_zoho_contacts(token, use_cache=False, force_refresh=True)
                
                # Count contact types
                zoho_vendors = sum(1 for c in contact_map.values() if c["contact_type"] == "vendor")
                zoho_customers = sum(1 for c in contact_map.values() if c["contact_type"] == "customer")
                
                stats["zoho"] = {
                    "total_contacts": len(contact_map) if contact_map else 0,
                    "vendors": zoho_vendors,
                    "customers": zoho_customers,
                    "chart_of_accounts": len(account_map) if account_map else 0
                }
        
        return jsonify({
            "status": "success",
            "stats": stats
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------------------------------------------------------
# CONTRA ROUTES
# ---------------------------------------------------------
@app.route('/contra')
def contra_page():
    return render_template('contra.html')

@app.route('/api/contra/fetch', methods=['POST'])
def api_fetch_contra():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        data = contra_module.get_all_contra_data(from_date, to_date, limit)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch contra from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/contra/upload', methods=['POST'])
def api_upload_contra():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        import tempfile, os
        from datetime import datetime
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as temp:
            file.save(temp.name)
            temp_path = temp.name
            
        parsed_contras = contra_module.parse_tally_json(temp_path)
        
        # Save to SQLite
        if database_manager and parsed_contras:
            database_manager.init_db()
            db_data_list = []
            for contra in parsed_contras:
                db_data = {
                    "contra_number": contra.get("contra_number", ""),
                    "voucher_type": contra.get("voucher_type", "Contra"),
                    "date": contra.get("date", ""),
                    "from_account": contra.get("from_account", ""),
                    "to_account": contra.get("to_account", ""),
                    "amount": contra.get("amount", 0) or 0,
                    "narration": contra.get("narration", ""),
                    "ledger_entries": json.dumps(contra.get("ledger_entries", [])),
                    "cost_center_allocations": json.dumps(contra.get("cost_center_allocations", [])),
                    "tally_guid": contra.get("tally_guid", ""),
                    "company_name": "",
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
                db_data_list.append(db_data)
                
            try:
                database_manager.bulk_save_contra(db_data_list)
            except AttributeError:
                pass
                
        os.unlink(temp_path)
        
        total_amount = sum(float(c.get("amount", 0)) for c in parsed_contras)
        return jsonify({"contra_vouchers": parsed_contras, "total_amount": total_amount})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/contra/fetch/start', methods=['POST'])
def api_contra_fetch_start():
    if not contra_module:
        return jsonify({"status": "error", "message": "Contra backend not available"}), 500
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")
    company_name = body.get("company_name")

    job = job_manager.create("contra_fetch")
    job.log(f"Contra fetch job started: {from_date} -> {to_date}")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            fn = getattr(contra_module, "fetch_tally_contra_job", None)
            if not callable(fn):
                raise RuntimeError("Contra fetch function not available")
            res = fn(from_date, to_date, limit, company_name, log=job.log, stop_event=job.stop_event)
            status = (res or {}).get("status") or "success"
            if job.stop_event.is_set() and status == "success":
                status = "stopped"
                res["status"] = "stopped"
            job_manager.finish(job.id, "success" if status == "success" else status, result=res)
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/contra/sync_zoho', methods=['POST'])
def api_sync_contra():
    try:
        selected = request.json.get("contras") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        result = contra_module.sync_contra_to_zoho(selected, from_date, to_date, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/contra/zoho/sync/start', methods=['POST'])
def api_contra_zoho_sync_start():
    if not contra_module:
        return jsonify({"status": "error", "message": "Contra backend not available"}), 500
    if not job_manager:
        return jsonify({"status": "error", "message": "Job manager not available"}), 500

    body = request.get_json(force=True, silent=True) or {}
    from_date = body.get("from_date", "20250401")
    to_date = body.get("to_date", "20250430")
    limit = body.get("limit")
    company_name = body.get("company_name")
    contra_numbers = body.get("contra_numbers")

    job = job_manager.create("contra_zoho_sync")
    job.log(f"Contra Zoho sync started: {from_date} -> {to_date}")
    if contra_numbers:
        job.log(f"Targeting {len(contra_numbers)} selected contra voucher(s).")
    
    from modules import company_manager
    from modules.zoho_connector import set_thread_company
    active_cid = session.get('active_company_id') or company_manager.get_active_company_id()
    active_comp = company_manager.get_active_company(active_cid)
    job_db = session.get('active_db') or active_comp.get('db_name') or database_manager.get_default_db_name()
    job.log(f"🔒 Thread locked to Company: '{active_comp.get('name')}' (Org ID: {active_comp.get('org_id')}, DB: {job_db})")

    def _runner():
        database_manager.set_active_db(job_db)
        set_thread_company(active_comp)

        try:
            fn = getattr(contra_module, "sync_contra_to_zoho_job", None)
            if not callable(fn):
                raise RuntimeError("Contra Zoho sync function not available")
            res = fn(from_date, to_date, limit, company_name, contra_numbers=contra_numbers, log=job.log, stop_event=job.stop_event)
            st = (res or {}).get("status") or "success"
            if st == "success":
                job_manager.finish(job.id, "success", result=res)
            elif st == "stopped":
                job_manager.finish(job.id, "stopped", result=res, message="Stopped by user")
            else:
                job_manager.finish(job.id, "error", result=res, message=(res or {}).get("message", "Failed"))
        except Exception as e:
            job.log(f"Unhandled error: {e}")
            job_manager.finish(job.id, "error", result={"status": "error", "message": str(e)}, message=str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"status": "success", "job_id": job.id})

@app.route('/api/contra/mark_synced', methods=['POST'])
def api_contra_mark_synced():
    """Mark contra vouchers as Synced or Pending in SQLite DB."""
    try:
        data = request.json or {}
        contra_numbers = data.get("contra_numbers", [])
        status = str(data.get("status", "synced")).lower()
        zoho_id = "MANUALLY_SYNCED" if status == "synced" else None
        
        if not contra_numbers:
            return jsonify({"error": "No contra numbers provided to update."}), 400
        
        updated = 0
        for cn in contra_numbers:
            try:
                database_manager.update_contra_status(
                    contra_number=cn,
                    zoho_transfer_id=zoho_id,
                    zoho_status=status if status == "synced" else "pending",
                    zoho_error=None
                )
                updated += 1
            except Exception:
                pass
        
        return jsonify({
            "status": "success",
            "message": f"Updated {updated} contra voucher(s) to '{status}'.",
            "updated_count": updated
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/contra/export_sync_errors_excel', methods=['POST', 'GET'])
def api_export_contra_sync_errors_excel():
    """Exports downloadable Excel listing contra sync errors, reasons, and fixes."""
    try:
        from datetime import datetime
        errors = []
        if request.is_json and request.json:
            errors = request.json.get("errors", [])
        
        if not errors and database_manager:
            database_manager.init_db()
            raw_contras = database_manager.get_all_contra()
            for r in raw_contras:
                rec = dict(r) if not isinstance(r, dict) else r
                if rec.get("zoho_status") in ("failed", "error") or rec.get("zoho_error"):
                    errors.append({
                        "contra_number": rec.get("contra_number", ""),
                        "date": rec.get("date", ""),
                        "from_account": rec.get("from_account", ""),
                        "to_account": rec.get("to_account", ""),
                        "amount": float(rec.get("amount") or 0),
                        "error": rec.get("zoho_error", "Sync Failed")
                    })
        
        if not errors:
            return jsonify({"error": "No contra sync errors found to export."}), 400
            
        excel_bytes = contra_module.generate_sync_errors_excel(errors)
        filename = f"Contra_Sync_Error_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/contra', methods=['GET'])
def api_db_contra():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        c_rows = database_manager.get_all_contra()
        contras = []
        for r in c_rows:
            d = dict(r)
            if isinstance(d.get('ledger_entries'), str):
                try: d['ledger_entries'] = json.loads(d['ledger_entries'])
                except: d['ledger_entries'] = []
            if isinstance(d.get('cost_center_allocations'), str):
                try: d['cost_center_allocations'] = json.loads(d['cost_center_allocations'])
                except: d['cost_center_allocations'] = []
            contras.append(d)
                
        total_amount = sum(float(r.get('amount', 0) or 0) for r in contras)
        return jsonify({
            "contra_vouchers": contras,
            "count": len(contras),
            "total_amount": total_amount
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# CREDIT NOTE ROUTES
# ---------------------------------------------------------
@app.route('/credit_note')
def credit_note_page():
    return render_template('credit_note.html')

@app.route('/api/credit_note/fetch', methods=['POST'])
def api_fetch_credit_note():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        data = credit_note_module.get_all_credit_note_data(from_date, to_date, limit)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch credit notes from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/credit_note/upload', methods=['POST'])
def api_upload_credit_note():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        import tempfile, os
        from datetime import datetime
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as temp:
            file.save(temp.name)
            temp_path = temp.name
            
        parsed_credit_notes = credit_note_module.parse_tally_json(temp_path)
        
        # Save to SQLite
        if database_manager and parsed_credit_notes:
            database_manager.init_db()
            db_data_list = []
            for credit_note in parsed_credit_notes:
                db_data = {
                    "credit_note_number": credit_note.get("credit_note_number", ""),
                    "voucher_type": credit_note.get("voucher_type", "Credit Note"),
                    "date": credit_note.get("date", ""),
                    "from_account": credit_note.get("from_account", ""),
                    "to_account": credit_note.get("to_account", ""),
                    "amount": credit_note.get("amount", 0) or 0,
                    "narration": credit_note.get("narration", ""),
                    "ledger_entries": json.dumps(credit_note.get("ledger_entries", [])),
                    "line_items": json.dumps(credit_note.get("line_items", [])),
                    "cost_center_allocations": json.dumps(credit_note.get("cost_center_allocations", [])),
                    "tally_guid": credit_note.get("tally_guid", ""),
                    "company_name": "",
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
                db_data_list.append(db_data)
                
            try:
                database_manager.bulk_save_credit_notes(db_data_list)
            except AttributeError:
                pass
                
        os.unlink(temp_path)
        
        total_amount = sum(float(c.get("amount", 0)) for c in parsed_credit_notes)
        return jsonify({"credit_notes": parsed_credit_notes, "total_amount": total_amount})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/credit_note/sync_zoho', methods=['POST'])
def api_sync_credit_note():
    try:
        selected = request.json.get("credit_notes") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        result = credit_note_module.sync_credit_note_to_zoho(selected, from_date, to_date, limit)
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/db/credit_notes', methods=['GET'])
def api_db_credit_notes():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        c_rows = database_manager.get_all_credit_notes()
        credit_notes = []
        for r in c_rows:
            d = dict(r)
            if isinstance(d.get('ledger_entries'), str):
                try: d['ledger_entries'] = json.loads(d['ledger_entries'])
                except: d['ledger_entries'] = []
            if isinstance(d.get('line_items'), str):
                try: d['line_items'] = json.loads(d['line_items'])
                except: d['line_items'] = []
            if isinstance(d.get('cost_center_allocations'), str):
                try: d['cost_center_allocations'] = json.loads(d['cost_center_allocations'])
                except: d['cost_center_allocations'] = []
            credit_notes.append(d)
                
        total_amount = sum(float(r.get('amount', 0) or 0) for r in credit_notes)
        return jsonify({
            "credit_notes": credit_notes,
            "count": len(credit_notes),
            "total_amount": total_amount
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------
# DEBIT NOTE ROUTES
# ---------------------------------------------------------
@app.route('/debit_note')
def debit_note_page():
    return render_template('debit_note.html')

@app.route('/api/debit_note/fetch', methods=['POST'])
def api_fetch_debit_note():
    try:
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        data = debit_note_module.get_all_debit_note_data(from_date, to_date, limit)
        if data:
            return jsonify(data)
        return jsonify({"error": "Failed to fetch debit notes from Tally"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/debit_note/upload', methods=['POST'])
def api_upload_debit_note():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        import tempfile, os
        from datetime import datetime
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as temp:
            file.save(temp.name)
            temp_path = temp.name
            
        parsed_debit_notes = debit_note_module.parse_tally_json(temp_path)
        
        # Save to SQLite
        if database_manager and parsed_debit_notes:
            database_manager.init_db()
            db_data_list = []
            for debit_note in parsed_debit_notes:
                db_data = {
                    "debit_note_number": debit_note.get("debit_note_number", ""),
                    "voucher_type": debit_note.get("voucher_type", "Debit Note"),
                    "date": debit_note.get("date", ""),
                    "from_account": debit_note.get("from_account", ""),
                    "to_account": debit_note.get("to_account", ""),
                    "amount": debit_note.get("amount", 0) or 0,
                    "narration": debit_note.get("narration", ""),
                    "ledger_entries": json.dumps(debit_note.get("ledger_entries", [])),
                    "line_items": json.dumps(debit_note.get("line_items", [])),
                    "cost_center_allocations": json.dumps(debit_note.get("cost_center_allocations", [])),
                    "tally_guid": debit_note.get("tally_guid", ""),
                    "company_name": "",
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
                db_data_list.append(db_data)
                
            try:
                database_manager.bulk_save_debit_notes(db_data_list)
            except AttributeError:
                pass
                
        os.unlink(temp_path)
        
        total_amount = sum(float(c.get("amount", 0)) for c in parsed_debit_notes)
        return jsonify({"debit_notes": parsed_debit_notes, "total_amount": total_amount})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/debit_note/sync_zoho', methods=['POST'])
def api_sync_debit_note():
    try:
        selected = request.json.get("debit_notes") if request.is_json else None
        from_date = request.json.get("from_date", "20250401") if request.is_json else "20250401"
        to_date = request.json.get("to_date", "20250430") if request.is_json else "20250430"
        limit = request.json.get("limit") if request.is_json else None
        
        result = debit_note_module.sync_debit_note_to_zoho(selected, from_date, to_date, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/db/debit_notes', methods=['GET'])
def api_db_debit_notes():
    if not database_manager: return jsonify({"error": "DB Manager not loaded"}), 500
    try:
        c_rows = database_manager.get_all_debit_notes()
        debit_notes = []
        for r in c_rows:
            d = dict(r)
            if isinstance(d.get('ledger_entries'), str):
                try: d['ledger_entries'] = json.loads(d['ledger_entries'])
                except: d['ledger_entries'] = []
            if isinstance(d.get('line_items'), str):
                try: d['line_items'] = json.loads(d['line_items'])
                except: d['line_items'] = []
            if isinstance(d.get('cost_center_allocations'), str):
                try: d['cost_center_allocations'] = json.loads(d['cost_center_allocations'])
                except: d['cost_center_allocations'] = []
            debit_notes.append(d)
                
        total_amount = sum(float(r.get('amount', 0) or 0) for r in debit_notes)
        return jsonify({
            "debit_notes": debit_notes,
            "count": len(debit_notes),
            "total_amount": total_amount
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/payments/split_expenses', methods=['POST'])
def split_payments_expenses():
    if not database_manager:
        from flask import jsonify
        return jsonify({"status": "error", "error": "Database manager not available"}), 500
        
    try:
        from flask import request, jsonify
        import json as _json

        conn = database_manager.get_db_connection(write=True)
        cursor = conn.cursor()
        
        # 1. Update Vendor Payments: vendor_name exists in ledgers with type='vendor' or parent contains 'creditor'
        cursor.execute("""
            UPDATE payments_made 
            SET payment_category = 'vendor', voucher_type = 'Payment'
            WHERE vendor_name IN (
                SELECT pm.vendor_name FROM payments_made pm
                INNER JOIN ledgers l ON LOWER(TRIM(pm.vendor_name)) = LOWER(TRIM(l.name)) COLLATE NOCASE
                WHERE LOWER(l.type) = 'vendor' OR LOWER(IFNULL(l.parent, '')) LIKE '%creditor%'
            )
        """)
        vendor_updated = cursor.rowcount

        # 2. Update Expenses: all others
        cursor.execute("""
            UPDATE payments_made 
            SET payment_category = 'expense', voucher_type = 'Expense'
            WHERE payment_category != 'vendor' OR payment_category IS NULL OR payment_category = ''
        """)
        expense_updated = cursor.rowcount
        conn.commit()
        print(f" DB Update: {vendor_updated} vendor payments, {expense_updated} expenses")

        # 3. Re-read directly from DB after update - DB is the truth source
        all_updated = cursor.execute(
            "SELECT * FROM payments_made ORDER BY date DESC"
        ).fetchall()
        
        vendor_payments = []
        expenses = []
        all_payments_list = []
        for row in all_updated:
            r = dict(row)
            # SQLite stores JSON arrays as strings, parse back to lists for frontend
            for field in ['bill_allocations', 'ledger_entries', 'cost_center_allocations']:
                if r.get(field):
                    try:
                        r[field] = _json.loads(r[field])
                    except:
                        r[field] = []
                else:
                    r[field] = []
                    
            cat = (r.get('payment_category') or '').lower()
            if cat == 'vendor' or (not cat and r.get('voucher_type', '').lower() == 'payment'):
                r['payment_category'] = 'vendor'
                vendor_payments.append(r)
            else:
                r['payment_category'] = 'expense'
                expenses.append(r)
            all_payments_list.append(r)
                
        conn.close()
        return jsonify({
            "status": "success",
            "payments": all_payments_list,
            "vendor_payments": vendor_payments,
            "expenses": expenses,
            "message": f"Split complete: {len(vendor_payments)} vendor payments, {len(expenses)} expenses"
        })
    except Exception as e:
        import traceback
        from flask import jsonify
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500



# ─────────────────────────────────────────────────────────────────────────────
# OFFLINE MIGRATION TOOL — JSON Converter
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/json-converter')
def json_converter_page():
    return render_template('json_converter.html')


@app.route('/api/converter/parse-json', methods=['POST'])
def api_converter_parse_json():
    """
    Accepts:
      - json_file  (multipart) — Tally exported JSON
      - excel_file (multipart, optional) — Zoho Books sample Excel/CSV

    Returns JSON with:
      detected_type, records, raw_fields, count, errors, sample_cols
    """
    try:
        import importlib.util as _ilu
        _conv_path = os.path.join(os.path.dirname(__file__), 'modules', 'json_to_zoho_converter.py')
        _conv_spec = _ilu.spec_from_file_location('json_to_zoho_converter', _conv_path)
        conv = _ilu.module_from_spec(_conv_spec)
        _conv_spec.loader.exec_module(conv)

        # ── JSON file ──────────────────────────────────────────────────
        jf = request.files.get('json_file')
        if not jf or not jf.filename:
            return jsonify({"error": "json_file is required"}), 400

        json_bytes = jf.read()

        # ── Parse Tally JSON ───────────────────────────────────────────
        result = conv.parse_tally_json(json_bytes)

        # ── Excel sample (optional) ────────────────────────────────────
        sample_cols = []
        ef = request.files.get('excel_file')
        if ef and ef.filename:
            ef_bytes = ef.read()
            ex_result = conv.parse_sample_excel(ef_bytes)
            sample_cols = ex_result.get('columns', [])
            if ex_result.get('error'):
                result['errors'] = result.get('errors', []) + [
                    f"Sample Excel warning: {ex_result['error']}"
                ]

        result['sample_cols'] = sample_cols

        # Limit records returned (preview only — mapping uses all)
        # We send all records (export needs them), but cap at 2000 to avoid huge JSON
        MAX_RECORDS = 2000
        if len(result['records']) > MAX_RECORDS:
            result['errors'] = result.get('errors', []) + [
                f"Showing first {MAX_RECORDS} of {len(result['records'])} records"
            ]
            result['records'] = result['records'][:MAX_RECORDS]

        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/converter/export', methods=['POST'])
def api_converter_export():
    """
    Accepts JSON body:
      {
        "records":       [...],          # list of flat dicts (from parse step)
        "field_mapping": {...},          # { zoho_col: tally_field }
        "export_format": "xlsx" | "csv"
      }

    Returns: binary file download
    """
    try:
        import io as _io
        import importlib.util as _ilu
        _conv_path = os.path.join(os.path.dirname(__file__), 'modules', 'json_to_zoho_converter.py')
        _conv_spec = _ilu.spec_from_file_location('json_to_zoho_converter', _conv_path)
        conv = _ilu.module_from_spec(_conv_spec)
        _conv_spec.loader.exec_module(conv)
        from flask import send_file

        body = request.get_json(force=True)
        if not body:
            return jsonify({"error": "Request body is required"}), 400

        records       = body.get('records', [])
        field_mapping = body.get('field_mapping', {})
        export_format = body.get('export_format', 'xlsx')

        if not records:
            return jsonify({"error": "No records to export"}), 400
        if not field_mapping:
            return jsonify({"error": "field_mapping is required"}), 400

        file_bytes, mimetype, filename = conv.build_export(
            records, field_mapping, export_format
        )

        return send_file(
            _io.BytesIO(file_bytes),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route('/api/test_zoho_connection', methods=['GET'])
def api_test_zoho_connection():
    try:
        from modules.zoho_connector import zoho
        from modules import company_manager
        
        comp = company_manager.get_active_company()
        org_id = comp.get("org_id") or os.getenv("ORGANIZATION_ID")
        
        res = zoho.api_call("GET", "/organizations")
        if res.get("code") == 0:
            orgs = res.get("organizations", [])
            org_name = "Unknown Organization"
            for o in orgs:
                if str(o.get("organization_id")) == str(org_id):
                    org_name = o.get("name")
                    break
            if org_name == "Unknown Organization" and orgs:
                org_name = orgs[0].get("name")
                
            return jsonify({
                "success": True,
                "message": f"Successfully connected to Zoho Books!",
                "organization_name": org_name,
                "organization_id": org_id,
                "profile_name": comp.get("name")
            })
        else:
            return jsonify({
                "success": False,
                "message": res.get("message", "Authentication Failed or Invalid Response")
            })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/ledgers/ai_clean', methods=['POST'])
def api_ai_clean_ledgers():
    import modules.ai_cleaner as ai_cleaner
    if not database_manager:
        return jsonify({"error": "DB manager not loaded"}), 500
        
    body = request.get_json(force=True, silent=True) or {}
    names_to_clean = body.get("names", [])
    
    if not names_to_clean:
        return jsonify({"error": "No ledgers provided"}), 400
        
    ledgers = database_manager.get_all_ledgers()
    to_process = []
    for l in ledgers:
        if l["name"] in names_to_clean:
            # Only process if address exists
            addr = (l.get("address") or "").strip()
            if addr:
                to_process.append({"id": l["name"], "address": addr})
            
    if not to_process:
        return jsonify({"error": "No matching ledgers with address found"}), 400
        
    try:
        results = ai_cleaner.clean_addresses_with_ai(to_process)
        
        updated = 0
        for res in results:
            name = res.get("id")
            if not name: continue
            
            existing = database_manager.get_ledger_by_name(name)
            if existing:
                if not existing.get("original_address"):
                    existing["original_address"] = existing.get("address")
                existing["address"] = res.get("clean_address", existing.get("address"))
                if res.get("city") and not existing.get("city"): existing["city"] = res.get("city")
                if res.get("state") and not existing.get("state"): existing["state"] = res.get("state")
                if res.get("country") and not existing.get("country"): existing["country"] = res.get("country")
                if res.get("pincode") and not existing.get("pincode"): existing["pincode"] = res.get("pincode")
                if res.get("phone") and not existing.get("phone"): existing["phone"] = res.get("phone")
                if res.get("email") and not existing.get("email"): existing["email"] = res.get("email")
                
                database_manager.insert_or_update_ledger(existing)
                updated += 1
                
        return jsonify({"status": "success", "updated": updated})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    import socket
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("\n" + "="*60)
    print(" >>> TALLY TO ZOHO BOOKS MIGRATION TOOL")
    print("="*60)
    print(f" [*] Your Local PC:      http://localhost:5000")
    print(f" [*] Team / Network URL: http://{local_ip}:5000")
    print("="*60 + "\n")

    app.run(host='0.0.0.0', debug=True, port=5000)


# Trigger reload
