import sys
import sqlite3
import json
import re

def enrich_database_receipt_invoices(db_path=r"c:\Users\Zen\OneDrive\Documents\Tally Migration Backup - 28-4-2026\Think_Tree_18-22.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. Load all invoices
    invoices = conn.execute("SELECT id, invoice_number, date, customer_name, total_amount, zoho_invoice_id, zoho_status FROM invoices").fetchall()
    print(f"Loaded {len(invoices)} invoices from {db_path}")

    def norm(s):
        return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

    def extract_digits(s):
        return [int(d) for d in re.findall(r'\d+', str(s or '')) if d.isdigit()]

    def get_fy_from_date(dt_str):
        s = re.sub(r'[^0-9]', '', str(dt_str or ''))
        if len(s) >= 8:
            yr = int(s[:4])
            m = int(s[4:6])
            if m >= 4:
                return f"{str(yr)[-2:]}-{str(yr+1)[-2:]}"
            else:
                return f"{str(yr-1)[-2:]}-{str(yr)[-2:]}"
        return ""

    customer_invoices = {}
    for inv in invoices:
        c_norm = norm(inv['customer_name'])
        if c_norm not in customer_invoices:
            customer_invoices[c_norm] = []
        customer_invoices[c_norm].append(dict(inv))

    def find_customer_invoices(cust_name):
        cn = norm(cust_name)
        if cn in customer_invoices:
            return customer_invoices[cn]
        for k, invs in customer_invoices.items():
            if cn and (cn in k or k in cn):
                return invs
        stop_words = {'pvt', 'ltd', 'private', 'limited', 'the', 'and', 'for', 'llp', 'bar', 'exchange', 'ms'}
        c_tokens = {w for w in re.findall(r'[a-z0-9]+', (cust_name or '').lower()) if len(w) >= 3 and w not in stop_words}
        if c_tokens:
            best_k = None
            best_overlap = 0
            for k, invs in customer_invoices.items():
                k_tokens = {w for w in re.findall(r'[a-z0-9]+', k) if len(w) >= 3 and w not in stop_words}
                inter = c_tokens.intersection(k_tokens)
                if len(inter) >= 2 and len(inter) > best_overlap:
                    best_overlap = len(inter)
                    best_k = k
            if best_k:
                return customer_invoices[best_k]
        return []

    # 2. Fetch receipts with against_reference
    receipts = conn.execute("SELECT * FROM receipts WHERE against_reference IS NOT NULL AND against_reference != ''").fetchall()
    print(f"Total receipts to process: {len(receipts)}")

    updated_count = 0

    for r in receipts:
        r_no = r['receipt_number']
        c_name = r['customer_name']
        agst = str(r['against_reference'] or '').strip()
        amt = float(r['amount'] or 0)
        r_date = str(r['date'] or '')
        r_fy = get_fy_from_date(r_date)

        invs = find_customer_invoices(c_name)
        matched_inv = None
        match_reason = ""

        # 1. Exact match
        exact = [i for i in invs if i['invoice_number'].lower() == agst.lower()]
        if exact:
            matched_inv = exact[0]
            match_reason = "Exact Invoice Number"

        # 2. Extract digits
        if not matched_inv:
            digits = extract_digits(agst)
            if digits:
                target_num = digits[0]
                candidates = [i for i in invs if target_num in extract_digits(i['invoice_number'])]
                if len(candidates) == 1:
                    matched_inv = candidates[0]
                    match_reason = f"Unique Number Match ({target_num})"
                elif len(candidates) > 1:
                    # check FY
                    fy_matches = [i for i in candidates if r_fy and r_fy in i['invoice_number']]
                    if len(fy_matches) == 1:
                        matched_inv = fy_matches[0]
                        match_reason = f"Number ({target_num}) + FY ({r_fy}) Match"
                    else:
                        pool = fy_matches or candidates
                        amt_matches = [i for i in pool if abs(i['total_amount'] - amt) <= 10.0]
                        if amt_matches:
                            matched_inv = amt_matches[0]
                            match_reason = f"Number ({target_num}) + Amount Match"
                        elif pool:
                            matched_inv = pool[0]
                            match_reason = f"Number ({target_num}) Match"

        # 3. Prior date & Amount match fallback
        if not matched_inv:
            amt_cands = [i for i in invs if abs(i['total_amount'] - amt) <= 10.0 and str(i['date']) <= r_date]
            if amt_cands:
                matched_inv = amt_cands[0]
                match_reason = "Customer Prior Date + Amount Match"

        if matched_inv:
            resolved_inv_no = matched_inv['invoice_number']
            zoho_inv_id = matched_inv.get('zoho_invoice_id') or ''
            
            # Update invoice_allocations JSON
            try:
                raw_allocs = json.loads(r['invoice_allocations']) if r['invoice_allocations'] else []
            except Exception:
                raw_allocs = []

            if not raw_allocs:
                raw_allocs = [{"invoice_number": agst, "bill_type": "Agst Ref", "amount": amt}]

            for a in raw_allocs:
                a['raw_reference'] = agst
                a['invoice_number'] = resolved_inv_no
                a['matched_invoice_number'] = resolved_inv_no
                if zoho_inv_id:
                    a['zoho_invoice_id'] = zoho_inv_id
                a['match_reason'] = match_reason

            conn.execute(
                "UPDATE receipts SET invoice_allocations = ?, against_reference = ? WHERE receipt_number = ?",
                (json.dumps(raw_allocs), resolved_inv_no, r_no)
            )
            updated_count += 1

    conn.commit()
    conn.close()
    print(f"\n Successfully enriched {updated_count} of {len(receipts)} receipts with full invoice numbers!")

if __name__ == "__main__":
    enrich_database_receipt_invoices()
