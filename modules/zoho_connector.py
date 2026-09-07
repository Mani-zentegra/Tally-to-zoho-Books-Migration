import requests
import os
import time
import sys
import random
from dotenv import load_dotenv

# Explicitly load .env from the project root (one level up from modules/)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_project_root, ".env")
load_dotenv(_env_path, override=True)

# ── Rate limit settings ──────────────────────────────────────────────────────
def _env_float(key: str, default: float) -> float:
    try:
        return float((os.getenv(key) or "").strip() or default)
    except Exception:
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(float((os.getenv(key) or "").strip() or default))
    except Exception:
        return default

# Defaults are conservative to avoid 429 during bulk imports.
API_CALL_DELAY = _env_float("ZOHO_API_CALL_DELAY", 0.6)  # seconds between calls
RATE_LIMIT_BACKOFF = _env_int("ZOHO_RATE_LIMIT_BACKOFF", 20)  # seconds to wait on 429 / busy
MAX_RETRIES = _env_int("ZOHO_MAX_RETRIES", 5)  # retry attempts for rate-limited calls
JITTER_MAX_SECONDS = _env_float("ZOHO_API_JITTER_MAX", 0.15)  # prevents burst patterns

# ── Dynamic credential loader — re-reads .env on every call ─────────────────
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_project_root, ".env")

import contextvars

# ContextVar for thread/request level company override
_THREAD_COMPANY_VAR = contextvars.ContextVar("active_company_override", default=None)

def set_thread_company(company_dict: dict):
    """Binds a specific company config to the current execution thread/coroutine context."""
    _THREAD_COMPANY_VAR.set(company_dict)

def get_thread_company() -> dict:
    return _THREAD_COMPANY_VAR.get()

def _get_creds(company_override=None):
    """Reads credentials dynamically with strict thread/session isolation."""
    try:
        from modules import company_manager
        active_comp = company_override or get_thread_company()
        if not active_comp:
            # Check Flask session if in web request context
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
            api_domain      = "www.zohoapis.in"   if zoho_dc == "in" else "www.zohoapis.com"
            return {
                "company_id":     active_comp.get("id", ""),
                "company_name":   active_comp.get("name", ""),
                "client_id":      active_comp.get("client_id", "").strip(),
                "client_secret":  active_comp.get("client_secret", "").strip(),
                "refresh_token":  active_comp.get("refresh_token", "").strip(),
                "access_token":   active_comp.get("access_token", "").strip(),
                "org_id":         active_comp.get("org_id", "").strip(),
                "auth_url":       f"https://{accounts_domain}/oauth/v2/token",
                "base_url":       f"https://{api_domain}/books/v3",
                "accounts_domain": accounts_domain,
                "api_domain":     api_domain,
            }
    except Exception as e:
        pass

    load_dotenv(_env_path, override=True)
    zoho_dc = (os.getenv("ZOHO_DC") or "in").strip().lower()
    accounts_domain = "accounts.zoho.in" if zoho_dc == "in" else "accounts.zoho.com"
    api_domain      = "www.zohoapis.in"   if zoho_dc == "in" else "www.zohoapis.com"
    return {
        "company_id":     "env",
        "company_name":   "env",
        "client_id":      (os.getenv("CLIENT_ID")       or "").strip(),
        "client_secret":  (os.getenv("CLIENT_SECRET")   or "").strip(),
        "refresh_token":  (os.getenv("REFRESH_TOKEN")   or "").strip(),
        "access_token":   (os.getenv("ACCESS_TOKEN")    or "").strip(),
        "org_id":         (os.getenv("ORGANIZATION_ID") or "").strip(),
        "auth_url":       f"https://{accounts_domain}/oauth/v2/token",
        "base_url":       f"https://{api_domain}/books/v3",
        "accounts_domain": accounts_domain,
        "api_domain":     api_domain,
    }

# Legacy names for any external code that imports these directly
def _accounts_domain(): return _get_creds()["accounts_domain"]
def _base_url():        return _get_creds()["base_url"]

