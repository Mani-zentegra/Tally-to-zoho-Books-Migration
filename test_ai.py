import sys
import os
import json
import sqlite3

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

try:
    from modules.ai_invoice_matcher import extract_invoice_details_from_narration, ai_resolve_invoice_candidate
except ImportError:
    sys.path.append(os.path.dirname(__file__))
    from modules.ai_invoice_matcher import extract_invoice_details_from_narration, ai_resolve_invoice_candidate

def test_custom_narration():
    print("\n" + "="*60)
    print(" [AI TEST] TEST NVIDIA AI WITH CUSTOM NARRATION")
    print("="*60)
    narration = input("\nEnter payment narration (e.g. 'NEFT recd for bill 14 less 2% TDS'):\n> ").strip()
    if not narration:
        narration = "Being NEFT recd agst inv no TTMH/282/21-22 & 283 less 2% TDS on professional fees"
        print(f"Using default sample narration:\n'{narration}'")
    
    invoices_input = input("\nEnter available invoices comma-separated (e.g. '14, 15, 16') or press Enter for defaults:\n> ").strip()
    if invoices_input:
        invoices = [inv.strip() for inv in invoices_input.split(",") if inv.strip()]
    else:
        invoices = ["TTMH/282/21-22", "TTMH/283/21-22", "TTMH/290/21-22", "14", "15"]

    print("\nCalling NVIDIA NIM AI (google/diffusiongemma-26b-a4b-it)...")
    res = extract_invoice_details_from_narration(narration, [{"invoice_number": inv} for inv in invoices])
    
    print("\n" + "="*60)
    print(" [SUCCESS] AI RESULT RECEIVED:")
    print("="*60)
    print(json.dumps(res, indent=2))
    print(f"\n- Matched Invoices: {res.get('matched_invoices')}")
    print(f"- TDS Rate:         {res.get('tds_rate_pct')}%")
    print(f"- Cheque / Ref:     {res.get('cheque_or_ref')}")
    print(f"- Confidence:       {res.get('confidence')}")
    print(f"- Explanation:      {res.get('explanation')}")
    print("="*60)

def test_live_db_receipts():
    print("\n" + "="*60)
    print(" [DB TEST] TEST NVIDIA AI ON LIVE RECEIPTS FROM Think_Tree_18-22.db")
    print("="*60)
    try:
        conn = sqlite3.connect("Think_Tree_18-22.db")
        c = conn.cursor()
        c.execute("""
            SELECT receipt_number, customer_name, narration, amount 
            FROM receipts 
            WHERE narration IS NOT NULL AND LENGTH(narration) > 10
            ORDER BY RANDOM() 
            LIMIT 3
        """)
        rows = c.fetchall()
        conn.close()
    except Exception as e:
        print(f"Error accessing DB: {e}")
        return

    for idx, (r_no, cust, narr, amt) in enumerate(rows, 1):
        print(f"\n--- Receipt #{idx}: {r_no} ---")
        print(f"Customer:  {cust}")
        print(f"Amount:    Rs. {amt:,.2f}")
        print(f"Narration: {narr}")
        print("Analyzing with AI...")
        
        dummy_invoices = [{"invoice_number": f"{cust[:4].upper()}/01/18-19", "total": amt}]
        res = extract_invoice_details_from_narration(narr, dummy_invoices, amt, amt)
        print("AI Output:")
        print(f"  * Invoices Detected: {res.get('matched_invoices')}")
        print(f"  * TDS Detected:      {res.get('tds_rate_pct')}%")
        print(f"  * Cheque/Ref:        {res.get('cheque_or_ref')}")
        print(f"  * Confidence:        {res.get('confidence')}")
        print(f"  * Summary:           {res.get('explanation')}")

if __name__ == "__main__":
    print("="*60)
    print("       NVIDIA NIM AI MIGRATION TESTER")
    print("="*60)
    print("1. Test with your own custom narration (Interactive)")
    print("2. Test 3 random receipts from database")
    choice = input("\nEnter choice [1 or 2] (default 1): ").strip()
    if choice == "2":
        test_live_db_receipts()
    else:
        test_custom_narration()
