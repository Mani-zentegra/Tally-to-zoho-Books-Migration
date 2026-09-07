# Tally-to-zoho-Books-Migration

A comprehensive, enterprise-grade migration platform to migrate financial data from **Tally ERP 9 / Tally Prime** into **Zoho Books** via automated REST APIs and XML export processing.

---

## 🚀 Key Modules & Capabilities

- **Invoices (/invoices)**: Fetch sales vouchers from Tally, validate against Zoho customer database, map line items, calculate GST/taxes, and sync directly to Zoho Books.
- **Bills (/bills)**: Extract purchase vouchers, match vendor GSTINs, and post vendor bills.
- **Payments Made (/payments_made)**: Sync vendor payments, handle cheque/bank transfers, and allocate against open vendor bills.
- **Receipts (/receipts)**: Migrate customer payments, handle advances, and match invoices.
- **Contra Vouchers (/contra)**: Automated bank-to-bank and cash-to-bank fund transfers with live terminal progress streaming.
- **Credit Notes (/credit_note) & Debit Notes (/debit_note)**: Full return and adjustment workflow sync.
- **Sales Orders (/sales_orders) & Purchase Orders (/purchase_orders)**: Complete order lifecycle migration.
- **Ledgers & Chart of Accounts (/ledgers)**: Auto-create missing Chart of Accounts, bank accounts, and customer/vendor master records.
- **Opening Balance (/opening_balance)**: Parse and reconcile trial balance PDFs with Zoho opening balances.
- **Multi-Company Architecture**: Seamlessly switch between different client databases and organizations.

---

## 🛠️ Tech Stack

- **Backend**: Python 3.11, Flask 3.0
- **Database**: SQLite3 (thread-safe multi-database manager)
- **APIs**: Zoho Books API v3 (OAuth2 with token auto-refresh & disk caching)
- **Tally Integration**: XML Request/Response over HTTP (default port 9000)
- **Frontend**: Bootstrap 5.3, Server-Sent Events (SSE) for live streaming sync consoles

---

## 📦 Quick Setup

1. **Clone the repository**:
   `ash
   git clone https://github.com/Mani-zentegra/Tally-to-zoho-Books-Migration.git
   cd Tally-to-zoho-Books-Migration
   `

2. **Install Python dependencies**:
   `ash
   pip install -r requirements.txt
   `

3. **Configure Environment Variables**:
   Copy .env.example to .env and fill in your Zoho OAuth credentials:
   `ash
   cp .env.example .env
   `

4. **Run the Application**:
   `ash
   python app.py
   `
   Open your browser and navigate to http://localhost:5000.

---

## 🔒 Security Notice

This repository contains sanitized configurations. All .env files, API client secrets, and runtime OAuth session tokens are strictly gitignored to protect client credentials.
