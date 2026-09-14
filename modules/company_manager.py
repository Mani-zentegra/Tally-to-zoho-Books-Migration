import os
import json
import re
from pathlib import Path

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config_file = os.path.join(_project_root, "companies_config.json")
_active_file = os.path.join(_project_root, ".active_company")

DEFAULT_COMPANIES = {
    "gel_frost": {
        "id": "gel_frost",
        "name": "Gel Frost Packs Kalyani Enterprises",
        "db_name": "Gel_frost_packs_kalyani_Enterprises_1-apr-2016.db",
        "client_id": "1000.P3FK274LXTD11U0X0NV732EMNO8DEU",
        "client_secret": "20a07b4f2ffc99b5f90f93b5a16af9d39b4034ef5b",
        "refresh_token": "1000.0e813b90f4c3d87c3df46dd6e0328d09.53d98faa2ffe1360008a58ffea397589",
        "org_id": "60076921838",
        "zoho_dc": "in",
        "tally_company": "Gel Frost Packs Kalyani Enterprises"
    },
    "inframart": {
        "id": "inframart",
        "name": "Inframart",
        "db_name": "inframart.db",
        "client_id": "1000.HK5ZDCPA9CNPZ61FDRS1PEU40GVGER",
        "client_secret": "14c9d6323b88e7958dfc493106abb2dd586975f621",
        "refresh_token": "1000.823c95230e67a83c2ce7aa32bd1ed814.5798b70a630d47977bf18852d0d8e3b9",
        "org_id": "60029735624",
        "zoho_dc": "in",
        "tally_company": "Inframart"
    },
    "agritough": {
        "id": "agritough",
        "name": "Agritough Machineries",
        "db_name": "agritough.db",
        "client_id": "1000.4906B4KIW4U1R10T8BB1SARC8KOL2E",
        "client_secret": "bcf8dfdad1cde0157e631bedb6d413526933c7c9f5",
        "refresh_token": "1000.11ae32a1c170293798978bcca9a42fdb.673ee49e1bb67700afa4a25e76b1d63c",
        "org_id": "60068255291",
        "zoho_dc": "in",
        "tally_company": "Agritough"
    }
}

def load_companies() -> dict:
    """Load all configured companies from disk, creating default if not existing."""
    if not os.path.exists(_config_file):
        save_all_companies(DEFAULT_COMPANIES)
        return DEFAULT_COMPANIES
    try:
        with open(_config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and data:
                return data
    except Exception as e:
        print(f"⚠️ Warning loading companies_config.json: {e}")
    return DEFAULT_COMPANIES

def save_all_companies(companies: dict):
    """Save dictionary of companies to companies_config.json."""
    try:
        with open(_config_file, "w", encoding="utf-8") as f:
            json.dump(companies, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving companies_config.json: {e}")

def get_active_company_id() -> str:
    """Get the currently selected company ID from disk or fallback to gel_frost."""
    if os.path.exists(_active_file):
        try:
            with open(_active_file, "r", encoding="utf-8") as f:
                cid = f.read().strip()
                if cid:
                    return cid
        except Exception:
            pass
    return "gel_frost"

def set_active_company_id(company_id: str):
    """Set active company ID."""
    try:
        with open(_active_file, "w", encoding="utf-8") as f:
            f.write(company_id)
    except Exception as e:
        print(f"⚠️ Error saving active company: {e}")

def get_active_company(company_id: str = None) -> dict:
    """Retrieve full configuration dictionary for active or requested company."""
    companies = load_companies()
    cid = company_id or get_active_company_id()
    if cid in companies:
        return companies[cid]
    for c in companies.values():
        if str(c.get("org_id")) == str(cid) or str(c.get("id")) == str(cid):
            return c
    active_cid = get_active_company_id()
    if active_cid in companies:
        return companies[active_cid]
    # Fallback to first company
    if companies:
        first_key = next(iter(companies))
        return companies[first_key]
    return DEFAULT_COMPANIES["gel_frost"]

def save_company(company_data: dict) -> str:
    """Add or update a company configuration."""
    companies = load_companies()
    name = str(company_data.get("name") or "New Company").strip()
    cid = str(company_data.get("id") or "").strip()
    
    if not cid:
        # Generate safe slug id
        cid = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_').lower()
        if not cid or cid in companies:
            cid = f"{cid}_{int(os.path.getmtime(_config_file) if os.path.exists(_config_file) else 1)}"

    db_name = str(company_data.get("db_name") or f"{cid}.db").strip()
    if not db_name.lower().endswith(".db"):
        db_name = f"{db_name}.db"

    zoho_dc = str(company_data.get("zoho_dc") or "in").strip().lower()

    companies[cid] = {
        "id": cid,
        "name": name,
        "db_name": db_name,
        "client_id": str(company_data.get("client_id") or "").strip(),
        "client_secret": str(company_data.get("client_secret") or "").strip(),
        "refresh_token": str(company_data.get("refresh_token") or "").strip(),
        "org_id": str(company_data.get("org_id") or "").strip(),
        "zoho_dc": zoho_dc,
        "tally_company": str(company_data.get("tally_company") or name).strip()
    }
    save_all_companies(companies)
    return cid

def delete_company(company_id: str) -> bool:
    """Remove a company profile from registry."""
    companies = load_companies()
    if company_id in companies:
        del companies[company_id]
        save_all_companies(companies)
        if get_active_company_id() == company_id and companies:
            set_active_company_id(next(iter(companies)))
        return True
    return False
