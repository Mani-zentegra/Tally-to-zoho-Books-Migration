"""
field_mapping_manager.py
Handles saving/loading Zoho Books field lists and DB-to-Zoho field mappings per module.
Stored as JSON files in the ./field_mappings/ directory.
"""
import json
import os

MAPPINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "field_mappings")
os.makedirs(MAPPINGS_DIR, exist_ok=True)


def _path(module: str) -> str:
    return os.path.join(MAPPINGS_DIR, f"{module}.json")


def save_zoho_fields(module: str, zoho_fields: list, sample_values: dict = None):
    """Save Zoho Books field names for a module (from imported XLSX headers or API)."""
    data = load(module)
    data["zoho_fields"] = zoho_fields
    if sample_values is not None:
        data["zoho_sample_values"] = sample_values
    _write(module, data)


def save_mapping(module: str, mapping: dict):
    """Save the user-defined DB-field → Zoho-field mapping."""
    data = load(module)
    data["mapping"] = mapping
    _write(module, data)


def load(module: str) -> dict:
    p = _path(module)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"zoho_fields": [], "mapping": {}, "zoho_sample_values": {}}


def has_zoho_fields(module: str) -> bool:
    data = load(module)
    return bool(data.get("zoho_fields"))


def _write(module: str, data: dict):
    with open(_path(module), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
