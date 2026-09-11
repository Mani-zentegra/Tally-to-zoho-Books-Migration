import os
import re
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "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")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "google/diffusiongemma-26b-a4b-it")
NVIDIA_FALLBACK_MODEL = os.getenv("NVIDIA_FALLBACK_MODEL", "deepseek-ai/deepseek-v4-flash-0731")
BASE_URL = "https://integrate.api.nvidia.com/v1"

# In-memory cache to avoid duplicate API requests during batch syncs
_CACHE = {}

def _clean_json_content(content: str) -> str:
    """Extract raw JSON string from potentially markdown-wrapped model output."""
    if not content:
        return ""
    text = content.strip()
    # Remove markdown code blocks ```json ... ```
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()
    return text

def _extract_digits(val) -> list:
    return [int(d) for d in re.findall(r"\d+", str(val or "")) if d.isdigit()]

def extract_invoice_details_from_narration(narration: str, open_invoices: list, receipt_amount: float = 0.0, net_amount: float = 0.0) -> dict:
    """
    Invokes NVIDIA NIM LLM to intelligently analyze payment narrations against available invoices.
    
    Returns structured dict:
    {
      "matched_invoices": ["inv_no_1", ...],
      "cheque_or_ref": "str or None",
      "tds_rate_pct": float or None,
      "confidence": float (0.0 to 1.0),
      "explanation": "str"
    }
    """
    if not narration or not narration.strip():
        return {
            "matched_invoices": [],
            "cheque_or_ref": None,
            "tds_rate_pct": None,
            "confidence": 0.0,
            "explanation": "Empty narration"
        }

    # Normalize open_invoices list for cache key & prompt
    clean_invs = []
    for inv in open_invoices:
        if isinstance(inv, dict):
            inv_no = inv.get("invoice_number", "")
            inv_tot = inv.get("total", 0)
            inv_bal = inv.get("balance", inv_tot)
            clean_invs.append({"invoice_number": inv_no, "total": inv_tot, "balance": inv_bal})
        elif isinstance(inv, str):
            clean_invs.append({"invoice_number": inv})

    cache_key = f"{narration.strip()}||{json.dumps([i.get('invoice_number') for i in clean_invs], sort_keys=True)}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json"
    }

    system_prompt = (
        "You are an expert Indian accounting AI specialized in invoice reconciliation for Zoho Books.\n"
        "Given a payment narration and the customer's open invoices:\n"
        "1. Identify which open invoices match the narration (e.g., 'bill 14', 'inv 282', 'TTMH/282/21-22').\n"
        "2. Detect any TDS deduction percentage or amount mentioned (e.g. 'less 2% TDS', '10% tds').\n"
        "3. Detect any cheque or transaction reference number (e.g. 'CH No. 000289', 'NEFT', 'UTR').\n"
        "Return ONLY a valid JSON object with the following schema:\n"
        "{\n"
        '  "matched_invoices": ["invoice_number_1", ...],\n'
        '  "cheque_or_ref": "string or null",\n'
        '  "tds_rate_pct": number or null,\n'
        '  "confidence": number between 0.0 and 1.0,\n'
        '  "explanation": "short concise reason"\n'
        "}\n"
        "Output strictly raw JSON without explanations outside the JSON."
    )

    user_content = (
        f"Payment Narration: {narration}\n"
        f"Receipt Amount: Rs. {receipt_amount}\n"
        f"Net Bank Amount: Rs. {net_amount}\n"
        f"Customer's Available Invoices: {json.dumps(clean_invs[:25], ensure_ascii=False)}"
    )

    models_to_try = [NVIDIA_MODEL, NVIDIA_FALLBACK_MODEL, "nvidia/nemotron-3-super-120b-a12b"]
    # De-duplicate while preserving order
    seen_m = set()
    models_to_try = [m for m in models_to_try if m and not (m in seen_m or seen_m.add(m))]

    last_error = None
    for model_name in models_to_try:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.05,
            "max_tokens": 500
        }

        try:
            res = requests.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                raw_text = res.json()["choices"][0]["message"]["content"]
                cleaned = _clean_json_content(raw_text)
                parsed = json.loads(cleaned)
                
                # Standardize output types
                result = {
                    "matched_invoices": [str(x) for x in parsed.get("matched_invoices", []) if x],
                    "cheque_or_ref": parsed.get("cheque_or_ref"),
                    "tds_rate_pct": float(parsed["tds_rate_pct"]) if parsed.get("tds_rate_pct") is not None else None,
                    "confidence": float(parsed.get("confidence", 0.0)),
                    "explanation": str(parsed.get("explanation", ""))
                }
                _CACHE[cache_key] = result
                return result
            else:
                last_error = f"HTTP {res.status_code}: {res.text}"
        except Exception as e:
            last_error = str(e)
            continue

    fallback_res = {
        "matched_invoices": [],
        "cheque_or_ref": None,
        "tds_rate_pct": None,
        "confidence": 0.0,
        "explanation": f"AI call failed: {last_error}"
    }
    return fallback_res

def ai_resolve_invoice_candidate(narration: str, cust_invoices: list, receipt_amount: float = 0.0, net_amount: float = 0.0) -> tuple:
    """
    Uses NVIDIA NIM AI to match the narration against customer invoices.
    Returns: (matched_invoice_dict, reason_str) or (None, reason_str)
    """
    if not narration or not cust_invoices:
        return None, "No narration or open invoices"

    ai_data = extract_invoice_details_from_narration(narration, cust_invoices, receipt_amount, net_amount)
    matched_inv_nos = ai_data.get("matched_invoices", [])
    confidence = ai_data.get("confidence", 0.0)
    explanation = ai_data.get("explanation", "")
    tds_pct = ai_data.get("tds_rate_pct")

    if not matched_inv_nos or confidence < 0.5:
        return None, f"AI low confidence or no match ({explanation})"

    # Find the matching invoice dict from cust_invoices
    for target_no in matched_inv_nos:
        target_str = str(target_no).strip().lower()
        target_digits = _extract_digits(target_str)

        # 1. Exact match on invoice_number
        for inv in cust_invoices:
            inv_no = str(inv.get("invoice_number", "")).strip().lower()
            if inv_no == target_str:
                tds_str = f" [AI detected {tds_pct}% TDS]" if tds_pct else ""
                return inv, f"Condition AI: NVIDIA NIM Matched '{inv.get('invoice_number')}' (Conf: {confidence:.2f}){tds_str}: {explanation}"

        # 2. Substring match or digit match
        for inv in cust_invoices:
            inv_no = str(inv.get("invoice_number", "")).strip().lower()
            inv_digits = _extract_digits(inv_no)
            if (target_str in inv_no) or (target_digits and any(d in inv_digits for d in target_digits)):
                tds_str = f" [AI detected {tds_pct}% TDS]" if tds_pct else ""
                return inv, f"Condition AI: NVIDIA NIM Matched '{inv.get('invoice_number')}' for target '{target_no}' (Conf: {confidence:.2f}){tds_str}: {explanation}"

    return None, f"AI suggested {matched_inv_nos} but none found in customer invoices"