class ZohoConnector:
    def __init__(self):
        self._tokens_cache = {}  # org_id -> {"token": ..., "expiry": ...}
        self._last_call_time = 0

    def set_thread_company(self, company_dict: dict):
        """Binds a specific company dictionary to the current thread."""
        set_thread_company(company_dict)

    def get_access_token(self, force_refresh=False, company_override=None):
        """Returns a valid access token for the active company/org in the current thread context."""
        creds = _get_creds(company_override=company_override)
        current_org = creds["org_id"]
        auth_url = creds["auth_url"]
        client_id = creds["client_id"]
        client_secret = creds["client_secret"]
        refresh_token = creds["refresh_token"]

        cached = self._tokens_cache.get(current_org, {})
        cached_token = cached.get("token")
        cached_expiry = cached.get("expiry", 0)

        cache_file = os.path.join(os.path.dirname(__file__), ".token_cache.json")
        if not cached_token and not force_refresh and os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    disk_cache = json.load(f)
                    c_entry = disk_cache.get(str(current_org), {})
                    if c_entry.get("token") and time.time() < c_entry.get("expiry", 0):
                        self._tokens_cache[current_org] = c_entry
                        cached_token = c_entry.get("token")
                        cached_expiry = c_entry.get("expiry", 0)
            except Exception:
                pass

        # Return cached token if still valid for this specific org
        if not force_refresh and cached_token and time.time() < cached_expiry:
            return cached_token

        # Generate fresh token via refresh_token OAuth grant
        if client_id and client_secret and refresh_token:
            params = {
                "refresh_token": refresh_token,
                "client_id":     client_id,
                "client_secret": client_secret,
                "grant_type":    "refresh_token"
            }
            try:
                resp = requests.post(auth_url, data=params, timeout=15)
                data = resp.json()
                if "access_token" in data:
                    token = data["access_token"]
                    entry = {
                        "token": token,
                        "expiry": time.time() + (data.get("expires_in", 3600) - 60)
                    }
                    self._tokens_cache[current_org] = entry
                    try:
                        disk_cache = {}
                        if os.path.exists(cache_file):
                            with open(cache_file, 'r', encoding='utf-8') as f:
                                disk_cache = json.load(f)
                        disk_cache[str(current_org)] = entry
                        with open(cache_file, 'w', encoding='utf-8') as f:
                            json.dump(disk_cache, f)
                    except Exception:
                        pass
                    return token
                else:
                    print(f" Token refresh failed for Org {current_org}: {data}")
            except Exception as e:
                print(f" Connection error during Zoho auth for Org {current_org}: {e}")

        # Fallback: static ACCESS_TOKEN if provided
        env_token = (creds.get("access_token") or "").strip()
        if env_token:
            self._tokens_cache[current_org] = {
                "token": env_token,
                "expiry": time.time() + 300
            }
            return env_token

        print(f" No valid Zoho credentials for Org '{current_org}'. client_id={bool(client_id)}, refresh_token={bool(refresh_token)}")
        return None

    def get_headers(self):
        token = self.get_access_token()
        if not token:
            return None
        return {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Content-Type":  "application/json"
        }

    def _throttle(self):
        elapsed = time.time() - self._last_call_time
        delay = max(0.0, API_CALL_DELAY - elapsed)
        if delay > 0:
            time.sleep(delay + (random.random() * JITTER_MAX_SECONDS))
        self._last_call_time = time.time()

    def api_call(self, method, endpoint, payload=None, params=None):
        """
        Makes a Zoho Books API call with throttling and auto-retry.
        """
        # Ensure endpoint starts with /
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint
            
        url = f"{_get_creds()['base_url']}{endpoint}"
        if not params:
            params = {}
        
        # Always read org_id fresh from .env
        self.org_id = _get_creds()["org_id"]
        params["organization_id"] = self.org_id

        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()

            headers = self.get_headers()
            if not headers:
                return {"code": 1, "message": "Auth Failed"}

            try:
                print(f" Zoho API {method} {url} | Params: {params if params else '{}'}")
                req_headers = headers.copy()
                
                if method == "GET":
                    resp = requests.get(url, headers=req_headers, params=params, timeout=30)
                elif method == "POST":
                    if isinstance(payload, dict) and "JSONString" in payload:
                        req_headers.pop("Content-Type", None)
                        resp = requests.post(url, headers=req_headers, params=params, data=payload, timeout=30)
                    else:
                        resp = requests.post(url, headers=req_headers, params=params, json=payload, timeout=30)
                elif method == "PUT":
                    if isinstance(payload, dict) and "JSONString" in payload:
                        req_headers.pop("Content-Type", None)
                        resp = requests.put(url, headers=req_headers, params=params, data=payload, timeout=30)
                    else:
                        resp = requests.put(url, headers=req_headers, params=params, json=payload, timeout=30)
                elif method == "DELETE":
                    resp = requests.delete(url, headers=req_headers, params=params, timeout=30)
                else:
                    return {"code": 1, "message": f"Unknown method: {method}"}

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = int(float(retry_after)) if retry_after else (RATE_LIMIT_BACKOFF * attempt)
                    except Exception:
                        wait = RATE_LIMIT_BACKOFF * attempt
                    time.sleep(wait)
                    continue

                try:
                    result = resp.json()
                    print(f" Zoho Response: {result.get('code')} - {result.get('message')}")
                except:
                    print(f" Zoho Response (Raw): {resp.text[:200]}")
                    return {"code": 1, "message": f"Non-JSON response: {resp.status_code}"}

                if result.get("code") in (429, 57, 58):
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = int(float(retry_after)) if retry_after else (RATE_LIMIT_BACKOFF * attempt)
                    except Exception:
                        wait = RATE_LIMIT_BACKOFF * attempt
                    time.sleep(wait)
                    continue

                if result.get("code") == 14 or "invalid_token" in str(result.get("message", "")):
                    self.access_token = None
                    continue

                return result

            except Exception as e:
                print(f" Zoho API Error: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RATE_LIMIT_BACKOFF)
                    continue
                return {"code": 1, "message": str(e)}

        return {"code": 1, "message": "Max retries exceeded — Zoho rate limit"}

    def get_reporting_tags(self):
        """Fetch and cache all Zoho Reporting Tags and options"""
        if hasattr(self, '_tags_cache') and self._tags_cache is not None:
            return self._tags_cache
            
        resp = self.api_call("GET", "/settings/tags")
        tags = []
        if resp.get("code") == 0:
            for t in resp.get("reporting_tags", []):
                tag_id = t["tag_id"]
                t_resp = self.api_call("GET", f"/settings/tags/{tag_id}")
                if t_resp.get("code") == 0:
                    options = t_resp.get("reporting_tag", {}).get("tag_options", [])
                    tag_data = {
                        "tag_id": str(tag_id),
                        "tag_name": str(t.get("tag_name", "")).lower(),
                        "options": {str(o.get("tag_option_name", "")).lower(): str(o.get("tag_option_id", "")) for o in options}
                    }
                    tags.append(tag_data)
                
        self._tags_cache = tags
        return tags


# Singleton instance used across the app
zoho = ZohoConnector()
