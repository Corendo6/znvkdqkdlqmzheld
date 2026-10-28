# -*- coding: utf-8 -*-
"""쿠팡 관리 프로그램 - Excel 원본을 바탕으로 재구성한 로컬 데스크톱 앱."""
import base64
import csv
import difflib
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import rocket_inventory
import rocket_sales
import gross_margin
from product_table import ProductTable

APP_NAME = "쿠팡 관리 프로그램"
VERSION = "0.4.12"


def resource_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def user_data_dir():
    if os.environ.get("COUPANG_MANAGER_PORTABLE") == "1":
        p = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent / "userdata"
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home())) / "CoupangManager"
    else:
        p = Path.home() / ".coupang_manager"
    p.mkdir(parents=True, exist_ok=True)
    return p


DATA_DIR = user_data_dir()
DB_PATH = DATA_DIR / "coupang_manager.db"
STARTER_DB = resource_path("data", "starter.db")
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"coupang_manager_{datetime.now():%Y-%m-%d}.log"
LOG_RETENTION_DAYS = 7
LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 2
DEBUG_DIR = DATA_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_old_logs():
    """Keep useful logs without letting the log folder grow forever."""
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    for path in LOG_DIR.glob("coupang_manager_*.log*"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except Exception:
            pass


def setup_logger():
    logger = logging.getLogger("CoupangManager")
    if logger.handlers:
        return logger
    cleanup_old_logs()
    logger.setLevel(logging.DEBUG)
    # Date-separated log, 2 MB per segment, current + two backups (about 6 MB/day max).
    handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s - %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOGGER = setup_logger()


def ensure_database():
    if not DB_PATH.exists():
        if not STARTER_DB.exists():
            raise FileNotFoundError(f"초기 DB를 찾을 수 없습니다: {STARTER_DB}")
        shutil.copy2(STARTER_DB, DB_PATH)


def money(v):
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return "0"


def num(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def excelish_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return date.today()


def protect_secret(value):
    """Protect API secrets with Windows DPAPI. Falls back to base64 outside Windows."""
    value = value or ""
    if not value:
        return ""
    if os.name != "nt":
        return "plain64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        raw = value.encode("utf-8")
        raw_buf = ctypes.create_string_buffer(raw)
        in_blob = DATA_BLOB(len(raw), ctypes.cast(raw_buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out_blob)
        )
        if not ok:
            raise ctypes.WinError()
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    except Exception:
        return "plain64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")


def unprotect_secret(value):
    value = value or ""
    if not value:
        return ""
    if value.startswith("plain64:"):
        try:
            return base64.b64decode(value.split(":", 1)[1]).decode("utf-8")
        except Exception:
            return ""
    if not value.startswith("dpapi:"):
        return value
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        encrypted = base64.b64decode(value.split(":", 1)[1])
        enc_buf = ctypes.create_string_buffer(encrypted)
        in_blob = DATA_BLOB(len(encrypted), ctypes.cast(enc_buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out_blob)
        )
        if not ok:
            raise ctypes.WinError()
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return raw.decode("utf-8")
    except Exception:
        return ""


class CoupangAPIError(RuntimeError):
    def __init__(self, message, stop_sync=False):
        super().__init__(message)
        self.stop_sync = stop_sync


class CoupangAPICancelled(CoupangAPIError):
    def __init__(self):
        super().__init__('사용자가 API 작업을 취소했습니다.', stop_sync=True)


class CoupangAPI:
    HOST = "https://api-gateway.coupang.com"
    PRODUCT_LIST_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    REVENUE_PATH = "/v2/providers/openapi/apis/api/v1/revenue-history"
    ITEM_INVENTORY_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendorItemId}/inventories"
    _rg_rate_lock = threading.Lock()
    _rg_next_at = 0.0

    def __init__(self, vendor_id, access_key, secret_key):
        self.vendor_id = (vendor_id or "").strip()
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.cancel_event = None
        self.progress = None
        self.phase = ''
        if not self.vendor_id or not self.access_key or not self.secret_key:
            raise CoupangAPIError("Vendor ID, Access Key, Secret Key를 모두 입력하세요.")

    def _authorization(self, method, path, query):
        signed_date = datetime.utcnow().strftime("%y%m%dT%H%M%SZ")
        message = signed_date + method.upper() + path + query
        signature = hmac.new(
            self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return (
            "CEA algorithm=HmacSHA256, access-key=" + self.access_key
            + ", signed-date=" + signed_date + ", signature=" + signature
        )

    def check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CoupangAPICancelled()

    def wait(self, seconds):
        if self.cancel_event is None:
            time.sleep(seconds)
        elif self.cancel_event.wait(seconds):
            raise CoupangAPICancelled()
        self.check_cancelled()

    def _request(self, method, path, params=None, timeout=15):
        timeout = min(timeout, 15)
        params = params or []
        if isinstance(params, dict):
            params = list(params.items())
        query = urllib.parse.urlencode(params, doseq=True)
        url = self.HOST + path + (("?" + query) if query else "")
        LOGGER.debug("API request %s %s%s", method.upper(), path, (("?" + query) if query else ""))
        last_error = None
        for attempt in range(2):
            self.check_cancelled()
            if '/rg/inventory/' in path or path.endswith('/rg/orders'):
                # Include retry attempts and subsequent refreshes in the 50/min limit.
                with CoupangAPI._rg_rate_lock:
                    self.wait(max(0, CoupangAPI._rg_next_at - time.monotonic()))
                    CoupangAPI._rg_next_at = time.monotonic() + 1.3
            auth = self._authorization(method, path, query)
            req = urllib.request.Request(url, method=method.upper())
            req.add_header("Content-Type", "application/json;charset=UTF-8")
            req.add_header("Authorization", auth)
            if self.progress:
                self.progress(f'{self.phase} · 응답 대기 {attempt+1}/2 (네트워크 대기 제한 {timeout}초)')
            self.check_cancelled()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    self.check_cancelled()
                    return json.loads(body) if body else {}
            except CoupangAPICancelled:
                raise
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                self.check_cancelled()
                hint = {401:'API 키와 PC 시간을 확인하세요.', 403:'API 권한과 WING의 허용 IP를 확인하세요.',
                        429:'호출 제한에 걸렸습니다. 잠시 후 다시 실행하세요.'}.get(e.code, '')
                last_error = CoupangAPIError(f"HTTP {e.code}: {hint} {body or e.reason}",
                                            stop_sync=e.code in (401,403,429,500,502,503,504))
                LOGGER.error("API HTTP error %s %s: %s", method.upper(), path, last_error)
                # 쿠팡 상품 상세조회는 간헐적으로 HTTP 400 + DEFAULT/generic message를
                # 반환한 뒤 동일 요청이 잠시 후 성공하는 경우가 실제 로그에서 확인됐다.
                # 명백한 파라미터 오류까지 무작정 재시도하지 않고, 이 generic DEFAULT만
                # 일시 오류로 간주하여 재서명 후 backoff 재시도한다.
                transient_400 = (
                    e.code == 400
                    and '"code":"DEFAULT"' in body.replace(' ', '')
                    and '자세한 사항은 로그를 확인해주세요' in body
                )
                retryable = e.code in (429, 500, 502, 503, 504) or transient_400
                if not retryable or attempt >= 1:
                    raise last_error
                delay = (1.8, 4.0, 7.0)[attempt]
                LOGGER.warning("API transient error retry %s/2 after %.1fs path=%s", attempt + 1, delay, path)
                self.wait(delay)
            except (urllib.error.URLError, TimeoutError) as e:
                self.check_cancelled()
                reason = getattr(e, 'reason', e)
                last_error = CoupangAPIError(f"네트워크 응답 지연 또는 연결 오류 ({timeout}초 제한): {reason}", stop_sync=True)
                LOGGER.error("API network error %s %s: %s", method.upper(), path, last_error)
                if attempt >= 1:
                    raise last_error
                self.wait(1.0 * (attempt + 1))
            except Exception as e:
                LOGGER.exception("API unexpected error %s %s", method.upper(), path)
                raise CoupangAPIError(str(e)) from e
        raise last_error or CoupangAPIError("API 요청에 실패했습니다.")

    def test_connection(self):
        return self._request("GET", self.PRODUCT_LIST_PATH, [
            ("vendorId", self.vendor_id), ("nextToken", 1), ("maxPerPage", 1)
        ])

    def get_all_products(self, progress=None, business_types=None, label="상품"):
        all_rows = []
        next_token = 1
        seen = set()
        page = 0
        while True:
            if next_token in seen:
                LOGGER.warning("%s 목록 nextToken 반복 감지: %s", label, next_token)
                break
            seen.add(next_token)
            params = [("vendorId", self.vendor_id), ("nextToken", next_token), ("maxPerPage", 100)]
            if business_types:
                for bt in business_types:
                    params.append(("businessTypes", bt))
            result = self._request("GET", self.PRODUCT_LIST_PATH, params)
            if str(result.get("code", "")).upper() not in ("SUCCESS", "200", ""):
                raise CoupangAPIError(result.get("message") or f"{label} 목록 조회 실패")
            rows = result.get("data") or []
            all_rows.extend(rows)
            page += 1
            if progress:
                progress(f"{label} 목록 {page}페이지 / {len(all_rows)}건 확인")
            token = result.get("nextToken")
            if token in (None, "", 0, "0"):
                break
            try:
                next_token = int(token)
            except Exception:
                next_token = token
        LOGGER.info("%s 목록 조회 완료: %s건", label, len(all_rows))
        return all_rows

    def get_product(self, seller_product_id):
        result = self._request(
            "GET", self.PRODUCT_LIST_PATH + "/" + str(seller_product_id)
        )
        if str(result.get("code", "")).upper() not in ("SUCCESS", "200", ""):
            raise CoupangAPIError(result.get("message") or f"상품 {seller_product_id} 상세조회 실패")
        return result.get("data") or {}

    def get_item_inventory(self, vendor_item_id):
        """Return the option-level quantity/price/sales-status payload."""
        vid = str(vendor_item_id or '').strip()
        if not vid:
            raise CoupangAPIError('옵션 ID(vendorItemId)가 없습니다.')
        path = self.ITEM_INVENTORY_PATH.format(vendorItemId=urllib.parse.quote(vid, safe=''))
        result = self._request('GET', path)
        if str(result.get('code', '')).upper() not in ('SUCCESS', '200', ''):
            raise CoupangAPIError(result.get('message') or f'옵션 {vid} 판매상태 조회 실패')
        data = result.get('data') or {}
        if not isinstance(data, dict):
            raise CoupangAPIError(f'옵션 {vid} 판매상태 응답 형식이 올바르지 않습니다.')
        return data

    def get_revenue_exposure_map(self, vendor_item_ids, lookback_days=365, progress=None):
        """Fallback resolver for vendorItemId -> productId from revenue history.

        Product detail (/seller-products/{sellerProductId}) remains the primary
        source. This method is called only for option rows whose exposed product ID
        could not be resolved from product detail or trusted existing data.
        Newest sale/payment date wins (recognition date is used only as a fallback).
        """
        targets={str(x or '').strip() for x in vendor_item_ids}
        targets.discard('')
        if not targets:
            return {'latest':{}, 'history':{}, 'daysScanned':0, 'requests':0}

        today=date.today()
        end=today-timedelta(days=1)  # API permits through yesterday only.
        floor=end-timedelta(days=max(1, int(lookback_days))-1)
        unresolved=set(targets)
        history={}  # vendorItemId -> productId -> most recent recognitionDate
        request_count=0
        scanned_days=0

        while end >= floor and unresolved:
            start=max(floor, end-timedelta(days=30))  # max 31 inclusive days
            token=''
            chunk_seen=set()
            page=0
            while True:
                params=[
                    ('vendorId', self.vendor_id),
                    ('recognitionDateFrom', start.isoformat()),
                    ('recognitionDateTo', end.isoformat()),
                    ('token', token),
                    ('maxPerPage', 50),
                ]
                result=self._request('GET', self.REVENUE_PATH, params, timeout=45)
                request_count += 1
                page += 1
                code=str(result.get('code',''))
                if code not in ('200','SUCCESS',''):
                    raise CoupangAPIError(result.get('message') or '매출내역 조회 실패')
                for sale in result.get('data') or []:
                    # productId reflects the ordered item; use payment/sale date for
                    # recency so a later refund recognition does not make an old mapping look current.
                    seen_date=str(sale.get('saleDate') or sale.get('recognitionDate') or '')
                    for item in sale.get('items') or []:
                        vid=str(item.get('vendorItemId') or '').strip()
                        pid=str(item.get('productId') or '').strip()
                        if vid not in unresolved or not pid or pid == '0':
                            continue
                        bucket=history.setdefault(vid,{})
                        if seen_date >= bucket.get(pid,''):
                            bucket[pid]=seen_date
                        chunk_seen.add(vid)
                if progress:
                    found_count=len((targets-unresolved) | chunk_seen)
                    progress(f'노출ID 확인 {start:%Y-%m-%d}~{end:%Y-%m-%d} {page}페이지 / 발견 {found_count}/{len(targets)}')
                has_next=bool(result.get('hasNext'))
                next_token=str(result.get('nextToken') or '')
                if not has_next or not next_token:
                    break
                token=next_token
            # We scan newest to oldest. Once an option appears in this entire
            # date chunk, older chunks cannot provide a more current mapping.
            unresolved -= chunk_seen
            scanned_days += (end-start).days + 1
            end=start-timedelta(days=1)

        latest={}
        for vid, by_pid in history.items():
            if not by_pid:
                continue
            # Latest date wins. If multiple IDs share the same newest date, keep
            # deterministic ordering while preserving every ID in history.
            pid, seen=max(by_pid.items(), key=lambda kv:(kv[1], kv[0]))
            latest[vid]={'productId':pid, 'seenDate':seen}
        return {
            'latest':latest,
            'history':history,
            'daysScanned':scanned_days,
            'requests':request_count,
            'unresolved':sorted(unresolved),
        }

    def _debug_find_list_page(self, seller_product_id, business_types=None):
        """Find only the list page containing one sellerProductId and preserve the API response."""
        target = str(seller_product_id).strip()
        next_token = 1
        seen = set()
        page = 0
        last_result = {}
        while True:
            if next_token in seen:
                break
            seen.add(next_token)
            params = [("vendorId", self.vendor_id), ("nextToken", next_token), ("maxPerPage", 100)]
            if business_types:
                for bt in business_types:
                    params.append(("businessTypes", bt))
            result = self._request("GET", self.PRODUCT_LIST_PATH, params)
            last_result = result
            page += 1
            rows = result.get("data") or []
            matches = [r for r in rows if str(r.get("sellerProductId") or "") == target]
            if matches:
                return {
                    "found": True,
                    "pagesScanned": page,
                    "nextTokenUsed": next_token,
                    "matchedData": matches,
                    "rawResponse": result,
                }
            token = result.get("nextToken")
            if token in (None, "", 0, "0"):
                break
            try:
                next_token = int(token)
            except Exception:
                next_token = token
        return {
            "found": False,
            "pagesScanned": page,
            "nextTokenUsed": next_token,
            "matchedData": [],
            "rawResponse": last_result,
        }

    def debug_product_responses(self, seller_product_id):
        """Read-only diagnostic. Never writes to the local DB."""
        target = str(seller_product_id).strip()
        result = {
            "sellerProductId": target,
            "capturedAt": datetime.now().isoformat(timespec="seconds"),
        }
        for key, business_types in (("marketplaceList", None), ("rocketGrowthList", ["rocketGrowth"])):
            try:
                result[key] = self._debug_find_list_page(target, business_types)
            except Exception as e:
                LOGGER.exception("단건 디버그 목록 조회 실패 sellerProductId=%s type=%s", target, key)
                result[key] = {"found": False, "error": str(e), "matchedData": [], "rawResponse": {}}
        try:
            result["detailRawResponse"] = self._request(
                "GET", self.PRODUCT_LIST_PATH + "/" + target
            )
        except Exception as e:
            LOGGER.exception("단건 디버그 상세 조회 실패 sellerProductId=%s", target)
            result["detailRawResponse"] = {"_debugError": str(e)}

        # Also resolve the same options through settlement revenue history. This
        # is read-only and shows the actual option-level productId/vendorItemId
        # pairs that the product APIs cannot represent for split hybrid products.
        try:
            detail_raw=result.get('detailRawResponse') or {}
            detail=detail_raw.get('data') if isinstance(detail_raw,dict) and isinstance(detail_raw.get('data'),dict) else {}
            vids=set()
            for item in detail.get('items') or []:
                if not isinstance(item,dict):
                    continue
                for src in (item, item.get('marketPlaceItemData'), item.get('marketplaceItemData'), item.get('rocketGrowthItemData')):
                    if isinstance(src,dict) and src.get('vendorItemId'):
                        vids.add(str(src.get('vendorItemId')))
            if vids:
                result['revenueExposureMap']=self.get_revenue_exposure_map(vids, lookback_days=186)
            else:
                result['revenueExposureMap']={'latest':{},'history':{},'unresolved':[]}
        except Exception as e:
            LOGGER.exception('단건 디버그 매출 노출ID 조회 실패 sellerProductId=%s', target)
            result['revenueExposureMap']={'_debugError':str(e),'latest':{},'history':{}}
        return result


class Database:
    def __init__(self, path, ensure_schema=True):
        self.path = path
        # Multiple API jobs use their own SQLite connection.  Wait for a writer
        # instead of failing immediately with "database is locked".
        self.conn = sqlite3.connect(path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # WAL lets the UI keep reading while an API worker is writing.  Changing
        # journal mode can itself require a short exclusive lock, so only the
        # primary connection attempts it and a failure is non-fatal.
        if ensure_schema:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError as e:
                LOGGER.warning("SQLite WAL 설정 보류: %s", e)
            self._ensure_schema()

    def _ensure_schema(self):
        """Add columns/tables introduced by newer app versions without resetting user data."""
        rocket_inventory.ensure_schema(self.conn)
        rocket_sales.ensure_schema(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(products)").fetchall()}
        if "seller_product_item_id" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN seller_product_item_id TEXT")
        if "item_id" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN item_id TEXT")
        if "fulfillment_type" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN fulfillment_type TEXT DEFAULT '판매자배송'")
            # v0.3.1 and earlier used status='로켓그로스' to identify the fulfillment channel.
            # Preserve that legacy management status and derive a separate fulfillment field.
            self.conn.execute("UPDATE products SET fulfillment_type='로켓그로스' WHERE status='로켓그로스'")
            self.conn.execute("UPDATE products SET fulfillment_type='판매자배송' WHERE COALESCE(fulfillment_type,'')='' ")
        if "sale_status" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN sale_status TEXT DEFAULT '미확인'")
            self.conn.execute("UPDATE products SET sale_status='미확인' WHERE COALESCE(sale_status,'')=''")
        if "api_stock_quantity" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN api_stock_quantity INTEGER")
        if "sale_status_checked_at" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN sale_status_checked_at TEXT")
        if "sale_status_error" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN sale_status_error TEXT")
        if "sale_status_error_at" not in cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN sale_status_error_at TEXT")
        # v0.3.3~0.3.7 incorrectly promoted a transient inventory API 400
        # ("유효한 옵션이 없습니다") into a persistent sale status.  It is an
        # API-query failure, not a sale status.  Recover those rows to 미확인 and
        # keep an error flag until the next successful inventory lookup.
        self.conn.execute("""UPDATE products
                           SET sale_status='미확인',
                               sale_status_error=CASE WHEN COALESCE(sale_status_error,'')=''
                                                     THEN '이전 버전의 유효하지않음 판정 - 재조회 필요'
                                                     ELSE sale_status_error END,
                               sale_status_error_at=COALESCE(sale_status_error_at,CURRENT_TIMESTAMP)
                           WHERE sale_status='유효하지않음'""")
        # 묶음/묶임은 아이템위너 추정용으로 쓰던 과거 관리값이다.
        self.conn.execute("UPDATE products SET status='' WHERE status IN ('묶음','묶임')")
        # Canonical product link: exposed product ID + vendorItemId only.
        # Rebuild legacy links that contained itemId or other query parameters.
        self.conn.execute("""UPDATE products
                           SET link='https://www.coupang.com/vp/products/' || TRIM(exposed_product_id)
                                    || '?vendorItemId=' || TRIM(option_id)
                           WHERE COALESCE(TRIM(exposed_product_id),'')<>''
                             AND COALESCE(TRIM(option_id),'')<>''""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_option_id ON products(option_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_fulfillment ON products(fulfillment_type)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_item_id ON products(item_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_seller_item_id ON products(seller_product_item_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_registered_id ON products(registered_product_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_exposed_id ON products(exposed_product_id)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS product_exposure_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            option_key TEXT NOT NULL,
            option_id TEXT,
            seller_product_item_id TEXT,
            registered_product_id TEXT,
            exposed_product_id TEXT NOT NULL,
            model TEXT,
            option_name TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT DEFAULT 'existing',
            UNIQUE(option_key, exposed_product_id)
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_exposure_history_key ON product_exposure_history(option_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_exposure_history_pid ON product_exposure_history(exposed_product_id)")
        # Lightweight change log for dashboard notifications. Rows are created only
        # when a currently stored value actually changes, not on every sync.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS product_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            product_row_id INTEGER,
            option_key TEXT,
            option_id TEXT,
            registered_product_id TEXT,
            model TEXT,
            option_name TEXT,
            old_value TEXT,
            new_value TEXT,
            source TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            acknowledged_at TEXT
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_product_events_type_ack ON product_events(event_type,acknowledged_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_product_events_option_key ON product_events(option_key)")

        # v0.3.5 model master / option-to-model mapping. The supplier price list
        # is the master source; Coupang vendorItemId mappings are kept separately.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS model_master(
            model TEXT PRIMARY KEY,
            source_sheet TEXT,
            status_note TEXT,
            category TEXT,
            product_name TEXT,
            color TEXT,
            package_type TEXT,
            pack_qty REAL,
            supply_price REAL,
            coupang_price REAL,
            coupon_price REAL,
            size_mm TEXT,
            kc TEXT,
            source_file TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_model_master_category ON model_master(category)")
        # v0.4.2: model-level Rocket Growth fulfillment profile.
        # 원가는 공급가에서 자동 계산하지 않고 사용자가 직접 관리한다.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS gross_margin_profiles(
            model TEXT PRIMARY KEY,
            size_type TEXT,
            source TEXT DEFAULT 'margin-ref',
            manual_cost REAL,
            cost_source TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        profile_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(gross_margin_profiles)").fetchall()}
        if 'manual_cost' not in profile_cols:
            self.conn.execute("ALTER TABLE gross_margin_profiles ADD COLUMN manual_cost REAL")
        if 'cost_source' not in profile_cols:
            self.conn.execute("ALTER TABLE gross_margin_profiles ADD COLUMN cost_source TEXT")
        # 기존 그로스 마진 계산표에 사용자가 입력해 둔 원가는 최초 1회 수동원가로 보존한다.
        try:
            self.conn.execute("""UPDATE gross_margin_profiles
                               SET manual_cost=(SELECT mr.cost FROM margin_refs mr
                                                WHERE mr.method='로켓그로스'
                                                  AND mr.model=gross_margin_profiles.model
                                                  AND mr.cost IS NOT NULL AND mr.cost>0
                                                ORDER BY mr.id LIMIT 1),
                                   cost_source=COALESCE(cost_source,'기존 마진표')
                               WHERE manual_cost IS NULL
                                 AND EXISTS (SELECT 1 FROM margin_refs mr
                                             WHERE mr.method='로켓그로스'
                                               AND mr.model=gross_margin_profiles.model
                                               AND mr.cost IS NOT NULL AND mr.cost>0)""")
            self.conn.execute("""INSERT INTO gross_margin_profiles(model,manual_cost,cost_source,updated_at)
                               SELECT mr.model,mr.cost,'기존 마진표',CURRENT_TIMESTAMP
                               FROM margin_refs mr
                               WHERE mr.method='로켓그로스' AND mr.cost IS NOT NULL AND mr.cost>0
                                 AND COALESCE(TRIM(mr.model),'')<>''
                               GROUP BY mr.model
                               ON CONFLICT(model) DO NOTHING""")
        except sqlite3.OperationalError:
            pass
        # Seed only reliable size types from the original gross-margin reference.
        try:
            self.conn.execute("""INSERT OR IGNORE INTO gross_margin_profiles(model,size_type,source,updated_at)
                               SELECT model,
                                      CASE REPLACE(TRIM(size_type),' ','')
                                        WHEN '극소형' THEN '극소형'
                                        WHEN '소형' THEN '소형'
                                        WHEN '중형' THEN '중형'
                                        WHEN '대형1' THEN '대형 1'
                                        WHEN '대형2' THEN '대형 2'
                                        WHEN '특대형' THEN '특대형'
                                      END,
                                      'margin-ref',CURRENT_TIMESTAMP
                               FROM margin_refs
                               WHERE method='로켓그로스'
                                 AND REPLACE(TRIM(COALESCE(size_type,'')),' ','')
                                     IN ('극소형','소형','중형','대형1','대형2','특대형')""")
        except sqlite3.OperationalError:
            pass
        self.conn.execute("""CREATE TABLE IF NOT EXISTS option_model_map(
            option_id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            source TEXT DEFAULT 'products',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_option_model_map_model ON option_model_map(model)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS unmatched_options(
            option_id TEXT PRIMARY KEY,
            option_name TEXT,
            product_name TEXT,
            registered_product_id TEXT,
            method TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS sales_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_date TEXT NOT NULL,
            option_id TEXT NOT NULL,
            option_name TEXT,
            product_name TEXT,
            registered_product_id TEXT,
            method TEXT,
            revenue REAL DEFAULT 0,
            orders REAL DEFAULT 0,
            quantity REAL DEFAULT 0,
            source_file TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sale_date, option_id, method)
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_pending_option_id ON sales_pending(option_id)")

        sales_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sales)").fetchall()}
        if "option_id" not in sales_cols:
            self.conn.execute("ALTER TABLE sales ADD COLUMN option_id TEXT")
        if "option_name" not in sales_cols:
            self.conn.execute("ALTER TABLE sales ADD COLUMN option_name TEXT")
        if "product_name" not in sales_cols:
            self.conn.execute("ALTER TABLE sales ADD COLUMN product_name TEXT")
        if "registered_product_id" not in sales_cols:
            self.conn.execute("ALTER TABLE sales ADD COLUMN registered_product_id TEXT")
        if "source" not in sales_cols:
            self.conn.execute("ALTER TABLE sales ADD COLUMN source TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_option_date ON sales(option_id,sale_date)")

        # Bundle the current Hyundai price-list model master so an existing user DB
        # gets the model dictionary immediately on first v0.3.5 launch. Future
        # supplier files imported by the user remain authoritative because the
        # bundled seed is applied only when model_master is empty.
        if self.conn.execute("SELECT COUNT(*) FROM model_master").fetchone()[0] == 0:
            seed_path = resource_path("data", "model_master_seed.json")
            if seed_path.exists():
                try:
                    with open(seed_path, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    models = payload.get("models") or []
                    self.conn.executemany("""INSERT OR REPLACE INTO model_master(
                        model,source_sheet,status_note,category,product_name,color,package_type,pack_qty,
                        supply_price,coupang_price,coupon_price,size_mm,kc,source_file,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", [
                        (str(x.get('model') or '').strip(),x.get('source_sheet') or '',x.get('status_note') or '',
                         x.get('category') or '',x.get('product_name') or '',x.get('color') or '',x.get('package_type') or '',
                         x.get('pack_qty'),x.get('supply_price'),x.get('coupang_price'),x.get('coupon_price'),
                         x.get('size_mm') or '',x.get('kc') or '',x.get('source_file') or payload.get('source') or '')
                        for x in models if str(x.get('model') or '').strip()
                    ])
                except Exception:
                    LOGGER.exception("모델 마스터 기본 데이터 로드 실패")

        # Build the stable vendorItemId -> model mapping from the user's existing
        # product table. Manual mappings are never overwritten by this migration.
        map_rows = self.conn.execute("""SELECT option_id,model FROM products
                                        WHERE COALESCE(TRIM(option_id),'')<>''
                                          AND COALESCE(TRIM(model),'')<>''
                                        ORDER BY id""").fetchall()
        seen_option_ids=set()
        for mr in map_rows:
            oid=str(mr['option_id'] or '').strip(); model=str(mr['model'] or '').strip()
            if not oid or not model or oid in seen_option_ids:
                continue
            seen_option_ids.add(oid)
            current=self.conn.execute("SELECT source FROM option_model_map WHERE option_id=?",(oid,)).fetchone()
            if not current:
                self.conn.execute("INSERT INTO option_model_map(option_id,model,source,updated_at) VALUES (?,?,'products',CURRENT_TIMESTAMP)",(oid,model))
            elif str(current['source'] or '')!='manual':
                self.conn.execute("UPDATE option_model_map SET model=?,source='products',updated_at=CURRENT_TIMESTAMP WHERE option_id=?",(model,oid))
        rows = self.conn.execute("""SELECT id,option_id,seller_product_item_id,registered_product_id,
                                           exposed_product_id,model,option_name,note
                                    FROM products WHERE COALESCE(exposed_product_id,'')<>''""").fetchall()
        for r in rows:
            oid = str(r['option_id'] or '').strip()
            sid = str(r['seller_product_item_id'] or '').strip()
            key = ('v:' + oid) if oid else (('s:' + sid) if sid else ('r:' + str(r['id'])))
            # Rows created automatically by older API versions may contain a
            # seller-product-level productId. Do not promote those to a trusted
            # manual/existing mapping during migration.
            source = 'api-list' if str(r['note'] or '').startswith('쿠팡 API') else 'existing'
            self.conn.execute("""INSERT OR IGNORE INTO product_exposure_history(
                option_key,option_id,seller_product_item_id,registered_product_id,exposed_product_id,model,option_name,source
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (key,oid,sid,str(r['registered_product_id'] or ''),str(r['exposed_product_id'] or ''),str(r['model'] or ''),str(r['option_name'] or ''),source))
        # v0.2.9 could blank exposed_product_id when Coupang detail API returned productId="".
        # Recover the latest value already recorded in history, without overwriting non-empty IDs.
        blank_rows = self.conn.execute("""SELECT id,option_id,seller_product_item_id FROM products
                                         WHERE COALESCE(exposed_product_id,'')=''""").fetchall()
        for r in blank_rows:
            oid = str(r['option_id'] or '').strip()
            sid = str(r['seller_product_item_id'] or '').strip()
            key = ('v:' + oid) if oid else (('s:' + sid) if sid else ('r:' + str(r['id'])))
            h = self.conn.execute("""SELECT exposed_product_id FROM product_exposure_history
                                     WHERE option_key=? AND COALESCE(exposed_product_id,'')<>''
                                     ORDER BY CASE
                                         WHEN source='manual' THEN 0
                                         WHEN source LIKE 'revenue%' THEN 1
                                         WHEN source IN ('existing','sibling','excel-import','api-item','api-single') THEN 2
                                         WHEN source='api-list' THEN 9 ELSE 5 END,
                                         last_seen DESC, id DESC LIMIT 1""", (key,)).fetchone()
            if h and h['exposed_product_id']:
                self.conn.execute("UPDATE products SET exposed_product_id=? WHERE id=? AND COALESCE(exposed_product_id,'')=''",
                                  (str(h['exposed_product_id']), r['id']))

        # v0.2.10/11 may have written a seller-product-level list productId into
        # every option. If that exact current value is recorded only as api-list
        # and a trusted per-option value already exists in history, restore it on
        # startup before the user even runs a new sync.
        current_rows = self.conn.execute("""SELECT id,option_id,seller_product_item_id,item_id,exposed_product_id,link
                                            FROM products WHERE COALESCE(exposed_product_id,'')<>''""").fetchall()
        for r in current_rows:
            oid=str(r['option_id'] or '').strip(); sid=str(r['seller_product_item_id'] or '').strip()
            key=('v:'+oid) if oid else (('s:'+sid) if sid else ('r:'+str(r['id'])))
            current_pid=str(r['exposed_product_id'] or '').strip()
            curhist=self.conn.execute("""SELECT source FROM product_exposure_history
                                         WHERE option_key=? AND exposed_product_id=? LIMIT 1""",
                                      (key,current_pid)).fetchone()
            if not curhist or str(curhist['source'] or '') != 'api-list':
                continue
            trusted=self.conn.execute("""SELECT exposed_product_id FROM product_exposure_history
                                         WHERE option_key=? AND COALESCE(exposed_product_id,'')<>''
                                           AND source<>'api-list'
                                         ORDER BY CASE WHEN source='manual' THEN 0
                                                       WHEN source LIKE 'revenue%' THEN 1
                                                       WHEN source IN ('existing','sibling','excel-import','api-item','api-single') THEN 2
                                                       ELSE 5 END,
                                                  last_seen DESC,id DESC LIMIT 1""", (key,)).fetchone()
            if not trusted or not trusted['exposed_product_id']:
                continue
            new_pid=str(trusted['exposed_product_id'])
            old_link=str(r['link'] or '')
            new_link=old_link
            if not old_link or 'coupang.com/vp/products/' in old_link:
                new_link=f'https://www.coupang.com/vp/products/{new_pid}'
                if oid:
                    new_link += '?' + urllib.parse.urlencode({'vendorItemId':oid})
            self.conn.execute("UPDATE products SET exposed_product_id=?,link=? WHERE id=?",
                              (new_pid,new_link,r['id']))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def backup_to(self, target):
        """Create a consistent SQLite backup (safe even when WAL mode is enabled)."""
        self.conn.commit()
        dest = sqlite3.connect(str(target), timeout=30.0)
        try:
            self.conn.backup(dest)
            dest.commit()
        finally:
            dest.close()

    def execute(self, sql, params=()):
        # sqlite busy_timeout handles normal contention.  The small retry here
        # also covers the narrow race where Windows/antivirus delays releasing
        # the DB file after another connection commits.
        last = None
        for attempt in range(4):
            try:
                cur = self.conn.execute(sql, params)
                self.conn.commit()
                return cur
            except sqlite3.OperationalError as e:
                last = e
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                time.sleep(0.15 * (attempt + 1))
        raise last

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def setting(self, key, default=""):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r[0] if r else default

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, str(value)))

    def distinct_models(self):
        rows = self.query("""
            SELECT model FROM products
            UNION SELECT model FROM sales
            UNION SELECT model FROM inbounds
            UNION SELECT model FROM model_master
            ORDER BY model COLLATE NOCASE
        """)
        return [r[0] for r in rows if r[0]]


class FieldDialog(tk.Toplevel):
    def __init__(self, parent, title, fields, values=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.resizable(True, False)
        self.result = None
        self.vars = {}
        values = values or {}
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        for i, f in enumerate(fields):
            key, label = f[0], f[1]
            kind = f[2] if len(f) > 2 else "entry"
            opts = f[3] if len(f) > 3 else None
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="w", padx=(0, 12), pady=5)
            var = tk.StringVar(value="" if values.get(key) is None else str(values.get(key, "")))
            self.vars[key] = var
            if kind == "combo":
                w = ttk.Combobox(body, textvariable=var, values=opts or [], state="readonly" if opts else "normal", width=42)
            else:
                w = ttk.Entry(body, textvariable=var, width=46)
            w.grid(row=i, column=1, sticky="ew", pady=5)
        body.columnconfigure(1, weight=1)
        btn = ttk.Frame(body)
        btn.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btn, text="취소", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btn, text="저장", command=self.save).pack(side="right")
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(50, self._center)

    def _center(self):
        self.update_idletasks()
        p = self.master
        x = p.winfo_rootx() + max(20, (p.winfo_width() - self.winfo_width()) // 2)
        y = p.winfo_rooty() + max(20, (p.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")

    def save(self):
        self.result = {k: v.get().strip() for k, v in self.vars.items()}
        self.destroy()


class ExcelImportDialog(tk.Toplevel):
    """Choose which known sheets to import from the user's Coupang workbook."""
    def __init__(self, parent, available, default_year):
        super().__init__(parent)
        self.title("Excel 가져오기")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.result = None
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="가져올 데이터 선택", font=("맑은 고딕", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text="현재 쿠팡 관리.xlsm 구조를 기준으로 옵션 ID 중심으로 병합합니다.", foreground="#666").pack(anchor="w", pady=(3, 12))
        self.vars = {}
        labels = [("등록관리", "상품 관리"), ("그로스 입고 관리", "그로스 입고 이력"), ("일일 매출", "일일 매출") ]
        for sheet, label in labels:
            v = tk.BooleanVar(value=sheet in available)
            self.vars[sheet] = v
            cb = ttk.Checkbutton(body, text=f"{label}  ({sheet})", variable=v)
            if sheet not in available:
                cb.state(["disabled"])
            cb.pack(anchor="w", pady=3)
        yr = ttk.Frame(body); yr.pack(fill="x", pady=(12, 0))
        ttk.Label(yr, text="입고 날짜 연도").pack(side="left")
        self.year_var = tk.StringVar(value=str(default_year))
        ttk.Entry(yr, textvariable=self.year_var, width=8).pack(side="left", padx=8)
        ttk.Label(yr, text="예: '5월 6일' 형식의 입고 날짜에만 사용", foreground="#666").pack(side="left")
        ttk.Separator(body).pack(fill="x", pady=14)
        ttk.Label(body, text="가져오기 직전에 DB 백업을 자동으로 생성합니다.", foreground="#176b3a").pack(anchor="w")
        btn = ttk.Frame(body); btn.pack(fill="x", pady=(14, 0))
        ttk.Button(btn, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(btn, text="가져오기", style="Accent.TButton", command=self.save).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(50, self._center)

    def _center(self):
        self.update_idletasks()
        p = self.master
        x = p.winfo_rootx() + max(20, (p.winfo_width() - self.winfo_width()) // 2)
        y = p.winfo_rooty() + max(20, (p.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")

    def save(self):
        try:
            year = int(self.year_var.get().strip())
            if year < 2000 or year > 2100:
                raise ValueError
        except Exception:
            return messagebox.showwarning(APP_NAME, "입고 날짜 연도를 4자리 숫자로 입력하세요.", parent=self)
        selected = [k for k, v in self.vars.items() if v.get()]
        if not selected:
            return messagebox.showwarning(APP_NAME, "가져올 항목을 하나 이상 선택하세요.", parent=self)
        self.result = {"sheets": selected, "year": year}
        self.destroy()


class CoupangManager(tk.Tk):
    def __init__(self):
        super().__init__()
        ensure_database()
        self.db = Database(DB_PATH)
        self.api_queue = queue.Queue()
        self.api_busy = False
        self.api_cancel_event = threading.Event()
        self.api_started = 0
        self.api_last_status = 'API 갱신을 실행하면 진행 상황이 여기에 표시됩니다.'
        self.api_status_var = tk.StringVar(value=self.api_last_status)
        self.title(f"{APP_NAME}  v{VERSION}")
        self.geometry("1500x900")
        self.minsize(1180, 720)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._style()
        self._build_shell()
        self.show_page("product_dashboard")
        self._search_dialog = None
        self.bind('<F5>', self.open_search_dialog)
        self.after(200, self._poll_api_queue)
        # v0.3.7: do not hold a write lock during application construction.
        # Run model-link repair after the window is ready and keep it non-fatal.
        self.after(700, self._startup_model_repair)

    def _startup_model_repair(self):
        try:
            repair = self._auto_repair_model_links(self.db)
            if repair.get('linked') or repair.get('pendingLinked') or repair.get('salesMoved'):
                LOGGER.info("모델 자동연결 복구 linked=%s pendingLinked=%s salesMoved=%s",
                            repair.get('linked',0), repair.get('pendingLinked',0), repair.get('salesMoved',0))
                # Refresh current page lazily; the user does not need to wait for
                # this repair before the main window opens.
                try:
                    if getattr(self, 'current_page', '') == 'master':
                        self.refresh_model_master()
                except Exception:
                    pass
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() or 'busy' in str(e).lower():
                LOGGER.warning("시작 모델 자동연결 DB 사용 중 - 이번 실행에서는 건너뜀: %s", e)
                self._set_api_status('DB가 사용 중이어서 모델 자동연결을 잠시 건너뛰었습니다.')
                return
            LOGGER.exception("시작 시 모델 자동연결 복구 실패")
        except Exception:
            LOGGER.exception("시작 시 모델 자동연결 복구 실패")

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=33, font=("맑은 고딕", 10))
        # Light row selection + orange cell outline makes the clicked cell obvious.
        style.map("Treeview", background=[("selected", "#d9ecff")], foreground=[("selected", "#000000")])
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        style.configure("Title.TLabel", font=("맑은 고딕", 20, "bold"))
        style.configure("Sub.TLabel", font=("맑은 고딕", 10))
        style.configure("CardValue.TLabel", font=("맑은 고딕", 22, "bold"))
        style.configure("Nav.TButton", font=("맑은 고딕", 10), padding=(14, 10), anchor="w")
        style.configure("Accent.TButton", font=("맑은 고딕", 9, "bold"), padding=(12, 7))
        style.configure("Toolbar.TButton", font=("맑은 고딕", 10), padding=(10, 7))
        style.configure("Danger.TButton", font=("맑은 고딕", 9, "bold"), padding=(9, 6))
        style.configure("Filter.TLabel", font=("맑은 고딕", 10, "bold"))

    def _build_shell(self):
        self.sidebar = ttk.Frame(self, padding=(10, 14))
        self.sidebar.pack(side="left", fill="y")
        ttk.Label(self.sidebar, text="쿠팡 관리", font=("맑은 고딕", 18, "bold")).pack(anchor="w", padx=8, pady=(2, 20))
        nav = [
            ("product_dashboard", "상품 현황"), ("products", "상품 관리"),
            ("rocket_inventory", "로켓그로스 재고 현황"),
            ("master", "마스터 데이터"), ("margin", "그로스 마진"),
            ("sales", "일일 매출"), ("data", "데이터 · 설정"),
        ]
        for key, label in nav:
            ttk.Button(self.sidebar, text=label, style="Nav.TButton", width=18,
                       command=lambda k=key: self.show_page(k)).pack(fill="x", pady=2)
        ttk.Separator(self.sidebar).pack(fill="x", pady=16)
        ttk.Label(self.sidebar, text=f"F5 · 검색\nDB: {DB_PATH.name}\nv{VERSION}", foreground="#666").pack(anchor="w", padx=8)

        self.content = ttk.Frame(self, padding=18)
        self.content.pack(side="left", fill="both", expand=True)
        self.pages = {}

    def clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def show_page(self, name):
        # Legacy links open the corresponding section inside the unified page.
        master_section = {'models': 'models', 'fees': 'fees'}.get(name)
        if master_section:
            name = 'master'
        elif name in ('dashboard', 'stock'):
            name = 'product_dashboard'
        self.clear_content()
        self.current_page = name
        getattr(self, f"build_{name}")()
        if master_section:
            self.master_tabs.select(self.master_models_tab if master_section == 'models' else self.master_fees_tab)

    def _search_target(self, page):
        """Use each page's existing local filter, without rebuilding the page."""
        if page == 'rocket_inventory':
            inventory = self.rocket_inventory_page
            return inventory.search, inventory.apply_filters
        targets = {
            'products': ('prod_q', self._reset_product_page_and_refresh),
            'master': ('model_q', self.refresh_model_master),
            'margin': ('gross_margin_search', self.refresh_gross_margin),
            'sales': ('sales_q', self.refresh_sales),
            'search': ('search_q', self.run_search),
        }
        variable, refresh = targets[page]
        return getattr(self, variable), refresh

    def open_search_dialog(self, event=None):
        dialog = self._search_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.search_entry.focus_set()
            dialog.search_entry.selection_range(0, 'end')
            return 'break'
        # Leave an already open edit/settings dialog in charge of its inputs.
        if self.grab_current() is not None:
            return 'break'

        titles = {'products': '상품 관리', 'rocket_inventory': '로켓그로스 재고 현황',
                  'master': '마스터 데이터 · 모델', 'margin': '그로스 마진',
                  'sales': '일일 매출', 'search': '상품 검색'}
        page = self.current_page if self.current_page in titles else 'products'
        fallback = page != self.current_page
        initial = '' if fallback else self._search_target(page)[0].get()
        previous_focus = self.focus_get()
        dialog = tk.Toplevel(self)
        self._search_dialog = dialog
        dialog.title('검색 · ' + titles[page])
        dialog.transient(self)
        dialog.resizable(False, False)
        box = ttk.Frame(dialog, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text=titles[page] + ' 검색', font=('맑은 고딕', 12, 'bold')).pack(anchor='w')
        hint = ('모델·옵션 매핑 탭에서 모델명·품명·분류를 검색합니다.' if page == 'master' else
                '검색하면 상품 관리로 이동합니다.' if fallback else '현재 화면의 필터를 유지하여 검색합니다.')
        ttk.Label(box, text=hint, foreground='#666').pack(anchor='w', pady=(5, 12))
        dialog.search_var = tk.StringVar(dialog, value=initial)
        entry = ttk.Entry(box, textvariable=dialog.search_var, width=42, font=('맑은 고딕', 11))
        dialog.search_entry = entry
        entry.pack(fill='x', ipady=5)
        ttk.Label(box, text='Enter 검색 · Esc 닫기 · 빈칸으로 검색하면 검색어 해제',
                  foreground='#666').pack(anchor='w', pady=(8, 14))

        def close(event=None):
            self._search_dialog = None
            dialog.grab_release()
            dialog.destroy()
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()
            return 'break'

        def apply(event=None):
            query = dialog.search_var.get().strip()
            close()
            if self.current_page != page:
                self.show_page(page)
            if page == 'margin':
                self.gross_margin_notebook.select(0)
            elif page == 'master':
                self.master_tabs.select(self.master_models_tab)
            variable, refresh = self._search_target(page)
            variable.set(query)
            refresh()
            return 'break'

        actions = ttk.Frame(box)
        actions.pack(fill='x')
        ttk.Button(actions, text='검색 (Enter)', command=apply).pack(side='right')
        ttk.Button(actions, text='닫기 (Esc)', command=close).pack(side='right', padx=6)
        dialog.protocol('WM_DELETE_WINDOW', close)
        dialog.bind('<Return>', apply)
        dialog.bind('<Escape>', close)
        dialog.bind('<F5>', self.open_search_dialog)
        dialog.update_idletasks()
        x = max(0, self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f'+{x}+{y}')
        dialog.grab_set()
        entry.focus_set()
        entry.selection_range(0, 'end')
        return 'break'

    def page_header(self, title, subtitle=""):
        top = ttk.Frame(self.content)
        top.pack(fill="x", pady=(0, 14))
        ttk.Label(top, text=title, style="Title.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(top, text=subtitle, style="Sub.TLabel", foreground="#666").pack(anchor="w", pady=(2, 0))
        if self.multitap_only_enabled():
            ttk.Label(top, text="멀티탭 전용 표시 중 · 다른 상품은 삭제되지 않고 숨겨져 있습니다.", foreground="#176b3a").pack(anchor="w", pady=(4, 0))
        return top

    def make_tree(self, parent, columns, widths=None, height=18):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        tree = ttk.Treeview(holder, columns=[c[0] for c in columns], show="headings", height=height, selectmode="extended")
        vs = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        hs = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1); holder.columnconfigure(0, weight=1)
        widths = widths or {}
        for key, label in columns:
            tree.heading(key, text=label)
            tree.column(key, width=widths.get(key, 120), minwidth=55, anchor="center")
        self._install_tree_cell_selection(tree)
        tree.bind("<Button-3>", lambda e, t=tree: self._tree_popup(e, t))
        return tree

    def _clipboard_set(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text or "")
            self.update_idletasks()
            return True
        except Exception:
            LOGGER.exception("클립보드 복사 실패")
            return False

    def _tree_hide_cell_marker(self, tree):
        if isinstance(tree, ProductTable):
            tree.clear_cell_marker()
            return
        marker = getattr(tree, "_cell_marker", None)
        if marker is not None:
            try:
                for part in marker:
                    part.place_forget()
            except Exception:
                pass

    def _tree_cell_value(self, tree, row, col):
        if not row or not col:
            return ""
        if col == "#0":
            return tree.item(row, "text") or ""
        try:
            idx = int(col[1:]) - 1
            vals = tree.item(row, "values")
            if 0 <= idx < len(vals):
                return "" if vals[idx] is None else str(vals[idx])
        except Exception:
            pass
        return ""

    def _tree_mark_cell(self, tree, row, col):
        if isinstance(tree, ProductTable):
            tree.mark_cell(row, col)
            return
        if not row or not col:
            return
        try:
            box = tree.bbox(row, col)
        except Exception:
            box = ()
        if not box:
            self._tree_hide_cell_marker(tree)
            return
        x, y, w, h = box
        marker = getattr(tree, "_cell_marker", None)
        if marker is None:
            marker = [tk.Frame(tree, bg="#ff6a00", bd=0) for _ in range(4)]
            tree._cell_marker = marker
        thickness = 3
        top, bottom, left, right = marker
        top.place(x=x, y=y, width=max(1,w), height=thickness)
        bottom.place(x=x, y=y+max(0,h-thickness), width=max(1,w), height=thickness)
        left.place(x=x, y=y, width=thickness, height=max(1,h))
        right.place(x=x+max(0,w-thickness), y=y, width=thickness, height=max(1,h))
        for part in marker:
            part.lift()
        tree._active_cell = (str(row), str(col))

    def _tree_click_cell(self, event, tree):
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        region = tree.identify_region(event.x, event.y)
        if row and col and region in ("cell", "tree"):
            tree._active_cell = (str(row), str(col))
            self.after_idle(lambda t=tree, r=str(row), c=str(col): self._tree_mark_cell(t, r, c))
        else:
            tree._active_cell = None
            self._tree_hide_cell_marker(tree)

    def _tree_copy_current(self, tree):
        selected = tree.selection()
        active = getattr(tree, "_active_cell", None)
        if active and len(selected) <= 1:
            self._tree_copy_cell(tree, active[0], active[1])
            return "break"
        return self._tree_copy_rows(tree, include_headers=False)

    def _install_tree_cell_selection(self, tree):
        tree._active_cell = None
        tree._cell_marker = None
        tree.bind("<Button-1>", lambda e, t=tree: self._tree_click_cell(e, t), add="+")
        tree.bind("<MouseWheel>", lambda e, t=tree: self._tree_hide_cell_marker(t), add="+")
        tree.bind("<Button-4>", lambda e, t=tree: self._tree_hide_cell_marker(t), add="+")
        tree.bind("<Button-5>", lambda e, t=tree: self._tree_hide_cell_marker(t), add="+")
        tree.bind("<Configure>", lambda e, t=tree: self._tree_hide_cell_marker(t), add="+")
        tree.bind("<Control-c>", lambda e, t=tree: self._tree_copy_current(t))
        tree.bind("<Control-C>", lambda e, t=tree: self._tree_copy_current(t))

    def _tree_copy_rows(self, tree, include_headers=False):
        selected = tree.selection()
        if not selected:
            focus = tree.focus()
            selected = (focus,) if focus else ()
        if not selected:
            return "break"
        lines = []
        cols = list(tree["columns"])
        has_tree_col = "tree" in str(tree.cget("show"))
        if include_headers:
            heads = ([str(tree.heading("#0", "text"))] if has_tree_col else []) + [str(tree.heading(c, "text")) for c in cols]
            lines.append("\t".join(heads))
        for iid in selected:
            vals = list(tree.item(iid, "values"))
            if has_tree_col:
                vals.insert(0, tree.item(iid, "text"))
            lines.append("\t".join("" if v is None else str(v) for v in vals))
        self._clipboard_set("\r\n".join(lines))
        return "break"

    def _tree_copy_cell(self, tree, row, col):
        if not row or not col:
            return
        try:
            if col == "#0":
                self._clipboard_set(tree.item(row, "text") or "")
                return
            idx = int(col[1:]) - 1
            vals = tree.item(row, "values")
            if 0 <= idx < len(vals):
                self._clipboard_set("" if vals[idx] is None else str(vals[idx]))
        except Exception:
            LOGGER.exception("표 셀 복사 실패")

    def _tree_popup(self, event, tree):
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if row and row not in tree.selection():
            tree.selection_set(row)
            tree.focus(row)
        if row and col:
            tree._active_cell = (str(row), str(col))
            self._tree_mark_cell(tree, row, col)
        menu = tk.Menu(self, tearoff=0)
        # 상품관리에서는 링크 컬럼을 따로 차지하지 않고 옵션 행 우클릭으로 연다.
        if tree is getattr(self, 'prod_tree', None) and row and str(row).isdigit():
            menu.add_command(label="옵션 상세 보기", command=self.show_product_details)
            menu.add_command(label="쿠팡에서 상품 열기", command=self.open_product_link)
            menu.add_command(label="노출ID 이력 보기", command=self.show_exposure_history)
            menu.add_separator()
        if row and col:
            menu.add_command(label="셀 복사", command=lambda: self._tree_copy_cell(tree, row, col))
        menu.add_command(label="선택 행 복사", command=lambda: self._tree_copy_rows(tree, False))
        menu.add_command(label="헤더 포함 복사", command=lambda: self._tree_copy_rows(tree, True))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------------- Display filter ----------------
    def multitap_only_enabled(self):
        return self.db.setting("display_multitap_only", "1") == "1"

    @staticmethod
    def _multitap_text_clause(alias="p"):
        a = (alias + ".") if alias else ""
        return (
            f"(COALESCE({a}category,'') LIKE '%멀티탭%' "
            f"OR COALESCE({a}product_name,'') LIKE '%멀티탭%' "
            f"OR COALESCE({a}display_name,'') LIKE '%멀티탭%' "
            f"OR COALESCE({a}option_name,'') LIKE '%멀티탭%')"
        )

    def _multitap_product_clause(self, alias="p"):
        a = (alias + ".") if alias else ""
        raw = self._multitap_text_clause("")
        direct = self._multitap_text_clause(alias)
        return (
            f"({direct} "
            f"OR (COALESCE({a}model,'')<>'' AND {a}model IN "
            f"(SELECT model FROM products WHERE COALESCE(model,'')<>'' AND {raw})) "
            f"OR (COALESCE({a}registered_product_id,'')<>'' AND {a}registered_product_id IN "
            f"(SELECT registered_product_id FROM products WHERE COALESCE(registered_product_id,'')<>'' AND {raw})))"
        )

    def visible_models(self):
        if not self.multitap_only_enabled():
            return self.db.distinct_models()
        clause = self._multitap_product_clause('p')
        rows = self.db.query(f"SELECT DISTINCT p.model FROM products p WHERE COALESCE(p.model,'')<>'' AND {clause} ORDER BY p.model COLLATE NOCASE")
        return [r[0] for r in rows if r[0]]

    # ---------------- Dashboard ----------------
    # ---------------- Product overview dashboard ----------------
    def _visible_product_rows(self):
        sql = "SELECT p.* FROM products p"
        if self.multitap_only_enabled():
            sql += " WHERE " + self._multitap_product_clause('p')
        sql += " ORDER BY p.id"
        return self.db.query(sql)

    def _product_event_rows(self, include_ack=False, limit=120):
        sql = "SELECT e.* FROM product_events e"
        params = []
        where = []
        if self.multitap_only_enabled():
            sql += " LEFT JOIN products p ON p.id=e.product_row_id"
            where.append("p.id IS NOT NULL AND " + self._multitap_product_clause('p'))
        if not include_ack:
            where.append("e.acknowledged_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.created_at DESC,e.id DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, params)

    @staticmethod
    def _product_event_type_label(event_type):
        return {
            'exposure_id':'노출ID 변경',
            'sale_status':'판매상태 변경',
        }.get(str(event_type or ''), str(event_type or '변경'))

    @staticmethod
    def _product_event_source_label(source):
        src=str(source or '')
        if src.startswith('revenue:'):
            return '매출내역 ' + src.split(':',1)[1]
        return {
            'api-item':'상품 API', 'api-single':'상품 API 단일옵션', 'sibling':'동일옵션 채널',
            'manual':'수동', 'excel-import':'Excel', 'inventory-api':'옵션 상태 API',
        }.get(src, src or '-')

    def _open_products_from_dashboard(self, sale_status=None, fulfillment=None, exposure_only=False,
                                      special_filter='', query=''):
        self._pending_product_filter = str(special_filter or '')
        self.show_page('products')
        if query and hasattr(self,'prod_q'):
            self.prod_q.set(str(query))
        if sale_status and hasattr(self,'prod_sale_status'):
            self.prod_sale_status.set(str(sale_status))
        if fulfillment and hasattr(self,'prod_fulfillment'):
            self.prod_fulfillment.set(str(fulfillment))
        if exposure_only and hasattr(self,'prod_exposure_only'):
            self.prod_exposure_only.set(True)
        self.refresh_products()

    def _dashboard_event_selection(self):
        tree=getattr(self,'product_event_tree',None)
        if not tree:
            return []
        out=[]
        for iid in tree.selection():
            try:
                out.append(int(str(iid)))
            except Exception:
                pass
        return out

    def _ack_product_events(self, all_visible=False):
        ids=[]
        if all_visible:
            tree=getattr(self,'product_event_tree',None)
            if tree:
                for iid in tree.get_children(''):
                    try: ids.append(int(str(iid)))
                    except Exception: pass
        else:
            ids=self._dashboard_event_selection()
        if not ids:
            return messagebox.showinfo(APP_NAME,'확인완료 처리할 변경 항목을 선택하세요.')
        placeholders=','.join('?' for _ in ids)
        self.db.execute(f"UPDATE product_events SET acknowledged_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})", ids)
        self._refresh_product_dashboard_events()
        self._refresh_product_dashboard_cards()

    def _open_dashboard_event_product(self, event=None):
        ids=self._dashboard_event_selection()
        if not ids:
            return
        row=self.db.one('SELECT * FROM product_events WHERE id=?',(ids[0],))
        if not row:
            return
        query=str(row['option_id'] or row['registered_product_id'] or row['model'] or '').strip()
        self._open_products_from_dashboard(query=query)

    def _refresh_product_dashboard_cards(self):
        if not hasattr(self,'product_dashboard_card_vars'):
            return
        rows=self._visible_product_rows()
        hist=self._exposure_history_map()
        keys={self._exposure_option_key(r) for r in rows}
        counts={
            'total':len(rows),
            'selling':sum(1 for r in rows if str(r['sale_status'] or '미확인')=='판매중'),
            'soldout':sum(1 for r in rows if str(r['sale_status'] or '미확인')=='품절'),
            'stopped':sum(1 for r in rows if str(r['sale_status'] or '미확인')=='판매중지'),
            'api_errors':sum(1 for r in rows if str(r['sale_status_error'] or '').strip()),
            'rocket':sum(1 for r in rows if str(r['fulfillment_type'] or '')=='로켓그로스'),
            'missing_exposure':sum(1 for r in rows if not str(r['exposed_product_id'] or '').strip()),
            'missing_status':sum(1 for r in rows if str(r['sale_status'] or '미확인') in ('','미확인')),
            'missing_model':sum(1 for r in rows if not str(r['model'] or '').strip()),
            'history_changed':sum(1 for key in keys if len(hist.get(key,[]))>1),
        }
        ev_rows=self._product_event_rows(include_ack=False,limit=100000)
        event_counts={}
        for r in ev_rows:
            k=str(r['event_type'] or '')
            event_counts[k]=event_counts.get(k,0)+1
        counts['exposure_alerts']=event_counts.get('exposure_id',0)
        counts['status_alerts']=event_counts.get('sale_status',0)
        for key,var in self.product_dashboard_card_vars.items():
            var.set(str(counts.get(key,0)))
        if hasattr(self,'product_dashboard_sync_var'):
            last_product=self.db.setting('api_last_sync','없음') or '없음'
            last_sale=self.db.setting('api_last_sale_status_sync','없음') or '없음'
            errors=self.db.setting('api_last_sync_errors','0') or '0'
            self.product_dashboard_sync_var.set(
                f'마지막 상품 동기화: {last_product}   |   판매상태 확인: {last_sale}   |   최근 상품조회 오류: {errors}건')

    def _refresh_product_dashboard_events(self):
        tree=getattr(self,'product_event_tree',None)
        if not tree:
            return
        try:
            if not tree.winfo_exists():
                return
        except Exception:
            return
        tree.delete(*tree.get_children())
        include_ack=bool(getattr(self,'product_event_include_ack',None) and self.product_event_include_ack.get())
        rows=self._product_event_rows(include_ack=include_ack,limit=150)
        for r in rows:
            ack='확인완료' if r['acknowledged_at'] else '확인필요'
            tags=('ack',) if r['acknowledged_at'] else (('exposure',) if r['event_type']=='exposure_id' else ('status',))
            tree.insert('', 'end', iid=str(r['id']), values=(
                self._product_event_type_label(r['event_type']), r['model'], r['option_name'], r['option_id'],
                r['old_value'], r['new_value'], self._product_event_source_label(r['source']),
                r['created_at'], ack), tags=tags)
        if hasattr(self,'product_event_count_label'):
            unack=sum(1 for r in rows if not r['acknowledged_at'])
            self.product_event_count_label.config(text=f'표시 {len(rows)}건 · 확인필요 {unack}건')

    def build_product_dashboard(self):
        self.page_header('상품 현황', '상품 상태 · 노출상품 ID 변경 · 데이터 누락을 한 화면에서 확인합니다.')
        top=ttk.Frame(self.content); top.pack(fill='x',pady=(0,10))
        self.product_dashboard_sync_var=tk.StringVar(value='')
        ttk.Label(top,textvariable=self.product_dashboard_sync_var,foreground='#666').pack(side='left')
        ttk.Button(top,text='새로고침',style='Toolbar.TButton',command=lambda:(self._refresh_product_dashboard_cards(),self._refresh_product_dashboard_events())).pack(side='right')

        self.product_dashboard_card_vars={}
        cards1=ttk.Frame(self.content); cards1.pack(fill='x',pady=(0,6))
        cards2=ttk.Frame(self.content); cards2.pack(fill='x',pady=(0,12))
        def card(parent,key,title,command=None):
            box=ttk.LabelFrame(parent,text=title,padding=(10,8)); box.pack(side='left',fill='x',expand=True,padx=3)
            var=tk.StringVar(value='0'); self.product_dashboard_card_vars[key]=var
            if command:
                b=ttk.Button(box,textvariable=var,style='Accent.TButton',command=command); b.pack(fill='x')
            else:
                ttk.Label(box,textvariable=var,style='CardValue.TLabel').pack(anchor='center')
        card(cards1,'total','전체 옵션',lambda:self._open_products_from_dashboard())
        card(cards1,'selling','판매중',lambda:self._open_products_from_dashboard(sale_status='판매중'))
        card(cards1,'soldout','품절',lambda:self._open_products_from_dashboard(sale_status='품절'))
        card(cards1,'stopped','판매중지',lambda:self._open_products_from_dashboard(sale_status='판매중지'))
        card(cards1,'api_errors','상태조회 오류',lambda:self._open_products_from_dashboard(special_filter='status_error'))
        card(cards1,'rocket','로켓그로스',lambda:self._open_products_from_dashboard(fulfillment='로켓그로스'))
        card(cards2,'exposure_alerts','노출ID 변경 알림')
        card(cards2,'history_changed','노출ID 변경이력',lambda:self._open_products_from_dashboard(exposure_only=True))
        card(cards2,'missing_exposure','노출ID 미확인',lambda:self._open_products_from_dashboard(special_filter='missing_exposure'))
        card(cards2,'missing_status','판매상태 미확인',lambda:self._open_products_from_dashboard(sale_status='미확인'))
        card(cards2,'missing_model','모델명 없음',lambda:self._open_products_from_dashboard(special_filter='missing_model'))
        card(cards2,'status_alerts','판매상태 변경 알림')

        eventbar=ttk.Frame(self.content); eventbar.pack(fill='x',pady=(2,6))
        ttk.Label(eventbar,text='최근 변경 알림',font=('맑은 고딕',11,'bold')).pack(side='left')
        self.product_event_count_label=ttk.Label(eventbar,text='',foreground='#666'); self.product_event_count_label.pack(side='left',padx=10)
        self.product_event_include_ack=tk.BooleanVar(value=False)
        ttk.Checkbutton(eventbar,text='확인완료 포함',variable=self.product_event_include_ack,command=self._refresh_product_dashboard_events).pack(side='right')
        ttk.Button(eventbar,text='표시 전체 확인완료',style='Toolbar.TButton',command=lambda:self._ack_product_events(True)).pack(side='right',padx=(0,5))
        ttk.Button(eventbar,text='선택 확인완료',style='Toolbar.TButton',command=self._ack_product_events).pack(side='right',padx=(0,5))
        ttk.Button(eventbar,text='상품관리 열기',style='Toolbar.TButton',command=self._open_dashboard_event_product).pack(side='right',padx=(0,5))

        cols=[('type','구분'),('model','모델명'),('option','옵션명'),('optionid','옵션 ID'),('old','이전 값'),('new','현재 값'),('source','출처'),('at','변경일시'),('ack','상태')]
        self.product_event_tree=self.make_tree(self.content,cols,{'type':105,'model':100,'option':180,'optionid':120,'old':120,'new':120,'source':110,'at':145,'ack':80},height=10)
        self.product_event_tree.tag_configure('exposure',background='#fff2cc')
        self.product_event_tree.tag_configure('status',background='#e8f2ff')
        self.product_event_tree.tag_configure('ack',foreground='#888888')
        self.product_event_tree.bind('<Double-1>',self._open_dashboard_event_product)

        note=ttk.Label(self.content,text='※ 변경 알림은 v0.3.4 이후 현재값이 실제로 바뀌는 순간에만 1건 저장됩니다. 같은 값을 다시 조회해도 DB 이력/알림 행이 계속 늘어나지 않습니다.',foreground='#666')
        note.pack(anchor='w',pady=(7,0))
        self._refresh_product_dashboard_cards(); self._refresh_product_dashboard_events()

    # ---------------- Products ----------------
    def product_hide_rocket_enabled(self):
        # This preference affects only the product-management query.
        return self.db.setting('product_hide_rocket', '1') == '1'

    def change_product_hide_rocket(self):
        hidden = bool(self.prod_hide_rocket.get())
        self.db.set_setting('product_hide_rocket', '1' if hidden else '0')
        if hidden and self.prod_fulfillment.get() == '로켓그로스':
            self.prod_fulfillment.set('전체')
        self.prod_fulfillment_cb.configure(values=['전체', '판매자배송'] if hidden else ['전체', '판매자배송', '로켓그로스'])
        self._reset_product_page_and_refresh()

    def product_view_mode(self):
        mode = self.db.setting("product_view_mode", "WING 보기")
        if mode == "묶음 보기":
            mode = "WING 보기"
        return mode if mode in ("WING 보기", "전체 열 보기") else "WING 보기"

    def change_product_view(self, event=None):
        if hasattr(self, "prod_view"):
            self.db.set_setting("product_view_mode", self.prod_view.get())
        self.prod_page = 1
        self.show_page("products")

    def _make_product_group_tree(self):
        tree = ProductTable(self.content, self)
        tree.bind('<Button-3>', lambda e, t=tree: self._tree_popup(e, t))
        tree.bind('<<TreeviewSelect>>', lambda e: self.update_product_detail())
        tree.bind('<ButtonRelease-1>', self.product_tree_click_action, add='+')
        tree.bind('<Double-1>', self.product_tree_double_click)
        self.prod_tree_holder = tree.holder
        return tree

    @staticmethod
    def _exposure_option_key(row):
        oid = str(row['option_id'] or '').strip()
        sid = str(row['seller_product_item_id'] or '').strip()
        if oid:
            return 'v:' + oid
        if sid:
            return 's:' + sid
        return 'r:' + str(row['id'])

    def _exposure_history_map(self):
        out = {}
        # api-list is only a seller-product-level guess and must not be shown
        # as a real option-level exposure history/change.
        rows = self.db.query("""SELECT option_key,exposed_product_id,first_seen,last_seen
                              FROM product_exposure_history
                              WHERE source<>'api-list' ORDER BY id""")
        for r in rows:
            key = str(r['option_key'] or '')
            pid = str(r['exposed_product_id'] or '').strip()
            if not key or not pid:
                continue
            bucket = out.setdefault(key, [])
            if pid not in bucket:
                bucket.append(pid)
        return out

    def _exposure_history_count_map(self):
        rows=self.db.query("""SELECT option_key,COUNT(*) AS cnt
                             FROM product_exposure_history
                             WHERE COALESCE(exposed_product_id,'')<>''
                             GROUP BY option_key""")
        return {str(r['option_key'] or ''): int(r['cnt'] or 0) for r in rows if str(r['option_key'] or '')}

    @staticmethod
    def _product_group_key(row):
        reg = str(row['registered_product_id'] or '').strip()
        pid = str(row['exposed_product_id'] or '').strip()
        model = str(row['model'] or '').strip()
        if reg:
            return 'reg:' + reg
        if pid:
            return 'pid:' + pid
        if model:
            return 'model:' + model
        return 'row:' + str(row['id'])

    def build_products(self):
        self.page_header("상품 관리")
        self.prod_special_filter=str(getattr(self,'_pending_product_filter','') or '')
        self._pending_product_filter=''
        self.prod_page=1
        page_size_saved=str(self.db.setting('product_page_size','30') or '30')
        if page_size_saved not in ('20','30','50','100'):
            page_size_saved='30'
        self.prod_page_size=tk.StringVar(value=page_size_saved)

        filter_box=ttk.Frame(self.content); filter_box.pack(fill='x',pady=(0,8))
        self.prod_q=tk.StringVar(); self.prod_fulfillment=tk.StringVar(value='전체'); self.prod_sale_status=tk.StringVar(value='전체')
        ttk.Button(filter_box,text='F5 검색',style='Toolbar.TButton',command=self.open_search_dialog).grid(row=0,column=0,sticky='w')
        self.prod_search_label=ttk.Label(filter_box,text='',foreground='#526a85',width=24,anchor='w')
        self.prod_search_label.grid(row=0,column=1,sticky='ew',padx=(10,14))
        filter_box.columnconfigure(1,weight=1)
        ttk.Label(filter_box,text='배송',style='Filter.TLabel').grid(row=0,column=2)
        fulfillment_cb=ttk.Combobox(filter_box,textvariable=self.prod_fulfillment,
            values=['전체','판매자배송'] if self.product_hide_rocket_enabled() else ['전체','판매자배송','로켓그로스'],state='readonly',width=10)
        self.prod_fulfillment_cb=fulfillment_cb
        fulfillment_cb.grid(row=0,column=3,padx=(5,14))
        ttk.Label(filter_box,text='판매상태',style='Filter.TLabel').grid(row=0,column=4)
        sale_cb=ttk.Combobox(filter_box,textvariable=self.prod_sale_status,values=['전체','판매중','품절','판매중지','미확인'],state='readonly',width=10)
        sale_cb.grid(row=0,column=5,padx=(5,10))
        ttk.Button(filter_box,text='검색 해제',style='Toolbar.TButton',command=self.clear_product_search).grid(row=0,column=6)

        options=ttk.Frame(self.content); options.pack(fill='x',pady=(0,9))
        self.prod_hide_rocket=tk.BooleanVar(value=self.product_hide_rocket_enabled())
        ttk.Checkbutton(options,text='로켓그로스 숨기기',variable=self.prod_hide_rocket,command=self.change_product_hide_rocket).pack(side='left')
        self.prod_exposure_only=tk.BooleanVar(value=False)
        ttk.Checkbutton(options,text='노출ID 변경만',variable=self.prod_exposure_only,command=self._reset_product_page_and_refresh).pack(side='left',padx=(16,0))
        self.prod_view=tk.StringVar(value=self.product_view_mode())
        view=ttk.Combobox(options,textvariable=self.prod_view,values=['WING 보기','전체 열 보기'],state='readonly',width=12)
        view.pack(side='right')
        ttk.Label(options,text='보기',style='Filter.TLabel').pack(side='right',padx=(0,6))
        view.bind('<<ComboboxSelected>>',self.change_product_view)

        if self.prod_special_filter:
            labels={'missing_exposure':'노출ID 미확인','missing_model':'모델명 없음','missing_link':'상품링크 불가','status_error':'판매상태 API 조회오류'}
            special=ttk.Frame(self.content); special.pack(fill='x',pady=(0,6))
            self.prod_special_label=ttk.Label(special,text='대시보드 필터: '+labels.get(self.prod_special_filter,self.prod_special_filter),foreground='#b00020')
            self.prod_special_label.pack(side='left')
            ttk.Button(special,text='필터 해제',style='Toolbar.TButton',command=self._clear_product_special_filter).pack(side='left',padx=8)

        actions=ttk.Frame(self.content); actions.pack(fill='x',pady=(0,7))
        ttk.Button(actions,text='추가',style='Accent.TButton',command=self.add_product).pack(side='left')
        ttk.Button(actions,text='수정',style='Toolbar.TButton',command=self.edit_product).pack(side='left',padx=5)
        ttk.Button(actions,text='선택 삭제',style='Danger.TButton',command=self.delete_product).pack(side='left')
        ttk.Separator(actions,orient='vertical').pack(side='left',fill='y',padx=10)
        ttk.Button(actions,text='Excel 가져오기',style='Toolbar.TButton',command=self.import_excel_workbook).pack(side='left',padx=5)
        ttk.Button(actions,text='노출ID 이력',style='Toolbar.TButton',command=self.show_exposure_history).pack(side='left')

        # Summary + pagination. Pagination is always based on registered-product groups,
        # never on individual option rows, so one product is never split across pages.
        nav=ttk.Frame(self.content); nav.pack(fill='x',pady=(0,6))
        self.prod_count_label=ttk.Label(nav,text='',foreground='#444',font=('맑은 고딕',10,'bold'))
        self.prod_count_label.pack(side='left')
        self.prod_pager=ttk.Frame(nav); self.prod_pager.pack(side='right')
        ttk.Label(self.prod_pager,text='페이지당').pack(side='left',padx=(0,4))
        size_cb=ttk.Combobox(self.prod_pager,textvariable=self.prod_page_size,values=['20','30','50','100'],state='readonly',width=5)
        size_cb.pack(side='left')
        size_cb.bind('<<ComboboxSelected>>',self._product_page_size_changed)
        self.prod_page_info=tk.StringVar(value='')
        ttk.Label(self.prod_pager,textvariable=self.prod_page_info,foreground='#666').pack(side='left',padx=(8,8))
        self.prod_page_buttons=ttk.Frame(self.prod_pager); self.prod_page_buttons.pack(side='left')

        if self.prod_view.get()=='WING 보기':
            ttk.Button(actions,text='모두 펼치기',command=lambda:self.set_all_product_groups(True)).pack(side='right')
            ttk.Button(actions,text='모두 접기',command=lambda:self.set_all_product_groups(False)).pack(side='right',padx=6)
            detail=ttk.LabelFrame(self.content,text='선택 옵션',padding=(10,6)); detail.pack(side='bottom',fill='x',pady=(6,0))
            self.prod_detail_var=tk.StringVar(value='더블클릭 / Enter: 상세 보기 · 우클릭: 복사 / 상품 열기 / ID 이력')
            ttk.Button(detail,text='상세 보기',command=self.show_product_details).pack(side='right',padx=(10,0))
            detail_label=ttk.Label(detail,textvariable=self.prod_detail_var,anchor='w',justify='left',wraplength=680)
            detail_label.pack(side='left',fill='x',expand=True)
            detail_label.bind('<Configure>',lambda e:detail_label.configure(wraplength=max(200,e.width)))
            self.prod_tree=self._make_product_group_tree()
        else:
            cols=[('id','ID'),('fulfillment','배송'),('sale_status','판매상태'),('category','분류'),('model','모델명'),('option','옵션명'),('gift','사은품'),('sale','판매가'),('coupon','쿠폰가'),('map','지도가'),('diff','가격차'),('regid','등록상품 ID'),('productid','노출상품 ID'),('history','노출ID 이력'),('optionid','옵션 ID'),('selleritemid','업체옵션 ID'),('note','비고')]
            self.prod_tree=self.make_tree(self.content,cols,{'id':55,'model':115,'option':220,'note':190,'category':90,'sale_status':105,'fulfillment':95,'regid':125,'productid':120,'history':100,'optionid':128,'selleritemid':130})
            self.prod_tree.bind('<ButtonRelease-1>',self.product_tree_click_action,add='+')
            self.prod_tree.bind('<Double-1>',self.product_tree_double_click)

        fulfillment_cb.bind('<<ComboboxSelected>>',lambda e:self._reset_product_page_and_refresh())
        sale_cb.bind('<<ComboboxSelected>>',lambda e:self._reset_product_page_and_refresh())
        self.refresh_products()

    def _product_page_size_value(self):
        try:
            value=int(self.prod_page_size.get())
        except Exception:
            value=30
        return value if value in (20,30,50,100) else 30

    def _reset_product_page_and_refresh(self):
        self.prod_page=1
        self.refresh_products()

    def _product_page_size_changed(self, event=None):
        self.db.set_setting('product_page_size',str(self._product_page_size_value()))
        self.prod_page=1
        self.refresh_products()

    def _goto_product_page(self, page):
        try:
            page=int(page)
        except Exception:
            page=1
        max_page=max(1,int(getattr(self,'prod_total_pages',1) or 1))
        self.prod_page=max(1,min(page,max_page))
        self.refresh_products()

    def _render_product_pager(self, total_groups):
        page_size=self._product_page_size_value()
        total_pages=max(1,(int(total_groups)+page_size-1)//page_size)
        self.prod_total_pages=total_pages
        self.prod_page=max(1,min(int(getattr(self,'prod_page',1) or 1),total_pages))
        if hasattr(self,'prod_page_info'):
            self.prod_page_info.set(f'{self.prod_page} / {total_pages} 페이지')
        frame=getattr(self,'prod_page_buttons',None)
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        ttk.Button(frame,text='‹',width=3,state=('normal' if self.prod_page>1 else 'disabled'),command=lambda:self._goto_product_page(self.prod_page-1)).pack(side='left',padx=1)
        start=max(1,self.prod_page-2); end=min(total_pages,start+4); start=max(1,end-4)
        if start>1:
            ttk.Button(frame,text='1',width=3,command=lambda:self._goto_product_page(1)).pack(side='left',padx=1)
            if start>2: ttk.Label(frame,text='…').pack(side='left',padx=2)
        for n in range(start,end+1):
            style='Accent.TButton' if n==self.prod_page else 'Toolbar.TButton'
            ttk.Button(frame,text=str(n),width=3,style=style,command=lambda x=n:self._goto_product_page(x)).pack(side='left',padx=1)
        if end<total_pages:
            if end<total_pages-1: ttk.Label(frame,text='…').pack(side='left',padx=2)
            ttk.Button(frame,text=str(total_pages),width=3,command=lambda:self._goto_product_page(total_pages)).pack(side='left',padx=1)
        ttk.Button(frame,text='›',width=3,state=('normal' if self.prod_page<total_pages else 'disabled'),command=lambda:self._goto_product_page(self.prod_page+1)).pack(side='left',padx=1)

    def _clear_product_special_filter(self):
        self.prod_special_filter=''
        if hasattr(self,'prod_special_label'):
            try: self.prod_special_label.destroy()
            except Exception: pass
        self.prod_page=1
        self.refresh_products()

    def _product_query_rows(self):
        raw_q=self.prod_q.get().strip(); ft=self.prod_fulfillment.get() if hasattr(self,'prod_fulfillment') else '전체'; sale_st=self.prod_sale_status.get() if hasattr(self,'prod_sale_status') else '전체'
        like=f"%{raw_q}%"; digits=''.join(ch for ch in raw_q if ch.isdigit()); id_term=digits if len(digits)>=6 else raw_q; id_like=f"%{id_term}%"
        sql=("SELECT p.* FROM products p WHERE (COALESCE(p.model,'') LIKE ? OR COALESCE(p.product_name,'') LIKE ? OR COALESCE(p.display_name,'') LIKE ? OR COALESCE(p.option_name,'') LIKE ? OR COALESCE(p.registered_product_id,'') LIKE ? OR COALESCE(p.exposed_product_id,'') LIKE ? OR COALESCE(p.option_id,'') LIKE ? OR COALESCE(p.seller_product_item_id,'') LIKE ?)")
        params=[like,like,like,like,id_like,id_like,id_like,id_like]
        if self.multitap_only_enabled(): sql += ' AND ' + self._multitap_product_clause('p')
        if self.product_hide_rocket_enabled(): sql += " AND TRIM(COALESCE(p.fulfillment_type,''))<>'로켓그로스'"
        if ft and ft!='전체': sql+=" AND COALESCE(NULLIF(p.fulfillment_type,''),'판매자배송')=?"; params.append(ft)
        if sale_st and sale_st!='전체': sql+=" AND COALESCE(NULLIF(p.sale_status,''),'미확인')=?"; params.append(sale_st)
        sql += (" ORDER BY CASE WHEN ?<>'' AND (p.option_id=? OR p.seller_product_item_id=?) THEN 0 ELSE 1 END, COALESCE(p.registered_product_id,''), COALESCE(p.product_name,''), COALESCE(p.model,''), CASE COALESCE(p.fulfillment_type,'판매자배송') WHEN '판매자배송' THEN 0 WHEN '로켓그로스' THEN 1 ELSE 2 END, CASE COALESCE(p.sale_status,'미확인') WHEN '판매중' THEN 0 WHEN '품절' THEN 1 WHEN '판매중지' THEN 2 ELSE 3 END, p.id")
        params.extend([id_term,id_term,id_term])
        rows=self.db.query(sql,params)
        special=str(getattr(self,'prod_special_filter','') or '')
        if special=='missing_exposure':
            rows=[r for r in rows if not str(r['exposed_product_id'] or '').strip()]
        elif special=='missing_model':
            rows=[r for r in rows if not str(r['model'] or '').strip()]
        elif special=='status_error':
            rows=[r for r in rows if str(r['sale_status_error'] or '').strip()]
        elif special=='missing_link':
            rows=[r for r in rows if not str(r['exposed_product_id'] or '').strip() or not str(r['option_id'] or '').strip()]
        return rows

    def clear_product_search(self):
        self.prod_q.set('')
        self._reset_product_page_and_refresh()

    def refresh_products(self):
        if hasattr(self,'prod_search_label') and self.prod_search_label.winfo_exists():
            query=self.prod_q.get().strip()
            self.prod_search_label.configure(text=('검색: '+query[:28]+('…' if len(query)>28 else '')) if query else '')
        rows=self._product_query_rows(); hist=self._exposure_history_map(); hist_counts=self._exposure_history_count_map()
        if getattr(self,'prod_exposure_only',None) and self.prod_exposure_only.get():
            rows=[r for r in rows if len(hist.get(self._exposure_option_key(r), [])) > 1]

        # Group first, then paginate by registered-product group.  This keeps all
        # seller-delivery / Rocket Growth options for one product on the same page.
        grouped={}
        for r in rows:
            grouped.setdefault(self._product_group_key(r),[]).append(r)
        group_items=list(grouped.items())
        total_groups=len(group_items); total_options=len(rows)
        self._render_product_pager(total_groups)
        page_size=self._product_page_size_value(); start=(self.prod_page-1)*page_size; end=start+page_size
        page_groups=group_items[start:end]
        page_keys={k for k,_ in page_groups}
        page_rows=[r for r in rows if self._product_group_key(r) in page_keys]

        if not isinstance(self.prod_tree, ProductTable):
            self.prod_tree.delete(*self.prod_tree.get_children())
        self._tree_hide_cell_marker(self.prod_tree)
        changed=sum(1 for r in rows if len(hist.get(self._exposure_option_key(r), [])) > 1)
        if hasattr(self,'prod_count_label'):
            if total_groups:
                first_no=start+1; last_no=min(end,total_groups)
                span=f'{first_no}–{last_no} / {total_groups}개 등록상품'
            else:
                span='0개 등록상품'
            extra=f' · 노출ID 변경 {changed}개' if changed else ''
            self.prod_count_label.config(text=f'{span} · 표시 옵션 {total_options}개{extra}')

        if isinstance(self.prod_tree, ProductTable):
            table_groups=[]
            for key, items in page_groups:
                first=items[0]
                name=str(first['product_name'] or first['display_name'] or first['model'] or '상품명 없음').strip()
                regid=str(first['registered_product_id'] or '').strip()
                market=sum(str(r['fulfillment_type'] or '판매자배송').strip()!='로켓그로스' for r in items)
                rocket=len(items)-market
                meta=[f'{len(items)}개 옵션']
                if market: meta.append(f'판매자배송 {market}')
                if rocket: meta.append(f'로켓그로스 {rocket}')
                if regid: meta.append(f'등록상품 ID {regid}')
                table_rows=[]
                for r in items:
                    fulfillment=str(r['fulfillment_type'] or '판매자배송').strip()
                    status=str(r['sale_status'] or '미확인').strip()
                    stock='—' if fulfillment=='로켓그로스' or r['api_stock_quantity'] is None else f"{int(r['api_stock_quantity']):,}"
                    option=str(r['option_name'] or r['model'] or '옵션명 없음')
                    history=hist.get(self._exposure_option_key(r), [])
                    table_rows.append({'iid':str(r['id']), 'text':option, 'model':str(r['model'] or ''),
                        'error':bool(str(r['sale_status_error'] or '').strip()), 'changed':len(history)>1,
                        'values':(fulfillment,status,money(r['sale_price']) if r['sale_price'] is not None else '—',
                                  money(r['map_price']) if r['map_price'] else '—',stock,'상세 보기')})
                table_groups.append({'iid':'grp:'+key, 'text':name, 'meta':'  ·  '.join(meta), 'rows':table_rows})
            self.prod_tree.set_groups(table_groups)
        else:
            for r in page_rows:
                history=hist.get(self._exposure_option_key(r), [])
                hcount=hist_counts.get(self._exposure_option_key(r),0)
                hlabel=(f'⚠ {hcount}개' if len(history)>1 else (f'{hcount}개' if hcount else ''))
                self.prod_tree.insert('', 'end', iid=str(r['id']), values=(r['id'],r['fulfillment_type'] or '판매자배송',r['sale_status'] or '미확인',r['category'],r['model'],r['option_name'],r['gift'],money(r['sale_price']),money(r['coupon_price']) if r['coupon_price'] else '',money(r['map_price']),money(r['price_diff']),r['registered_product_id'],r['exposed_product_id'],hlabel,r['option_id'],r['seller_product_item_id'],r['note']))
        self.update_product_detail()

    def set_all_product_groups(self, opened):
        if not hasattr(self,'prod_tree'): return
        if isinstance(self.prod_tree, ProductTable):
            self.prod_tree.set_all_open(opened)
            return
        def walk(iid):
            try: self.prod_tree.item(iid,open=opened)
            except Exception: pass
            for child in self.prod_tree.get_children(iid):
                if not str(child).isdigit():
                    walk(child)
        for iid in self.prod_tree.get_children(''):
            if str(iid).startswith('grp:'):
                walk(iid)

    @staticmethod
    def _tree_column_key(tree, column):
        if not column or column == '#0':
            return '#0'
        try:
            idx=int(str(column).lstrip('#'))-1
            cols=list(tree['columns'])
            return str(cols[idx]) if 0 <= idx < len(cols) else ''
        except Exception:
            return ''

    @staticmethod
    def _exposure_source_label(source):
        source=str(source or '')
        if source.startswith('revenue:'):
            return '매출내역 ' + source.split(':',1)[1]
        return {
            'manual':'수동',
            'existing':'기존 데이터',
            'sibling':'동일옵션 채널',
            'api-item':'상품 API 옵션값',
            'api-single':'상품 API 단일옵션',
            'excel-import':'Excel 가져오기',
            'api-list':'상품목록 대표값(미검증)',
        }.get(source, source or '-')

    def _exposure_history_rows(self, row, include_untrusted=True):
        if not row:
            return []
        key=self._exposure_option_key(row)
        sql=("SELECT id,option_key,option_id,seller_product_item_id,registered_product_id,"
             "exposed_product_id,model,option_name,first_seen,last_seen,source "
             "FROM product_exposure_history WHERE option_key=?")
        params=[key]
        if not include_untrusted:
            sql += " AND source<>'api-list'"
        sql += " ORDER BY COALESCE(last_seen,'') DESC,id DESC"
        return self.db.query(sql,params)

    def _resolved_product_link(self, row):
        if not row:
            return ''
        # Current rule: always prefer a canonical URL generated from
        # exposed product ID + vendorItemId. Stored legacy links are only a fallback.
        canonical=self._coupang_product_link(row['exposed_product_id'], '', row['option_id'])
        if canonical:
            return canonical
        return str(row['link'] or '').strip()

    def _selected_product_ids(self, include_group_children=True):
        if not hasattr(self,'prod_tree'):
            return []
        out=[]
        def collect(iid):
            si=str(iid)
            if si.isdigit():
                out.append(int(si)); return
            if include_group_children:
                for child in self.prod_tree.get_children(iid):
                    collect(child)
        for iid in self.prod_tree.selection():
            collect(iid)
        return list(dict.fromkeys(out))

    def product_tree_click_action(self,event=None):
        if not event or not hasattr(self,'prod_tree'):
            return
        row=self.prod_tree.identify_row(event.y)
        col=self.prod_tree.identify_column(event.x)
        if not row or not str(row).isdigit():
            return
        key=self._tree_column_key(self.prod_tree,col)
        if key not in ('history', 'details'):
            return
        self.prod_tree.selection_set(row)
        self.prod_tree.focus(row)
        if key == 'history':
            self.show_exposure_history()
        else:
            self.show_product_details()
        return 'break'

    def product_tree_double_click(self,event=None):
        if not hasattr(self,'prod_tree'): return
        iid=self.prod_tree.identify_row(event.y) if event else self.prod_tree.focus()
        if not iid: return
        si=str(iid)
        if si.startswith('grp:') or si.startswith('sec:'):
            self.prod_tree.item(iid,open=not bool(self.prod_tree.item(iid,'open')))
            return 'break'
        col=self.prod_tree.identify_column(event.x) if event else ''
        key=self._tree_column_key(self.prod_tree,col)
        if key == 'history':
            self.show_exposure_history(); return 'break'
        if isinstance(self.prod_tree, ProductTable):
            self.prod_tree.selection_set(iid)
            self.show_product_details()
        else:
            self.edit_product()
        return 'break'

    def show_exposure_history(self):
        ids=self._selected_product_ids(include_group_children=True)
        if not ids:
            return messagebox.showinfo(APP_NAME,'노출상품 ID 이력을 확인할 옵션 행을 선택하세요.')
        placeholders=','.join('?' for _ in ids)
        products=self.db.query(f'SELECT * FROM products WHERE id IN ({placeholders}) ORDER BY model,option_name,status,id',ids)
        hist_rows=[]
        current_by_key={}
        product_by_key={}
        for pr in products:
            key=self._exposure_option_key(pr)
            current_by_key[key]=str(pr['exposed_product_id'] or '').strip()
            product_by_key[key]=pr
            for h in self._exposure_history_rows(pr,include_untrusted=True):
                hist_rows.append((pr,h))
        if not hist_rows:
            return messagebox.showinfo(APP_NAME,'선택한 옵션에 저장된 노출상품 ID 이력이 없습니다.')

        win=tk.Toplevel(self)
        win.title('노출상품 ID 이력')
        win.geometry('1060x520')
        win.minsize(820,420)
        win.transient(self)

        top=ttk.Frame(win,padding=(12,10,12,6)); top.pack(fill='x')
        ttk.Label(top,text=f'선택 옵션 {len(products)}개 · 이력 {len(hist_rows)}건',font=('맑은 고딕',11,'bold')).pack(side='left')
        ttk.Label(top,text='상품목록 대표값(미검증)은 참고용이며 옵션별 변경 판정에는 사용하지 않습니다.',foreground='#777').pack(side='right')

        holder=ttk.Frame(win,padding=(12,0,12,8)); holder.pack(fill='both',expand=True)
        cols=[('model','모델명'),('option','옵션명'),('oid','옵션 ID'),('current','현재'),('pid','노출상품 ID'),('source','출처'),('first','최초 확인'),('last','최근 확인')]
        tree=ttk.Treeview(holder,columns=[c[0] for c in cols],show='headings',selectmode='browse')
        widths={'model':105,'option':170,'oid':125,'current':55,'pid':125,'source':155,'first':140,'last':140}
        for key,label in cols:
            tree.heading(key,text=label); tree.column(key,width=widths[key],anchor='center')
        y=ttk.Scrollbar(holder,orient='vertical',command=tree.yview); x=ttk.Scrollbar(holder,orient='horizontal',command=tree.xview)
        tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        tree.grid(row=0,column=0,sticky='nsew'); y.grid(row=0,column=1,sticky='ns'); x.grid(row=1,column=0,sticky='ew')
        holder.rowconfigure(0,weight=1); holder.columnconfigure(0,weight=1)
        tree.tag_configure('current',background='#e8f2ff',font=('맑은 고딕',9,'bold'))
        tree.tag_configure('untrusted',foreground='#777')
        row_links={}
        for idx,(pr,h) in enumerate(hist_rows):
            key=self._exposure_option_key(pr)
            pid=str(h['exposed_product_id'] or '').strip()
            is_current=(pid != '' and pid == current_by_key.get(key,''))
            source=str(h['source'] or '')
            tags=[]
            if is_current: tags.append('current')
            if source=='api-list': tags.append('untrusted')
            iid=f'h{idx}'
            tree.insert('', 'end', iid=iid, values=(pr['model'],pr['option_name'],pr['option_id'],'현재' if is_current else '',pid,self._exposure_source_label(source),h['first_seen'],h['last_seen']), tags=tuple(tags))
            row_links[iid]=self._coupang_product_link(pid,'',pr['option_id']) if pid else ''
        tree.selection_set(tree.get_children()[0]); tree.focus(tree.get_children()[0])

        def selected_pid():
            sel=tree.selection()
            if not sel: return ''
            vals=tree.item(sel[0],'values')
            return str(vals[4]) if len(vals)>4 else ''
        def copy_pid():
            pid=selected_pid()
            if pid: self._clipboard_set(pid)
        def open_pid(event=None):
            sel=tree.selection()
            if not sel: return
            link=row_links.get(sel[0],'')
            if link: webbrowser.open_new_tab(link)
        tree.bind('<Double-1>',open_pid)

        bottom=ttk.Frame(win,padding=(12,0,12,12)); bottom.pack(fill='x')
        ttk.Button(bottom,text='선택 상품 열기',command=open_pid).pack(side='left')
        ttk.Button(bottom,text='노출 ID 복사',command=copy_pid).pack(side='left',padx=6)
        ttk.Button(bottom,text='닫기',command=win.destroy).pack(side='right')
        win.grab_set()

    def update_product_detail(self):
        if not hasattr(self,'prod_detail_var'): return
        r=self.selected_product()
        if isinstance(getattr(self,'prod_tree',None), ProductTable):
            if not r:
                self.prod_detail_var.set('더블클릭 / Enter: 상세 보기 · 우클릭: 복사 / 상품 열기 / ID 이력')
            else:
                summary=f"{r['fulfillment_type'] or '판매자배송'}  ·  {r['model'] or '모델 미연결'}  ·  {r['option_name'] or '-'}"
                if r['sale_status_checked_at']: summary += f"\n상태 확인 {r['sale_status_checked_at']}"
                if str(r['sale_status_error'] or '').strip(): summary += '  ·  최근 조회 오류: 상세 보기에서 확인'
                self.prod_detail_var.set(summary)
            return
        if not r:
            self.prod_detail_var.set('옵션 행을 선택하면 주요 ID와 상태 확인 시각이 표시됩니다.'); return
        parts=[f"배송 {r['fulfillment_type'] or '판매자배송'}",f"판매상태 {r['sale_status'] or '미확인'}",f"모델 {r['model'] or '-'}",f"옵션 {r['option_name'] or '-'}",
               f"판매가 {money(r['sale_price'])}",f"지도가 {money(r['map_price']) if r['map_price'] else '-'}",
               f"등록상품ID {r['registered_product_id'] or '-'}",f"노출상품ID {r['exposed_product_id'] or '-'}",f"옵션ID {r['option_id'] or '-'}"]
        if r['api_stock_quantity'] is not None and str(r['fulfillment_type'] or '판매자배송')=='판매자배송':
            parts.append(f"재고 {int(r['api_stock_quantity']):,}")
        if r['sale_status_checked_at']: parts.append(f"상태확인 {r['sale_status_checked_at']}")
        if str(r['sale_status_error'] or '').strip():
            parts.append(f"⚠ 상태조회 오류 {r['sale_status_error']} ({r['sale_status_error_at'] or '-'})")
        key=self._exposure_option_key(r); current_pid=str(r['exposed_product_id'] or '').strip()
        if current_pid:
            src=self.db.one("""SELECT source,last_seen FROM product_exposure_history WHERE option_key=? AND exposed_product_id=? ORDER BY CASE WHEN source='manual' THEN 0 WHEN source LIKE 'revenue%' THEN 1 ELSE 2 END,last_seen DESC,id DESC LIMIT 1""", (key,current_pid))
            if src:
                source=str(src['source'] or '')
                source_label=('매출내역 ' + source.split(':',1)[1]) if source.startswith('revenue:') else {'manual':'수동','existing':'기존 데이터','sibling':'동일옵션 채널','api-item':'상품 API 옵션값','api-single':'상품 API 단일옵션','excel-import':'Excel 가져오기','api-list':'상품목록 대표값(미검증)'}.get(source,source or '-')
                parts.append('노출ID 출처 '+source_label)
        history=self._exposure_history_map().get(key, [])
        if len(history)>1: parts.append('⚠ 노출ID 이력 ' + ' → '.join(history))
        if r['note']: parts.append(f"비고 {r['note']}")
        self.prod_detail_var.set('   ·   '.join(parts))

    def show_product_details(self):
        row=self.selected_product()
        if row is None:
            return messagebox.showinfo(APP_NAME,'상세정보를 볼 옵션 행을 선택하세요.')
        current=getattr(self,'_product_detail_dialog',None)
        if current is not None and current.winfo_exists():
            current.lift(); return
        win=tk.Toplevel(self); self._product_detail_dialog=win
        win.title('상품 옵션 상세'); win.geometry('820x660'); win.minsize(680,620)
        win.transient(self)
        def close():
            win.destroy()
            if self.prod_tree.winfo_exists(): self.prod_tree.focus_set()
        win.protocol('WM_DELETE_WINDOW',close)
        content=ttk.Frame(win,padding=18); content.pack(fill='both',expand=True)
        ttk.Label(content,text=row['product_name'] or row['display_name'] or '상품 옵션',
                  font=('맑은 고딕',12,'bold'),wraplength=740).pack(anchor='w',pady=(0,12))
        table=ttk.Frame(content); table.pack(fill='x')
        fulfillment=str(row['fulfillment_type'] or '판매자배송')
        fields=[('배송 / 판매상태',fulfillment+' / '+str(row['sale_status'] or '미확인')),
                ('모델명',row['model']),('옵션명',row['option_name']),
                ('판매가 / 쿠폰가 / 지도가',' / '.join(money(row[k])+'원' if row[k] is not None else '—' for k in ('sale_price','coupon_price','map_price'))),
                ('판매자배송 재고',money(row['api_stock_quantity']) if fulfillment=='판매자배송' and row['api_stock_quantity'] is not None else '—'),
                ('등록상품 ID',row['registered_product_id']),('노출상품 ID',row['exposed_product_id']),
                ('옵션 ID',row['option_id']),('업체옵션 ID',row['seller_product_item_id']),
                ('상태 확인 시각',row['sale_status_checked_at']),('최근 조회 오류',row['sale_status_error']),
                ('오류 발생 시각',row['sale_status_error_at']),('비고',row['note'])]
        for n,(label,value) in enumerate(fields):
            ttk.Label(table,text=label,foreground='#58677d').grid(row=n,column=0,sticky='w',padx=(0,16),pady=4)
            entry=ttk.Entry(table,font=('맑은 고딕',10))
            entry.insert(0,str(value or '—')); entry.configure(state='readonly')
            entry.grid(row=n,column=1,sticky='ew',pady=4)
        table.columnconfigure(1,weight=1)
        ttk.Label(content,text='값을 선택해 Ctrl+C로 복사할 수 있습니다.',foreground='#66758a').pack(anchor='w',pady=(12,6))
        buttons=ttk.Frame(content); buttons.pack(fill='x',side='bottom',pady=(12,0))
        ttk.Button(buttons,text='전체 정보 복사',command=lambda:self._clipboard_set('\r\n'.join(f'{k}\t{v or "—"}' for k,v in fields))).pack(side='left')
        def history():
            close(); self.show_exposure_history()
        ttk.Button(buttons,text='노출ID 이력',command=history).pack(side='left',padx=6)
        ttk.Button(buttons,text='쿠팡 상품 열기',command=self.open_product_link).pack(side='left')
        ttk.Button(buttons,text='닫기',command=close).pack(side='right')
        win.bind('<Escape>',lambda e:close())
        win.grab_set()
        win.focus_set()

    def product_fields(self):
        return [('fulfillment_type','배송방식','combo',['판매자배송','로켓그로스']),('sale_status','판매상태','combo',['미확인','판매중','품절','판매중지']),('category','분류'),('model','모델명'),('product_name','상품명'),('display_name','노출명'),('registered_product_id','등록상품 ID'),('exposed_product_id','노출상품 ID'),('option_name','옵션명'),('option_id','옵션 ID'),('gift','사은품'),('sale_price','판매가'),('coupon_price','쿠폰적용가'),('map_price','지도가'),('link','링크'),('note','비고')]

    def add_product(self):
        d=FieldDialog(self,'상품 추가',self.product_fields(),{'fulfillment_type':'판매자배송','sale_status':'미확인'}); self.wait_window(d)
        if not d.result: return
        v=d.result
        if not v['model']: return messagebox.showwarning(APP_NAME,'모델명을 입력하세요.')
        sale=num(v['sale_price'],None); mapp=num(v['map_price'],None); diff=(sale-mapp) if sale is not None and mapp is not None else None
        self.db.execute('''INSERT INTO products(category,fulfillment_type,status,sale_status,model,product_name,display_name,registered_product_id,exposed_product_id,option_name,option_id,gift,sale_price,coupon_price,map_price,price_diff,link,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(v['category'],v['fulfillment_type'],'',v['sale_status'],v['model'],v['product_name'],v['display_name'],v['registered_product_id'],v['exposed_product_id'],v['option_name'],v['option_id'],v['gift'],sale,num(v['coupon_price'],None),mapp,diff,v['link'],v['note']))
        self.refresh_products()

    def selected_product(self):
        sel=self.prod_tree.selection() if hasattr(self,'prod_tree') else ()
        if not sel: return None
        iid=str(sel[0])
        if not iid.isdigit(): return None
        return self.db.one('SELECT * FROM products WHERE id=?',(int(iid),))

    def edit_product(self):
        r=self.selected_product()
        if not r: return messagebox.showinfo(APP_NAME,'수정할 옵션 행을 선택하세요.')
        vals={k:r[k] for k, *_ in self.product_fields()}; d=FieldDialog(self,'상품 수정',self.product_fields(),vals); self.wait_window(d)
        if not d.result: return
        v=d.result; sale=num(v['sale_price'],None); mapp=num(v['map_price'],None); diff=(sale-mapp) if sale is not None and mapp is not None else None
        self.db.execute('''UPDATE products SET category=?,fulfillment_type=?,status=?,sale_status=?,model=?,product_name=?,display_name=?,registered_product_id=?,exposed_product_id=?,option_name=?,option_id=?,gift=?,sale_price=?,coupon_price=?,map_price=?,price_diff=?,link=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(v['category'],v['fulfillment_type'],str(r['status'] or ''),v['sale_status'],v['model'],v['product_name'],v['display_name'],v['registered_product_id'],v['exposed_product_id'],v['option_name'],v['option_id'],v['gift'],sale,num(v['coupon_price'],None),mapp,diff,v['link'],v['note'],r['id']))
        if str(v.get('exposed_product_id') or '').strip():
            self._record_exposure(self.db,v.get('option_id',''),r['seller_product_item_id'],v.get('registered_product_id',''),v.get('exposed_product_id',''),v.get('model',''),v.get('option_name',''),'manual',r['id'])
            self._record_product_event(self.db,'exposure_id',r,r['exposed_product_id'],v.get('exposed_product_id',''),'manual')
            self.db.conn.commit()
        self.refresh_products()

    def delete_product(self):
        sel=self.prod_tree.selection() if hasattr(self,'prod_tree') else ()
        ids=sorted({int(str(i)) for i in sel if str(i).isdigit()})
        if not ids:
            return messagebox.showinfo(APP_NAME,'삭제할 옵션 행을 선택하세요.\n묶음 제목 행은 삭제 대상에서 제외됩니다.')
        rows=self.db.query('SELECT id,model,option_name FROM products WHERE id IN ('+','.join('?' for _ in ids)+')',ids)
        if not rows:
            return
        sample=' / '.join(f"{r['model']} {r['option_name'] or ''}".strip() for r in rows[:3])
        more=f" 외 {len(rows)-3}개" if len(rows)>3 else ''
        if not messagebox.askyesno(APP_NAME,f"선택한 {len(rows)}개 옵션 행을 삭제할까요?\n\n{sample}{more}"):
            return
        self.db.conn.executemany('DELETE FROM products WHERE id=?',[(r['id'],) for r in rows])
        self.db.conn.commit()
        self.refresh_products()

    def open_product_link(self):
        r=self.selected_product()
        if not r:
            return messagebox.showinfo(APP_NAME,'옵션 행을 선택하세요.')
        link=self._resolved_product_link(r)
        if link:
            webbrowser.open_new_tab(link)
        else:
            messagebox.showinfo(APP_NAME,'선택한 행에 노출상품 ID/링크가 없습니다.')

    # ---------------- Search ----------------
    def build_search(self):
        self.page_header("상품 검색", "모델명·상품명·등록상품 ID·노출상품 ID·옵션 ID를 통합 검색합니다.")
        bar=ttk.Frame(self.content); bar.pack(fill='x',pady=(0,12)); self.search_q=tk.StringVar(value='HMP33L')
        ent=ttk.Entry(bar,textvariable=self.search_q,width=40,font=('맑은 고딕',12)); ent.pack(side='left',ipady=4); ttk.Button(bar,text='검색',style='Accent.TButton',command=self.run_search).pack(side='left',padx=8); self.search_label=ttk.Label(bar,text=''); self.search_label.pack(side='left',padx=8)
        cols=[('fulfillment','배송'),('sale_status','판매상태'),('model','모델명'),('product','상품명'),('display','노출명'),('regid','등록상품 ID'),('pid','노출상품 ID'),('option','옵션명'),('oid','옵션 ID'),('selleritemid','업체옵션 ID'),('gift','사은품'),('sale','판매가'),('coupon','쿠폰가'),('map','지도가'),('diff','가격차'),('link','링크')]
        self.search_tree=self.make_tree(self.content,cols,{'fulfillment':95,'sale_status':105,'model':115,'product':250,'display':280,'option':190,'link':220,'regid':120,'pid':115,'oid':120,'selleritemid':125}); self.search_tree.bind('<Double-1>',self.search_open_link)
        ent.bind('<Return>',lambda e:self.run_search()); ent.focus_set(); self.run_search()

    def run_search(self):
        q=self.search_q.get().strip(); self.search_tree.delete(*self.search_tree.get_children())
        if not q: self.search_label.config(text='검색어를 입력하세요.'); return
        like=f"%{q}%"; digits=''.join(ch for ch in q if ch.isdigit()); id_term=digits if len(digits)>=6 else q; id_like=f"%{id_term}%"
        sql=("SELECT p.* FROM products p WHERE (COALESCE(p.model,'') LIKE ? OR COALESCE(p.product_name,'') LIKE ? OR COALESCE(p.display_name,'') LIKE ? OR COALESCE(p.option_name,'') LIKE ? OR COALESCE(p.registered_product_id,'') LIKE ? OR COALESCE(p.exposed_product_id,'') LIKE ? OR COALESCE(p.option_id,'') LIKE ? OR COALESCE(p.seller_product_item_id,'') LIKE ?)")
        params=[like,like,like,like,id_like,id_like,id_like,id_like]
        if self.multitap_only_enabled(): sql += ' AND ' + self._multitap_product_clause('p')
        sql += (" ORDER BY CASE WHEN COALESCE(p.option_id,'')=? THEN 0 WHEN COALESCE(p.seller_product_item_id,'')=? THEN 1 WHEN COALESCE(p.registered_product_id,'')=? THEN 2 WHEN COALESCE(p.exposed_product_id,'')=? THEN 3 ELSE 4 END,p.model,p.fulfillment_type,p.sale_status,p.status,p.id")
        params.extend([id_term,id_term,id_term,id_term]); rows=self.db.query(sql,params); self.search_label.config(text=f"검색 결과: {len(rows)}건")
        for r in rows:
            self.search_tree.insert('', 'end', iid=str(r['id']),values=(r['fulfillment_type'] or '판매자배송',r['sale_status'] or '미확인',r['model'],r['product_name'],r['display_name'],r['registered_product_id'],r['exposed_product_id'],r['option_name'],r['option_id'],r['seller_product_item_id'],r['gift'],money(r['sale_price']),money(r['coupon_price']) if r['coupon_price'] else '',money(r['map_price']),money(r['price_diff']),self._resolved_product_link(r)))

    def search_open_link(self,event=None):
        sel=self.search_tree.selection()
        if sel:
            r=self.db.one('SELECT * FROM products WHERE id=?',(int(sel[0]),))
            link=self._resolved_product_link(r) if r else ''
            if link: webbrowser.open_new_tab(link)

    # ---------------- Model master ----------------
    @staticmethod
    def _norm_excel_header(value):
        return ''.join(str(value or '').replace('\n',' ').replace('\r',' ').split()).lower()

    @staticmethod
    def _split_color_model(value):
        text=' '.join(str(value or '').replace('\n',' ').replace('\r',' ').split()).strip()
        if not text:
            return '', ''
        if '/' in text:
            left,right=text.rsplit('/',1)
            model=right.strip(); color=left.strip()
            if model:
                return color,model
        return '',text

    def _sync_option_model_map_from_products(self):
        rows=self.db.query("""SELECT option_id,model FROM products
                              WHERE COALESCE(TRIM(option_id),'')<>''
                                AND COALESCE(TRIM(model),'')<>''
                              ORDER BY id""")
        inserted=updated=skipped=0
        seen=set()
        for r in rows:
            oid=str(r['option_id'] or '').strip(); model=str(r['model'] or '').strip()
            if not oid or not model or oid in seen:
                continue
            seen.add(oid)
            cur=self.db.one('SELECT model,source FROM option_model_map WHERE option_id=?',(oid,))
            if not cur:
                self.db.conn.execute("INSERT INTO option_model_map(option_id,model,source,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",(oid,model,'products')); inserted+=1
            elif str(cur['source'] or '')=='manual':
                skipped+=1
            elif str(cur['model'] or '').strip()!=model:
                self.db.conn.execute("UPDATE option_model_map SET model=?,source='products',updated_at=CURRENT_TIMESTAMP WHERE option_id=?",(model,oid)); updated+=1
            else:
                skipped+=1
        self.db.conn.commit()
        return {'inserted':inserted,'updated':updated,'skipped':skipped}

    def _resolve_model_for_option(self, option_id, db=None):
        db = db or self.db
        oid=str(option_id or '').strip()
        if not oid:
            return ''
        r=db.one('SELECT model FROM option_model_map WHERE option_id=?',(oid,))
        if r and str(r['model'] or '').strip():
            return str(r['model']).strip()
        r=db.one("SELECT model FROM products WHERE option_id=? AND COALESCE(TRIM(model),'')<>'' ORDER BY id LIMIT 1",(oid,))
        if r and str(r['model'] or '').strip():
            model=str(r['model']).strip()
            db.conn.execute("INSERT OR REPLACE INTO option_model_map(option_id,model,source,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",(oid,model,'products'))
            db.conn.commit()
            return model
        return ''

    @staticmethod
    def _model_match_norm(text):
        s=str(text or '').lower()
        generic=(
            '현대일렉트릭','현대','국산','멀티탭','멀티','콘센트','씨드잇','벨크로타이','밸크로 타이',
            '벨크로','밸크로','가정용','사무용','안전한','전선형','2800w','3500w','16a','10a','20a',
            '1개','1세트','세트','안전커버','포트','스위치','전기'
        )
        for token in generic:
            s=s.replace(token,' ')
        s=re.sub(r'[^0-9a-z가-힣.]+',' ',s)
        return ' '.join(s.split())

    @staticmethod
    def _model_match_spec(text):
        t=str(text or '').lower()
        meters=re.findall(r'(?<![\d.])(0\.5|1\.5|2\.5|10|15|20|30|50|[123578])\s*m\b',t)
        length=float(meters[-1]) if meters else None
        outlets=re.findall(r'(\d+)\s*구',t)
        outlet=int(outlets[-1]) if outlets else None
        amps=re.findall(r'(10|16|20)\s*a\b',t)
        amp=int(amps[-1]) if amps else None
        feature_names=('누전','배선','과부하','개별','usb','고용량','연장','방수','방진','서버','오브제','절전','하이탭','접지','일반')
        features={x for x in feature_names if x in t}
        color=None
        for c in ('리얼리티 레드','다크그레이','스카이블루','화이트','네이비','베이지','블랙','퍼플','옐로우','실버','그레이','골드','핑크','카키','민트','블루','레드'):
            if c.lower() in t:
                color=c
                break
        return (length,outlet,amp,features,color)

    @staticmethod
    def _model_match_compatible(target_spec, candidate_spec):
        l1,o1,a1,f1,c1=target_spec
        l2,o2,a2,f2,c2=candidate_spec
        if l1 is not None and (l2 is None or abs(l1-l2)>0.01):
            return False
        if o1 is not None and (o2 is None or o1!=o2):
            return False
        if a1 is not None and a2 is not None and a1!=a2:
            return False
        if c1 and c2 and c1!=c2:
            return False
        for feat in ('누전','배선','과부하','개별','usb','고용량','방수','방진','서버','오브제','하이탭','접지'):
            if feat in f1 and feat not in f2:
                return False
        if '누전' in f1 and '배선' in f2:
            return False
        if '배선' in f1 and '누전' in f2:
            return False
        if '절전' in f1 and '과부하' not in f1 and '과부하' in f2:
            return False
        return True

    def _build_model_match_examples(self, db=None):
        db=db or self.db
        examples=[]
        for r in db.query("""SELECT model,product_name,option_name,display_name
                             FROM products WHERE COALESCE(TRIM(model),'')<>''"""):
            raw=' '.join(str(r[k] or '') for k in ('product_name','option_name','display_name'))
            examples.append({'model':str(r['model']).strip(),'norm':self._model_match_norm(raw),
                             'spec':self._model_match_spec(raw),'source':'product'})
        for r in db.query("SELECT model,product_name,color,category,source_sheet FROM model_master"):
            compare=' '.join(str(r[k] or '') for k in ('product_name','color','category','source_sheet'))
            spec_text=' '.join(str(r[k] or '') for k in ('product_name','color'))
            examples.append({'model':str(r['model'] or '').strip(),'norm':self._model_match_norm(compare),
                             'spec':self._model_match_spec(spec_text),'source':'master'})
        return [x for x in examples if x['model'] and x['norm']]

    def _infer_model_from_text(self, option_name='', product_name='', display_name='', db=None, examples=None):
        """Conservative model inference. Returns (model, best_score, runner_up_score)."""
        db=db or self.db
        target=' '.join(str(x or '') for x in (option_name,product_name,display_name)).strip()
        if not target:
            return '',0.0,0.0
        target_norm=self._model_match_norm(target)
        target_spec=self._model_match_spec(target)
        target_tokens=set(target_norm.split())
        if not target_norm:
            return '',0.0,0.0
        examples=examples or self._build_model_match_examples(db)
        best_by_model={}
        for ex in examples:
            if not self._model_match_compatible(target_spec,ex['spec']):
                continue
            cand_norm=ex['norm']; cand_tokens=set(cand_norm.split())
            seq=difflib.SequenceMatcher(None,target_norm,cand_norm).ratio()
            jac=len(target_tokens & cand_tokens)/max(1,len(target_tokens | cand_tokens))
            l1,o1,a1,f1,c1=target_spec; l2,o2,a2,f2,c2=ex['spec']
            score=0.45*seq + 0.35*jac
            if l1 is not None and l2 is not None: score+=0.22
            if o1 is not None and o2 is not None: score+=0.16
            if a1 is not None and a2 is not None: score+=0.07
            if c1 and c2: score+=0.05
            score+=0.04*len((f1-{'일반','절전','연장'}) & (f2-{'일반','절전','연장'}))
            if ex['source']=='product': score+=0.07
            if ex['model'] not in best_by_model or score>best_by_model[ex['model']]:
                best_by_model[ex['model']]=score
        if not best_by_model:
            return '',0.0,0.0
        ranked=sorted(best_by_model.items(),key=lambda x:x[1],reverse=True)
        model,best=ranked[0]; second=ranked[1][1] if len(ranked)>1 else 0.0
        if best < 0.76:
            return '',best,second
        if len(ranked)>1 and best-second < 0.06 and best < 1.15:
            return '',best,second
        return model,best,second

    def _store_auto_model_mapping(self, db, option_id, model, source='auto-master'):
        oid=str(option_id or '').strip(); model=str(model or '').strip()
        if not oid or not model:
            return False
        cur=db.one('SELECT model,source FROM option_model_map WHERE option_id=?',(oid,))
        if cur and str(cur['source'] or '')=='manual':
            return str(cur['model'] or '').strip()==model
        db.conn.execute("""INSERT INTO option_model_map(option_id,model,source,updated_at)
                           VALUES (?,?,?,CURRENT_TIMESTAMP)
                           ON CONFLICT(option_id) DO UPDATE SET model=excluded.model,source=excluded.source,updated_at=CURRENT_TIMESTAMP""",
                        (oid,model,source))
        db.conn.execute("UPDATE products SET model=?,updated_at=CURRENT_TIMESTAMP WHERE option_id=? AND COALESCE(TRIM(model),'')=''",(model,oid))
        return True

    def _auto_link_missing_models(self, db=None, examples=None):
        db=db or self.db
        examples=examples or self._build_model_match_examples(db)
        rows=db.query("""SELECT id,option_id,option_name,product_name,display_name
                         FROM products WHERE COALESCE(TRIM(model),'')=''
                           AND COALESCE(TRIM(option_id),'')<>'' ORDER BY id""")
        linked=ambiguous=0
        for r in rows:
            oid=str(r['option_id'] or '').strip()
            existing=self._resolve_model_for_option(oid,db)
            if existing:
                db.conn.execute("UPDATE products SET model=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND COALESCE(TRIM(model),'')=''",(existing,r['id']))
                linked+=1
                continue
            model,best,second=self._infer_model_from_text(r['option_name'],r['product_name'],r['display_name'],db,examples)
            if model:
                self._store_auto_model_mapping(db,oid,model,'auto-master')
                linked+=1
                LOGGER.info('모델 자동연결 optionId=%s model=%s score=%.3f runner=%.3f',oid,model,best,second)
            elif best:
                ambiguous+=1
        db.conn.commit()
        return {'linked':linked,'ambiguous':ambiguous,'total':len(rows)}

    def _auto_link_pending_models(self, db=None, examples=None):
        db=db or self.db
        examples=examples or self._build_model_match_examples(db)
        rows=db.query("""SELECT option_id,MAX(option_name) option_name,MAX(product_name) product_name
                         FROM sales_pending GROUP BY option_id ORDER BY option_id""")
        linked=0
        for r in rows:
            oid=str(r['option_id'] or '').strip()
            if self._resolve_model_for_option(oid,db):
                continue
            model,best,second=self._infer_model_from_text(r['option_name'],r['product_name'],'',db,examples)
            if model:
                self._store_auto_model_mapping(db,oid,model,'auto-master')
                linked+=1
                LOGGER.info('대기매출 모델 자동연결 optionId=%s model=%s score=%.3f runner=%.3f',oid,model,best,second)
        db.conn.commit()
        return linked

    def _auto_repair_model_links(self, db=None):
        db=db or self.db
        examples=self._build_model_match_examples(db)
        products=self._auto_link_missing_models(db,examples)
        pending_linked=self._auto_link_pending_models(db,examples)
        moved=0
        if db is self.db:
            m=self._materialize_pending_sales()
            moved=int(m.get('inserted',0))+int(m.get('updated',0))
        return {'linked':products.get('linked',0),'ambiguous':products.get('ambiguous',0),
                'pendingLinked':pending_linked,'salesMoved':moved}

    def _model_master_header(self, ws):
        for row_no,row in enumerate(ws.iter_rows(min_row=1,max_row=12,values_only=True),start=1):
            headers=[self._excel_text(v) for v in row]
            norm=[self._norm_excel_header(v) for v in headers]
            if any('색상/모델명' in h for h in norm) and any(h=='품명' or '품명' in h for h in norm):
                return row_no,headers
        return None,None

    def _import_model_master_sheet(self, ws, source_file):
        header_row,headers=self._model_master_header(ws)
        if not header_row:
            return {'inserted':0,'updated':0,'skipped':0,'rows':0}
        norm=[self._norm_excel_header(h) for h in headers]
        def find_col(*needles):
            for i,h in enumerate(norm):
                if all(n in h for n in needles):
                    return i
            return None
        ci={
            'status':find_col('상태구분'), 'category':find_col('분류'), 'product':find_col('품명'),
            'color_model':find_col('색상/모델명'), 'package':find_col('포장타입'), 'pack_qty':find_col('입수량'),
            'supply':find_col('공급가'), 'size':find_col('사이즈'), 'kc':find_col('전기안전인증'),
        }
        price_cols=[i for i,h in enumerate(norm) if i!=(ci['supply'] if ci['supply'] is not None else -1) and ('등록가' in h or '쿠폰미적용' in h or '쿠폰적용제외' in h or '쿠폰적용' in h)]
        ci['coupang_price']=price_cols[0] if price_cols else None
        ci['coupon_price']=price_cols[1] if len(price_cols)>1 else None
        carry={k:'' for k in ('status','category','product','package','pack_qty','supply','coupang_price','coupon_price','size','kc')}
        inserted=updated=skipped=seen_rows=0
        for row in ws.iter_rows(min_row=header_row+1,values_only=True):
            def raw(key):
                i=ci.get(key); return row[i] if i is not None and i<len(row) else None
            raw_cm=raw('color_model')
            color,model=self._split_color_model(raw_cm)
            if not model:
                continue
            if model.lower() in ('색상 / 모델명','색상/모델명','모델명'):
                continue
            seen_rows+=1
            for key in carry:
                val=raw(key)
                if val not in (None,''):
                    carry[key]=val
            status=self._excel_text(carry['status']); category=self._excel_text(carry['category']); product=self._excel_text(carry['product'])
            package=self._excel_text(carry['package']); size_mm=self._excel_text(carry['size']); kc=self._excel_text(carry['kc'])
            pack_qty=self._excel_number(carry['pack_qty'],None); supply=self._excel_number(carry['supply'],None)
            coupang_price=self._excel_number(carry['coupang_price'],None); coupon_price=self._excel_number(carry['coupon_price'],None)
            current=self.db.one('SELECT * FROM model_master WHERE model=?',(model,))
            values=(ws.title,status,category,product,color,package,pack_qty,supply,coupang_price,coupon_price,size_mm,kc,source_file,model)
            if current:
                changed=(str(current['source_sheet'] or '')!=str(ws.title) or str(current['status_note'] or '')!=status or
                         str(current['category'] or '')!=category or str(current['product_name'] or '')!=product or
                         str(current['color'] or '')!=color or str(current['package_type'] or '')!=package or
                         str(current['pack_qty'] if current['pack_qty'] is not None else '')!=str(pack_qty if pack_qty is not None else '') or
                         str(current['supply_price'] if current['supply_price'] is not None else '')!=str(supply if supply is not None else '') or
                         str(current['coupang_price'] if current['coupang_price'] is not None else '')!=str(coupang_price if coupang_price is not None else '') or
                         str(current['coupon_price'] if current['coupon_price'] is not None else '')!=str(coupon_price if coupon_price is not None else '') or
                         str(current['size_mm'] or '')!=size_mm or str(current['kc'] or '')!=kc or str(current['source_file'] or '')!=source_file)
                if changed:
                    self.db.conn.execute("""UPDATE model_master SET source_sheet=?,status_note=?,category=?,product_name=?,color=?,package_type=?,pack_qty=?,supply_price=?,coupang_price=?,coupon_price=?,size_mm=?,kc=?,source_file=?,updated_at=CURRENT_TIMESTAMP WHERE model=?""",values); updated+=1
                else:
                    skipped+=1
            else:
                self.db.conn.execute("""INSERT INTO model_master(source_sheet,status_note,category,product_name,color,package_type,pack_qty,supply_price,coupang_price,coupon_price,size_mm,kc,source_file,model,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",values); inserted+=1
        return {'inserted':inserted,'updated':updated,'skipped':skipped,'rows':seen_rows}

    def import_model_master_excel(self):
        path=filedialog.askopenfilename(title='현대 단가표 가져오기',filetypes=[('Excel 통합문서','*.xlsx *.xlsm'),('Excel','*.xlsx'),('모든 파일','*.*')])
        if not path:
            return
        try:
            from openpyxl import load_workbook
        except Exception:
            return messagebox.showerror(APP_NAME,'Excel 가져오기에 필요한 openpyxl 모듈을 찾을 수 없습니다.')
        try:
            wb=load_workbook(path,read_only=True,data_only=True)
        except Exception as e:
            LOGGER.exception('모델 마스터 Excel 열기 실패: %s',path)
            return messagebox.showerror(APP_NAME,f'Excel 파일을 열지 못했습니다.\n\n{e}')
        backup=self._excel_backup_before_import()
        total={'inserted':0,'updated':0,'skipped':0,'rows':0,'sheets':0}
        try:
            self.db.conn.execute('BEGIN')
            for ws in wb.worksheets:
                r=self._import_model_master_sheet(ws,Path(path).name)
                if r['rows']:
                    total['sheets']+=1
                    for k in ('inserted','updated','skipped','rows'): total[k]+=r[k]
            self.db.conn.commit()
            mapping=self._sync_option_model_map_from_products()
            repair=self._auto_repair_model_links(self.db)
        except Exception as e:
            self.db.conn.rollback(); LOGGER.exception('모델 마스터 가져오기 실패: %s',path)
            return messagebox.showerror(APP_NAME,f'모델 마스터 가져오기 중 오류가 발생했습니다.\n\n{e}')
        finally:
            try: wb.close()
            except Exception: pass
        self.db.set_setting('model_master_last_import',datetime.now().isoformat(timespec='seconds'))
        self.db.set_setting('model_master_source_file',Path(path).name)
        LOGGER.info('모델 마스터 가져오기 완료 file=%s result=%s mapping=%s repair=%s',path,total,mapping,repair)
        self.refresh_model_master()
        messagebox.showinfo(APP_NAME,
            f"모델 마스터 가져오기 완료\n\n시트 {total['sheets']}개 / 모델 {total['rows']}행\n추가 {total['inserted']} / 갱신 {total['updated']} / 동일 {total['skipped']}\n옵션ID 매핑 {mapping['inserted']+mapping['updated']}건 반영\n빈 모델 자동연결 {repair.get('linked',0)}건 / 대기매출 반영 {repair.get('salesMoved',0)}건\n\n자동 백업: {backup.name}")

    def build_master(self):
        self.page_header('마스터 데이터','모델 기준정보·옵션 연결과 마진 계산에 쓰는 풀필먼트 요금을 관리합니다.')
        self.master_tabs=ttk.Notebook(self.content); self.master_tabs.pack(fill='both',expand=True)
        self.master_models_tab=ttk.Frame(self.master_tabs,padding=8)
        self.master_fees_tab=ttk.Frame(self.master_tabs,padding=8)
        self.master_tabs.add(self.master_models_tab,text='  모델 · 옵션 매핑  ')
        self.master_tabs.add(self.master_fees_tab,text='  풀필먼트 요금  ')
        self._build_model_master(self.master_models_tab)
        self._build_fulfillment_rates(self.master_fees_tab)

    def clear_model_search(self):
        self.model_q.set('')
        self.refresh_model_master()

    def _build_model_master(self, parent):
        bar=ttk.Frame(parent); bar.pack(fill='x',pady=(0,8))
        ttk.Button(bar,text='현대 단가표 가져오기',style='Accent.TButton',command=self.import_model_master_excel).pack(side='left')
        ttk.Button(bar,text='빈 모델 자동연결',command=self.auto_link_missing_models_ui).pack(side='left',padx=(6,0))
        ttk.Button(bar,text='옵션ID 매핑 재생성',command=self.refresh_option_model_map).pack(side='left',padx=6)
        self.model_q=tk.StringVar()
        ttk.Button(bar,text='F5 검색',command=self.open_search_dialog).pack(side='right')
        ttk.Button(bar,text='검색 해제',command=self.clear_model_search).pack(side='right',padx=6)
        self.model_summary=ttk.Label(parent,text='',foreground='#555'); self.model_summary.pack(fill='x',pady=(0,8))
        self.model_summary.bind('<Configure>',lambda e:self.model_summary.configure(wraplength=max(200,e.width)))
        self.model_panes=ttk.Panedwindow(parent,orient='vertical'); self.model_panes.pack(fill='both',expand=True)

        master_box=ttk.LabelFrame(self.model_panes,text='모델 기준정보',padding=8); self.model_panes.add(master_box,weight=3)
        cols=[('model','모델명'),('category','분류'),('product','품명'),('color','색상'),('package','포장'),('pack','입수량'),('supply','공급가'),('price','쿠팡 등록가'),('sheet','원본 시트')]
        self.model_tree=self.make_tree(master_box,cols,{'model':120,'category':110,'product':300,'color':100,'package':95,'pack':70,'supply':90,'price':100,'sheet':110},height=7)

        unmatched_box=ttk.LabelFrame(self.model_panes,text='미매칭 옵션 ID · 모델 연결',padding=8); self.model_panes.add(unmatched_box,weight=2)
        ub=ttk.Frame(unmatched_box); ub.pack(fill='x',pady=(0,6))
        ttk.Button(ub,text='선택 모델 지정',style='Accent.TButton',command=self.assign_unmatched_model).pack(side='left')
        ttk.Label(ub,text='한 번 지정하면 이후 같은 옵션 ID는 자동으로 같은 모델명에 매칭됩니다.',foreground='#666').pack(side='left',padx=10)
        ucols=[('oid','옵션 ID'),('option','옵션명'),('product','상품명'),('reg','등록상품ID'),('method','판매방식'),('first','최초확인'),('last','최근확인')]
        self.unmatched_tree=self.make_tree(unmatched_box,ucols,{'oid':125,'option':260,'product':300,'reg':120,'method':90,'first':135,'last':135},height=4)
        self.unmatched_tree.bind('<Double-1>',lambda e:self.assign_unmatched_model())
        self.refresh_model_master()

    def auto_link_missing_models_ui(self):
        try:
            result=self._auto_repair_model_links(self.db)
        except Exception as e:
            LOGGER.exception('빈 모델 자동연결 실패')
            return messagebox.showerror(APP_NAME,f'모델 자동연결 중 오류가 발생했습니다.\n\n{e}')
        self.refresh_model_master()
        try:
            if hasattr(self,'prod_tree') and self.prod_tree.winfo_exists(): self.refresh_products()
            if hasattr(self,'sales_tree') and self.sales_tree.winfo_exists(): self.refresh_sales()
        except Exception:
            pass
        messagebox.showinfo(APP_NAME,
            f"모델 자동연결 완료\n\n상품 연결 {result.get('linked',0)}건\n대기매출 옵션 연결 {result.get('pendingLinked',0)}건\n대기매출 반영 {result.get('salesMoved',0)}건\n\n애매한 항목은 오매칭 방지를 위해 자동 연결하지 않습니다.")

    def refresh_option_model_map(self):
        r=self._sync_option_model_map_from_products()
        self.refresh_model_master()
        messagebox.showinfo(APP_NAME,f"옵션ID → 모델명 매핑을 다시 만들었습니다.\n추가 {r['inserted']} / 갱신 {r['updated']} / 유지 {r['skipped']}")

    def refresh_model_master(self):
        self._sync_option_model_map_from_products()
        q='%' + (self.model_q.get().strip() if hasattr(self,'model_q') else '') + '%'
        if hasattr(self,'model_tree') and self.model_tree.winfo_exists():
            self.model_tree.delete(*self.model_tree.get_children())
            rows=self.db.query("""SELECT * FROM model_master WHERE model LIKE ? OR COALESCE(product_name,'') LIKE ? OR COALESCE(category,'') LIKE ? ORDER BY category COLLATE NOCASE, model COLLATE NOCASE""",(q,q,q))
            for r in rows:
                self.model_tree.insert('', 'end', iid=str(r['model']), values=(r['model'],r['category'],r['product_name'],r['color'],r['package_type'],money(r['pack_qty']) if r['pack_qty'] is not None else '',money(r['supply_price']) if r['supply_price'] is not None else '',money(r['coupang_price']) if r['coupang_price'] is not None else '',r['source_sheet']))
        if hasattr(self,'unmatched_tree') and self.unmatched_tree.winfo_exists():
            self.unmatched_tree.delete(*self.unmatched_tree.get_children())
            for r in self.db.query('SELECT * FROM unmatched_options ORDER BY last_seen DESC, option_id'):
                self.unmatched_tree.insert('', 'end', iid=str(r['option_id']), values=(r['option_id'],r['option_name'],r['product_name'],r['registered_product_id'],r['method'],r['first_seen'],r['last_seen']))
        master=self.db.one('SELECT COUNT(*) n FROM model_master')['n']; mappings=self.db.one('SELECT COUNT(*) n FROM option_model_map')['n']; unmatched=self.db.one('SELECT COUNT(*) n FROM unmatched_options')['n']; pending=self.db.one('SELECT COUNT(*) n FROM sales_pending')['n']
        missing=self.db.one("""SELECT COUNT(DISTINCT p.model) n FROM products p LEFT JOIN model_master m ON m.model=p.model WHERE COALESCE(TRIM(p.model),'')<>'' AND m.model IS NULL""")['n']
        if hasattr(self,'model_summary') and self.model_summary.winfo_exists():
            query=self.model_q.get().strip()
            search_label=(' · 검색: '+query[:35]+('…' if len(query)>35 else '')) if query else ''
            self.model_summary.config(text=f'마스터 {master} · 옵션매핑 {mappings} · 미매칭 {unmatched} · 대기매출 {pending} · 단가표 미등록 모델 {missing}'+search_label)

    def _upsert_sale_from_mapping(self, sale_date, model, method, revenue, orders, qty, option_id='', option_name='', product_name='', registered_product_id='', source=''):
        oid=str(option_id or '').strip(); method=str(method or '로켓그로스').strip() or '로켓그로스'
        if not str(source).startswith(rocket_sales.SOURCE_PREFIX):
            rocket_sales.prefer_external_sale(self.db.conn, sale_date, model, method, oid)
        existing=None
        if oid:
            existing=self.db.one("SELECT * FROM sales WHERE sale_date=? AND option_id=? AND COALESCE(method,'')=? ORDER BY id LIMIT 1",(sale_date,oid,method))
        if existing:
            changed=(str(existing['model'] or '')!=str(model or '') or float(existing['revenue'] or 0)!=float(revenue or 0) or float(existing['orders'] or 0)!=float(orders or 0) or float(existing['quantity'] or 0)!=float(qty or 0) or str(existing['option_name'] or '')!=str(option_name or '') or str(existing['product_name'] or '')!=str(product_name or '') or str(existing['registered_product_id'] or '')!=str(registered_product_id or ''))
            if changed:
                self.db.conn.execute("""UPDATE sales SET model=?,method=?,revenue=?,orders=?,quantity=?,option_id=?,option_name=?,product_name=?,registered_product_id=?,source=? WHERE id=?""",(model,method,revenue,orders,qty,oid,option_name,product_name,registered_product_id,source,existing['id']))
                return 'updated'
            return 'skipped'
        self.db.conn.execute("""INSERT INTO sales(sale_date,model,method,revenue,orders,quantity,option_id,option_name,product_name,registered_product_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",(sale_date,model,method,revenue,orders,qty,oid,option_name,product_name,registered_product_id,source))
        return 'inserted'

    def _materialize_pending_sales(self, option_ids=None):
        params=[]; where=''
        if option_ids:
            ids=[str(x).strip() for x in option_ids if str(x).strip()]
            if not ids: return {'inserted':0,'updated':0,'skipped':0,'remaining':0}
            where=' WHERE option_id IN ('+','.join('?' for _ in ids)+')'; params=ids
        rows=self.db.query('SELECT * FROM sales_pending'+where+' ORDER BY sale_date,id',params)
        counts={'inserted':0,'updated':0,'skipped':0}
        done=[]
        for r in rows:
            model=self._resolve_model_for_option(r['option_id'])
            if not model: continue
            state=self._upsert_sale_from_mapping(r['sale_date'],model,r['method'],r['revenue'],r['orders'],r['quantity'],r['option_id'],r['option_name'],r['product_name'],r['registered_product_id'],'coupang-insights')
            counts[state]+=1; done.append(r['id'])
            self.db.conn.execute('DELETE FROM unmatched_options WHERE option_id=?',(r['option_id'],))
        if done:
            self.db.conn.executemany('DELETE FROM sales_pending WHERE id=?',[(i,) for i in done])
        api_linked = rocket_sales.rebuild_sales(self.db.conn, self.db.setting('api_vendor_id','').strip(), option_ids=option_ids)
        counts['api_updated'] = api_linked['updated']
        self.db.conn.commit()
        counts['remaining']=self.db.one('SELECT COUNT(*) n FROM sales_pending')['n']
        return counts

    def assign_unmatched_model(self):
        if not hasattr(self,'unmatched_tree'): return
        sel=self.unmatched_tree.selection()
        if not sel:
            return messagebox.showinfo(APP_NAME,'모델명을 지정할 미매칭 옵션을 선택하세요.')
        models=self.db.distinct_models()
        d=FieldDialog(self,'미매칭 옵션 모델 지정',[('model','모델명','combo',models)],{'model':''}); self.wait_window(d)
        if not d.result or not d.result.get('model'):
            return
        model=d.result['model'].strip(); ids=[str(i) for i in sel]
        self.db.conn.executemany("INSERT OR REPLACE INTO option_model_map(option_id,model,source,updated_at) VALUES (?,?,'manual',CURRENT_TIMESTAMP)",[(oid,model) for oid in ids])
        self.db.conn.executemany("UPDATE products SET model=?,updated_at=CURRENT_TIMESTAMP WHERE option_id=? AND COALESCE(TRIM(model),'')=''",[(model,oid) for oid in ids])
        self.db.conn.commit()
        moved=self._materialize_pending_sales(ids)
        self.refresh_model_master()
        try:
            if hasattr(self,'sales_tree') and self.sales_tree.winfo_exists(): self.refresh_sales()
        except Exception: pass
        messagebox.showinfo(APP_NAME,f"{len(ids)}개 옵션을 {model}에 연결했습니다.\n대기 매출 반영: 추가 {moved['inserted']} / 갱신 {moved['updated']}\nAPI 매출 모델 연결: {moved.get('api_updated',0)}건")

    def import_coupang_sales_excel(self):
        path=filedialog.askopenfilename(title='쿠팡 일일매출 파일 가져오기',filetypes=[('쿠팡 Excel','*.xlsx'),('Excel','*.xlsx *.xlsm'),('모든 파일','*.*')])
        if not path: return
        d=FieldDialog(self,'쿠팡 일일매출 가져오기',[('sale_date','매출 기준일 (YYYY-MM-DD)')],{'sale_date':(date.today()-timedelta(days=1)).isoformat()}); self.wait_window(d)
        if not d.result: return
        sale_date=d.result.get('sale_date','').strip()
        try: datetime.strptime(sale_date,'%Y-%m-%d')
        except Exception: return messagebox.showwarning(APP_NAME,'매출 기준일을 YYYY-MM-DD 형식으로 입력하세요.')
        legacy_count=self.db.one("SELECT COUNT(*) n FROM sales WHERE sale_date=? AND COALESCE(TRIM(option_id),'')=''",(sale_date,))['n']
        if legacy_count:
            if not messagebox.askyesno(APP_NAME,f'{sale_date}에 기존 방식으로 저장된 매출 {legacy_count}건이 있습니다.\n이 날짜를 다시 가져오면 기존 합계와 중복 집계될 수 있습니다.\n\n그래도 계속할까요?'):
                return
        try:
            from openpyxl import load_workbook
        except Exception:
            return messagebox.showerror(APP_NAME,'Excel 가져오기에 필요한 openpyxl 모듈을 찾을 수 없습니다.')
        try:
            wb=load_workbook(path,read_only=True,data_only=True)
        except Exception as e:
            return messagebox.showerror(APP_NAME,f'쿠팡 매출 파일을 열지 못했습니다.\n\n{e}')
        self._sync_option_model_map_from_products()
        self._auto_repair_model_links(self.db)
        result=None
        try:
            for ws in wb.worksheets:
                r=self._import_coupang_sales_sheet(ws,sale_date,Path(path).name)
                if r is not None:
                    result=r; break
        finally:
            try: wb.close()
            except Exception: pass
        if result is None:
            return messagebox.showwarning(APP_NAME,'쿠팡 일일매출 형식을 찾지 못했습니다.\n필수 컬럼: 옵션 ID / 옵션명 / 판매방식 / 매출(원) / 주문 / 판매량')
        self.db.conn.commit()
        self.db.set_setting('sales_insights_last_import',datetime.now().isoformat(timespec='seconds'))
        self.db.set_setting('sales_insights_last_date',sale_date)
        try: self.refresh_sales()
        except Exception: pass
        try:
            if hasattr(self,'model_tree') and self.model_tree.winfo_exists(): self.refresh_model_master()
        except Exception: pass
        messagebox.showinfo(APP_NAME,
            f"쿠팡 일일매출 가져오기 완료 ({sale_date})\n\n추가 {result['inserted']} / 갱신 {result['updated']} / 동일 {result['skipped']}\n모델 자동연결 {result.get('autoMatched',0)} / 0매출 제외 {result['zero']}\n미매칭 옵션 {result['unmatched']}개 → 마스터 데이터의 '미매칭 옵션'에 저장\n\n애매한 항목만 미매칭으로 남기며, 모델명 한 번 지정하면 이후 자동 연결됩니다.")

    def _import_coupang_sales_sheet(self, ws, sale_date, source_file):
        rows=ws.iter_rows(values_only=True)
        try: header=[self._excel_text(v) for v in next(rows)]
        except StopIteration: return None
        norm=[self._norm_excel_header(h) for h in header]
        def find(*names):
            for name in names:
                n=self._norm_excel_header(name)
                for i,h in enumerate(norm):
                    if h==n or h.startswith(n): return i
            return None
        ci_oid=find('옵션 ID','옵션ID'); ci_option=find('옵션명'); ci_product=find('상품명'); ci_reg=find('등록상품ID','등록상품 ID'); ci_method=find('판매방식'); ci_rev=find('매출(원)','매출'); ci_orders=find('주문'); ci_qty=find('판매량')
        if None in (ci_oid,ci_option,ci_method,ci_rev,ci_orders,ci_qty):
            return None
        counts={'inserted':0,'updated':0,'skipped':0,'unmatched':0,'autoMatched':0,'zero':0}
        unmatched_ids=set()
        for row in rows:
            def value(i): return row[i] if i is not None and i<len(row) else None
            oid=self._excel_text(value(ci_oid)); option_name=self._excel_text(value(ci_option)); product_name=self._excel_text(value(ci_product)); regid=self._excel_text(value(ci_reg)); method=self._excel_text(value(ci_method)) or '판매자배송'
            revenue=self._excel_number(value(ci_rev),0) or 0; orders=self._excel_number(value(ci_orders),0) or 0; qty=self._excel_number(value(ci_qty),0) or 0
            if not oid: continue
            existing_api = self.db.one("SELECT 1 FROM sales WHERE sale_date=? AND option_id=? AND method=? AND source LIKE ? LIMIT 1", (sale_date,oid,method,rocket_sales.SOURCE_PREFIX+'%'))
            if abs(float(revenue))+abs(float(orders))+abs(float(qty))==0 and not existing_api:
                counts['zero']+=1; continue
            rocket_sales.prefer_external_sale(self.db.conn,sale_date,'',method,oid)
            model=self._resolve_model_for_option(oid)
            if not model:
                model,best,second=self._infer_model_from_text(option_name,product_name,'',self.db)
                if model:
                    self._store_auto_model_mapping(self.db,oid,model,'auto-master')
                    counts['autoMatched']+=1
                    LOGGER.info('매출파일 모델 자동연결 optionId=%s model=%s score=%.3f runner=%.3f',oid,model,best,second)
            if model:
                state=self._upsert_sale_from_mapping(sale_date,model,method,revenue,orders,qty,oid,option_name,product_name,regid,'coupang-insights')
                counts[state]+=1
                self.db.conn.execute('DELETE FROM unmatched_options WHERE option_id=?',(oid,))
                self.db.conn.execute('DELETE FROM sales_pending WHERE sale_date=? AND option_id=? AND COALESCE(method,\'\')=?',(sale_date,oid,method))
            else:
                unmatched_ids.add(oid)
                self.db.conn.execute("""INSERT INTO unmatched_options(option_id,option_name,product_name,registered_product_id,method,first_seen,last_seen) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                                      ON CONFLICT(option_id) DO UPDATE SET option_name=excluded.option_name,product_name=excluded.product_name,registered_product_id=excluded.registered_product_id,method=excluded.method,last_seen=CURRENT_TIMESTAMP""",(oid,option_name,product_name,regid,method))
                self.db.conn.execute("""INSERT INTO sales_pending(sale_date,option_id,option_name,product_name,registered_product_id,method,revenue,orders,quantity,source_file) VALUES (?,?,?,?,?,?,?,?,?,?)
                                      ON CONFLICT(sale_date,option_id,method) DO UPDATE SET option_name=excluded.option_name,product_name=excluded.product_name,registered_product_id=excluded.registered_product_id,revenue=excluded.revenue,orders=excluded.orders,quantity=excluded.quantity,source_file=excluded.source_file""",(sale_date,oid,option_name,product_name,regid,method,revenue,orders,qty,source_file))
        counts['unmatched']=len(unmatched_ids)
        return counts

    # ---------------- Gross margin ----------------
    @staticmethod
    def _normalize_gross_size(value):
        raw=str(value or '').strip().replace(' ','')
        return {
            '극소형':'극소형','소형':'소형','중형':'중형',
            '대형1':'대형 1','대형2':'대형 2','특대형':'특대형',
        }.get(raw,'')

    def _gross_rate_for(self, size_type, sale_price):
        size=self._normalize_gross_size(size_type)
        sale=num(sale_price,0)
        if not size or sale<=0:
            return None
        return self.db.one('''SELECT * FROM fulfillment_rates
                              WHERE size_type=? AND min_price<=?
                                AND (max_price IS NULL OR ?<max_price)
                              ORDER BY min_price DESC LIMIT 1''',(size,sale,sale))

    def _infer_gross_size_from_legacy_ref(self, ref):
        """Recover a size only when the old fee pair maps to exactly one size."""
        if not ref:
            return ''
        direct=self._normalize_gross_size(ref['size_type'])
        if direct:
            return direct
        sale=num(ref['sale_price'],0); old_in=num(ref['inout_fee'],None); old_ship=num(ref['shipping_fee'],None)
        if sale<=0 or old_in is None or old_ship is None:
            return ''
        candidates=[]
        rows=self.db.query('''SELECT * FROM fulfillment_rates
                              WHERE min_price<=? AND (max_price IS NULL OR ?<max_price)
                              ORDER BY id''',(sale,sale))
        for r in rows:
            inbound=[x for x in (r['inout_current'],r['inout_regular']) if x is not None]
            shipping=[x for x in (r['shipping_current'],r['shipping_regular']) if x is not None]
            if any(abs(old_in-float(x))<0.01 for x in inbound) and any(abs(old_ship-float(x))<0.01 for x in shipping):
                candidates.append(self._normalize_gross_size(r['size_type']))
        candidates=[x for x in dict.fromkeys(candidates) if x]
        if len(candidates)==1:
            size=candidates[0]
            try:
                self.db.conn.execute("INSERT OR IGNORE INTO gross_margin_profiles(model,size_type,source,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
                                     (str(ref['model'] or '').strip(),size,'legacy-fee-infer'))
                self.db.conn.commit()
            except Exception:
                LOGGER.exception('그로스 사이즈형 자동복구 저장 실패 model=%s',ref['model'])
            return size
        return ''

    def _gross_margin_data(self):
        sql="SELECT p.* FROM products p WHERE COALESCE(NULLIF(p.fulfillment_type,''),'판매자배송')='로켓그로스'"
        if self.multitap_only_enabled():
            sql+=" AND "+self._multitap_product_clause('p')
        sql+=" ORDER BY COALESCE(NULLIF(p.model,''),'~'),p.registered_product_id,p.option_name,p.id"
        products=self.db.query(sql)
        context=gross_margin.load_context(self.db)
        out=[]
        for product in products:
            row=gross_margin.evaluate(product,context)
            row['link']=self._resolved_product_link(product)
            out.append(row)
        return out

    def build_margin(self):
        self.page_header("그로스 마진", "API 동기화 판매가 + 모델 연결 + 현재 풀필먼트 요금을 자동으로 합쳐 전 품목 마진을 한 번에 봅니다.")
        notebook=ttk.Notebook(self.content); notebook.pack(fill='both',expand=True)
        self.gross_margin_notebook = notebook
        bulk=ttk.Frame(notebook,padding=4); calc=ttk.Frame(notebook,padding=8)
        notebook.add(bulk,text='  일괄 현황  '); notebook.add(calc,text='  단건 계산기  ')

        tools=ttk.Frame(bulk); tools.pack(fill='x',pady=(0,8))
        self.gross_margin_search=tk.StringVar(); self.gross_margin_missing=tk.BooleanVar(value=False)
        ttk.Label(tools,text='검색',style='Filter.TLabel').pack(side='left')
        ent=ttk.Entry(tools,textvariable=self.gross_margin_search,width=28); ent.pack(side='left',padx=(6,8)); ent.bind('<Return>',lambda e:self.refresh_gross_margin())
        ttk.Checkbutton(tools,text='미확인만',variable=self.gross_margin_missing,command=self.refresh_gross_margin).pack(side='left',padx=4)
        ttk.Button(tools,text='조회',style='Toolbar.TButton',command=self.refresh_gross_margin).pack(side='left',padx=4)
        ttk.Button(tools,text='선택 수정',style='Toolbar.TButton',command=self.edit_selected_gross_margin).pack(side='left',padx=4)
        ttk.Label(tools,text='판매가=상품 API · 원가=직접 입력 · 풀필먼트 비용=현재 요금표',foreground='#666').pack(side='right',padx=4)

        summary=ttk.Frame(bulk); summary.pack(fill='x',pady=(0,8))
        self.gross_margin_cards={}
        for key,title in [('total','로켓 옵션'),('ready','자동 계산'),('missing','미확인'),('loss','적자'),('avg','평균 마진율')]:
            box=ttk.LabelFrame(summary,text=title,padding=(10,6)); box.pack(side='left',fill='x',expand=True,padx=(0,6))
            lbl=ttk.Label(box,text='-',font=('맑은 고딕',14,'bold')); lbl.pack(anchor='e')
            self.gross_margin_cards[key]=lbl

        cols=[('model','모델명'),('option','옵션명'),('status','판매상태'),('sale','판매가(API)'),('cost','원가'),
              ('size','사이즈형'),('inout','입출고비'),('shipping','운송비'),('logistics','물류비(VAT)'),
              ('settlement','정산가'),('margin','마진'),('rate','마진율'),('oid','옵션 ID')]
        widths={'model':115,'option':190,'status':90,'sale':105,'cost':100,'size':85,'inout':90,'shipping':90,'logistics':105,
                'settlement':105,'margin':100,'rate':80,'oid':125}
        self.gross_margin_tree=self.make_tree(bulk,cols,widths,height=19)
        self.gross_margin_tree.tag_configure('loss',background='#fff1f1',foreground='#9c1c1c')
        self.gross_margin_tree.tag_configure('warn',background='#fff8df')
        self.gross_margin_tree.tag_configure('good',background='#f8fcf9')
        self.gross_margin_tree.bind('<Button-3>',self._gross_margin_popup)
        self.gross_margin_tree.bind('<Double-1>',self._gross_margin_double_click)
        self.gross_margin_tree.bind('<Return>',lambda e:self.edit_selected_gross_margin())
        self.gross_margin_detail=ttk.Label(bulk,text='',foreground='#666',anchor='w')
        self.gross_margin_detail.pack(fill='x',pady=(6,0))
        self.gross_margin_tree.bind('<<TreeviewSelect>>',lambda e:self._show_gross_margin_detail())

        self._build_margin_calculator(calc)
        self.refresh_gross_margin()

    def refresh_gross_margin(self):
        if not hasattr(self,'gross_margin_tree'):
            return
        try:
            tree=self.gross_margin_tree
            if not tree.winfo_exists(): return
        except Exception:
            return
        for iid in tree.get_children(): tree.delete(iid)
        rows=self._gross_margin_data(); self.gross_margin_row_cache={}
        q=(self.gross_margin_search.get().strip().lower() if hasattr(self,'gross_margin_search') else '')
        missing_only=bool(self.gross_margin_missing.get()) if hasattr(self,'gross_margin_missing') else False
        for r in rows:
            hay=' '.join([r['model'],r['option_name'],r['option_id'],r['product_name'],r['registered_product_id']]).lower()
            if q and q not in hay: continue
            if missing_only and not r['missing']: continue
            status=r['sale_status'] or '미확인'
            status={'판매중':'● 판매중','품절':'▲ 품절','판매중지':'■ 판매중지'}.get(status,'? '+status)
            tag='warn' if r['missing'] else ('loss' if r['margin'] is not None and r['margin']<0 else 'good')
            iid='g:'+str(r['id'])
            vals=(r['model'] or '미연결',r['option_name'],status,
                  money(r['sale']) if r['sale'] is not None else '—',money(r['cost']) if r['cost'] is not None else '—',
                  r['size'] or '미확인',money(r['inout']) if r['inout'] is not None else '—',
                  money(r['shipping']) if r['shipping'] is not None else '—',money(r['logistics']) if r['logistics'] is not None else '—',
                  money(r['settlement']) if r['settlement'] is not None else '—',money(r['margin']) if r['margin'] is not None else '—',
                  (f"{r['margin_rate']:.1f}%" if r['margin_rate'] is not None else '—'),r['option_id'])
            tree.insert('', 'end', iid=iid, values=vals, tags=(tag,))
            self.gross_margin_row_cache[iid]=r
        ready=[r for r in rows if not r['missing'] and r['margin'] is not None]
        losses=[r for r in ready if r['margin']<0]
        missing=[r for r in rows if r['missing']]
        avg=(sum(r['margin_rate'] for r in ready)/len(ready)) if ready else 0
        cards=getattr(self,'gross_margin_cards',{})
        if cards:
            cards['total'].configure(text=f"{len(rows):,}")
            cards['ready'].configure(text=f"{len(ready):,}")
            cards['missing'].configure(text=f"{len(missing):,}")
            cards['loss'].configure(text=f"{len(losses):,}")
            cards['avg'].configure(text=f"{avg:.1f}%")
        self._show_gross_margin_detail()

    def _selected_gross_margin_row(self):
        if not hasattr(self,'gross_margin_tree'): return None
        sel=self.gross_margin_tree.selection()
        if not sel: return None
        return getattr(self,'gross_margin_row_cache',{}).get(sel[0])

    def _show_gross_margin_detail(self):
        if not hasattr(self,'gross_margin_detail'): return
        r=self._selected_gross_margin_row()
        if not r:
            self.gross_margin_detail.configure(text='행 선택 → 원가/사이즈 출처와 미확인 사유 표시 · 더블클릭 / Enter → 원가·사이즈 수정')
            return
        missing=', '.join(r['missing']) if r['missing'] else '없음'
        size_src={'margin-ref':'기존 마진표','legacy-fee-infer':'기존 비용으로 자동복구','manual':'수동 지정'}.get(r['size_source'],r['size_source'] or '미확인')
        self.gross_margin_detail.configure(text=(f"{r['model'] or '미연결'} · 판매가 출처 {r['sale_source']} · 원가 출처 {r['cost_source']} · "
                                                 f"사이즈 출처 {size_src} · 미확인 {missing}"))

    def _gross_margin_double_click(self, event):
        tree=self.gross_margin_tree
        row=tree.identify_row(event.y)
        if not row or tree.identify_region(event.x,event.y) not in ('cell','tree'):
            return 'break'
        tree.selection_set(row); tree.focus(row)
        return self.edit_selected_gross_margin()

    def _gross_margin_popup(self,event):
        tree=self.gross_margin_tree
        row=tree.identify_row(event.y)
        if not row:
            return 'break'
        tree.selection_set(row); tree.focus(row); self._show_gross_margin_detail()
        menu=tk.Menu(self,tearoff=0)
        menu.add_command(label='원가·사이즈 수정',command=self.edit_selected_gross_margin)
        menu.add_command(label='쿠팡에서 상품 열기',command=self.open_selected_gross_product)
        menu.add_separator()
        menu.add_command(label='옵션 ID 복사',command=lambda:self._clipboard_set((self._selected_gross_margin_row() or {}).get('option_id','')))
        try:
            menu.tk_popup(event.x_root,event.y_root)
        finally:
            menu.grab_release()

    def open_selected_gross_product(self):
        r=self._selected_gross_margin_row()
        if not r: return
        link=str(r.get('link') or '')
        if not link:
            return messagebox.showinfo(APP_NAME,'이 옵션은 노출상품 ID/옵션 ID가 없어 링크를 만들 수 없습니다.')
        webbrowser.open(link)

    def edit_selected_gross_margin(self):
        existing=getattr(self,'_gross_margin_edit_dialog',None)
        if existing is not None and existing.winfo_exists():
            existing.lift(); existing.cost_entry.focus_set()
            return 'break'
        r=self._selected_gross_margin_row()
        if not r:
            messagebox.showinfo(APP_NAME,'수정할 모델 행을 선택하세요.')
            return 'break'
        if not r.get('model'):
            messagebox.showinfo(APP_NAME,'마스터 데이터 > 모델·옵션 매핑에서 모델을 먼저 연결하세요.')
            return 'break'
        win=tk.Toplevel(self); self._gross_margin_edit_dialog=win
        win.title('그로스 마진 수정'); win.transient(self); win.grab_set(); win.resizable(False,False)
        frame=ttk.Frame(win,padding=20); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=r['model'],font=('맑은 고딕',14,'bold')).grid(row=0,column=0,columnspan=2,sticky='w')
        ttk.Label(frame,text=r['option_name'] or r['product_name'],wraplength=440).grid(row=1,column=0,columnspan=2,sticky='w',pady=(6,14))
        price=(money(r['sale'])+'원') if r.get('sale') is not None else '미확인'
        ttk.Label(frame,text='현재 판매가').grid(row=2,column=0,sticky='w',pady=6)
        ttk.Label(frame,text=price+' · '+r['sale_source'],foreground='#526a85').grid(row=2,column=1,sticky='e',pady=6)
        value=r.get('cost')
        raw='' if value is None else str(int(value)) if float(value).is_integer() else str(value)
        cost=tk.StringVar(win,value=raw); size=tk.StringVar(win,value=r.get('size') or '미지정')
        win.cost_var=cost; win.size_var=size
        ttk.Label(frame,text='원가 (원)').grid(row=3,column=0,sticky='w',pady=6)
        entry=ttk.Entry(frame,textvariable=cost,width=24,justify='right'); entry.grid(row=3,column=1,sticky='ew',pady=6)
        win.cost_entry=entry
        ttk.Label(frame,text='사이즈형').grid(row=4,column=0,sticky='w',padx=(0,20),pady=6)
        cb=ttk.Combobox(frame,textvariable=size,values=['미지정','극소형','소형','중형','대형 1','대형 2','특대형'],state='readonly',width=22)
        cb.grid(row=4,column=1,sticky='ew',pady=6)
        copied_size=tk.StringVar(win)
        win.copied_size_var=copied_size
        ttk.Label(frame,text='쿠팡 사이즈 정보\n붙여넣기').grid(row=5,column=0,sticky='w',padx=(0,20),pady=6)
        pasted=ttk.Entry(frame,textvariable=copied_size,width=42)
        pasted.grid(row=5,column=1,sticky='ew',pady=6)
        detected=tk.StringVar(win,value='예: 극소형 - 182*311*49(mm), 455g')
        ttk.Label(frame,textvariable=detected,foreground='#526a85',wraplength=440).grid(row=6,column=0,columnspan=2,sticky='w')
        def detect_size(*args):
            text=copied_size.get().strip()
            parsed=gross_margin.parse_coupang_size(text)
            if parsed:
                size.set(parsed)
                detected.set('인식된 사이즈형: '+parsed+' · 저장 시 적용')
            elif text:
                detected.set('사이즈형을 인식하지 못했습니다. 쿠팡의 등급명부터 붙여 넣어 주세요.')
            else:
                detected.set('예: 극소형 - 182*311*49(mm), 455g')
        copied_size.trace_add('write',detect_size)
        ttk.Label(frame,text='같은 모델의 모든 로켓그로스 옵션에 적용됩니다.\n원가 빈칸·사이즈 미지정은 기존 값을 유지합니다.\n판매가는 상품정보를 사용하고, 물류비는 요금표로 계산합니다.',
                  foreground='#666',justify='left',wraplength=440).grid(row=7,column=0,columnspan=2,sticky='w',pady=(10,8))
        def close(event=None):
            win.destroy()
            if self.gross_margin_tree.winfo_exists(): self.gross_margin_tree.focus_set()
            return 'break'
        def save(event=None):
            if copied_size.get().strip() and not gross_margin.parse_coupang_size(copied_size.get()):
                messagebox.showerror(APP_NAME,'붙여 넣은 사이즈형을 인식하지 못했습니다.\n등급명부터 다시 붙여 넣거나, 붙여넣기 칸을 비우고 목록에서 선택하세요.',parent=win)
                pasted.focus_set(); return 'break'
            raw=cost.get().strip()
            value=gross_margin.number(raw) if raw else None
            if raw and (value is None or value<=0):
                messagebox.showerror(APP_NAME,'원가는 0보다 큰 숫자로 입력하세요.',parent=win)
                entry.focus_set(); return 'break'
            selected_size=self._normalize_gross_size(size.get())
            if size.get()!='미지정' and not selected_size:
                messagebox.showerror(APP_NAME,'사이즈형을 목록에서 선택하세요.',parent=win)
                return 'break'
            changes={}
            if value is not None and value!=r.get('cost'):
                changes.update(manual_cost=value,cost_source='수동 입력')
            if selected_size and selected_size!=r.get('size'):
                changes.update(size_type=selected_size,source='manual')
            if changes:
                fields=['model',*changes]
                update=','.join(key+'=excluded.'+key for key in changes)
                sql=('INSERT INTO gross_margin_profiles('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') '
                     'ON CONFLICT(model) DO UPDATE SET '+update+',updated_at=CURRENT_TIMESTAMP')
                try:
                    with self.db.conn:
                        self.db.conn.execute(sql,[r['model'],*changes.values()])
                except sqlite3.Error as exc:
                    messagebox.showerror(APP_NAME,'수정 내용을 저장하지 못했습니다.\n'+str(exc),parent=win)
                    return 'break'
            iid='g:'+str(r['id'])
            close(); self.refresh_gross_margin()
            if self.gross_margin_tree.exists(iid):
                self.gross_margin_tree.selection_set(iid); self.gross_margin_tree.focus(iid); self.gross_margin_tree.see(iid)
            return 'break'
        buttons=ttk.Frame(frame); buttons.grid(row=8,column=0,columnspan=2,sticky='e',pady=(12,0))
        ttk.Button(buttons,text='취소',command=close).pack(side='left',padx=4)
        ttk.Button(buttons,text='저장',style='Accent.TButton',command=save).pack(side='left',padx=4)
        win.bind('<Escape>',close); win.bind('<Return>',save); win.protocol('WM_DELETE_WINDOW',close)
        win.update_idletasks()
        x=max(0,self.winfo_rootx()+(self.winfo_width()-win.winfo_reqwidth())//2)
        y=max(0,self.winfo_rooty()+(self.winfo_height()-win.winfo_reqheight())//2)
        win.geometry(f'+{x}+{y}')
        entry.focus_set(); entry.selection_range(0,'end')
        return 'break'

    def _build_margin_calculator(self, parent):
        outer=ttk.Frame(parent); outer.pack(fill='both',expand=True)
        left=ttk.LabelFrame(outer,text='계산기',padding=16); left.pack(side='left',fill='y',padx=(0,12))
        right=ttk.LabelFrame(outer,text='계산 결과',padding=16); right.pack(side='left',fill='both',expand=True)
        self.margin_method=tk.StringVar(value='로켓그로스'); self.margin_model=tk.StringVar()
        self.mvars={k:tk.StringVar() for k in ['cost','sale','commission','size','inout','shipping']}
        self.mvars['commission'].set(self.db.setting('commission_rate','7.8'))
        r=0
        ttk.Label(left,text='판매 방식').grid(row=r,column=0,sticky='w',pady=5); cb=ttk.Combobox(left,textvariable=self.margin_method,values=['로켓그로스','판매자배송'],state='readonly',width=20); cb.grid(row=r,column=1,pady=5); r+=1
        ttk.Label(left,text='기존 품목 불러오기').grid(row=r,column=0,sticky='w',pady=5); self.margin_model_cb=ttk.Combobox(left,textvariable=self.margin_model,width=20); self.margin_model_cb.grid(row=r,column=1,pady=5); r+=1
        for k,label in [('cost','원가'),('sale','판매가'),('commission','수수료(%)'),('size','사이즈형'),('inout','입출고비'),('shipping','배송/운송비')]:
            ttk.Label(left,text=label).grid(row=r,column=0,sticky='w',pady=5)
            if k=='size': w=ttk.Combobox(left,textvariable=self.mvars[k],values=['극소형','소형','중형','대형 1','대형 2','특대형'],width=20)
            else: w=ttk.Entry(left,textvariable=self.mvars[k],width=23)
            w.grid(row=r,column=1,pady=5); r+=1
        ttk.Button(left,text='풀필먼트 요금 자동 적용',command=self.apply_fulfillment_rate).grid(row=r,column=0,columnspan=2,sticky='ew',pady=(12,4)); r+=1
        ttk.Button(left,text='계산',style='Accent.TButton',command=self.calculate_margin).grid(row=r,column=0,columnspan=2,sticky='ew',pady=4)
        self.margin_method.trace_add('write',lambda *_:self.refresh_margin_models()); self.margin_model_cb.bind('<<ComboboxSelected>>',lambda e:self.load_margin_ref())
        self.margin_result=ttk.Frame(right); self.margin_result.pack(fill='both',expand=True)
        self.refresh_margin_models(); self.render_margin_result(None)

    def refresh_margin_models(self):
        method=self.margin_method.get()
        rows=self.db.query('SELECT DISTINCT model FROM margin_refs WHERE method=? ORDER BY model',(method,))
        values=[r[0] for r in rows]
        if self.multitap_only_enabled():
            allowed=set(self.visible_models())
            values=[m for m in values if m in allowed]
        self.margin_model_cb['values']=values
        if self.margin_model.get() not in self.margin_model_cb['values']: self.margin_model.set('')
        if method=='판매자배송':
            self.mvars['size'].set(''); self.mvars['inout'].set('0')

    def load_margin_ref(self):
        r=self.db.one('SELECT * FROM margin_refs WHERE method=? AND model=? ORDER BY id LIMIT 1',(self.margin_method.get(),self.margin_model.get()))
        if not r: return
        self.mvars['cost'].set('' if r['cost'] is None else str(r['cost']))
        api_row=None
        if self.margin_method.get()=='로켓그로스':
            api_row=self.db.one("""SELECT sale_price FROM products WHERE fulfillment_type='로켓그로스' AND model=? AND sale_price IS NOT NULL ORDER BY updated_at DESC,id DESC LIMIT 1""",(self.margin_model.get(),))
        self.mvars['sale'].set(str(api_row['sale_price']) if api_row and api_row['sale_price'] else ('' if r['sale_price'] is None else str(r['sale_price'])))
        self.mvars['commission'].set(str(r['commission'] or self.db.setting('commission_rate','7.8')))
        profile=self.db.one('SELECT size_type FROM gross_margin_profiles WHERE model=?',(self.margin_model.get(),))
        size=self._normalize_gross_size(profile['size_type'] if profile else r['size_type'])
        if not size: size=self._infer_gross_size_from_legacy_ref(r)
        self.mvars['size'].set(size)
        self.mvars['inout'].set('' if r['inout_fee'] is None else str(r['inout_fee']))
        self.mvars['shipping'].set('' if r['shipping_fee'] is None else str(r['shipping_fee']))
        if self.margin_method.get()=='로켓그로스' and size and num(self.mvars['sale'].get(),0)>0:
            rate=self._gross_rate_for(size,num(self.mvars['sale'].get(),0))
            if rate:
                self.mvars['shipping'].set(str(rate['shipping_current'] or rate['shipping_regular'] or 0))
                self.mvars['inout'].set(str(rate['inout_current'] or rate['inout_regular'] or 0))
        self.calculate_margin()

    def apply_fulfillment_rate(self):
        if self.margin_method.get()!='로켓그로스': return messagebox.showinfo(APP_NAME,'풀필먼트 요금 자동 적용은 로켓그로스용입니다.')
        sale=num(self.mvars['sale'].get()); size=self.mvars['size'].get().strip()
        if not sale or not size: return messagebox.showwarning(APP_NAME,'판매가와 사이즈형을 먼저 입력하세요.')
        r=self._gross_rate_for(size,sale)
        if not r: return messagebox.showwarning(APP_NAME,'해당 요금 구간을 찾지 못했습니다.')
        self.mvars['shipping'].set(str(r['shipping_current'] or r['shipping_regular'] or 0)); self.mvars['inout'].set(str(r['inout_current'] or r['inout_regular'] or 0))
        self.calculate_margin()

    def calculate_margin(self):
        method=self.margin_method.get(); cost=num(self.mvars['cost'].get()); sale=num(self.mvars['sale'].get()); ship=num(self.mvars['shipping'].get()); inbound=num(self.mvars['inout'].get())
        comm=num(self.mvars['commission'].get(),7.8)
        if method=='로켓그로스':
            factor=1-(comm*1.1/100); settlement=round(sale*factor); logistics=(inbound+ship)*1.1; margin=settlement-cost-logistics
        else:
            factor=1-(comm*1.1/100); settlement=round(sale*factor); logistics=ship; margin=settlement-cost-ship
        rate=(margin/sale*100) if sale else 0
        self.render_margin_result({'settlement':settlement,'logistics':logistics,'margin':margin,'rate':rate,'factor':factor})

    def render_margin_result(self, data):
        for w in self.margin_result.winfo_children(): w.destroy()
        if not data:
            ttk.Label(self.margin_result,text='왼쪽에서 품목을 불러오거나 값을 입력한 뒤 계산하세요.',foreground='#666').pack(anchor='w'); return
        vals=[('정산가',money(data['settlement'])+' 원'),('물류/운송 비용',money(data['logistics'])+' 원'),('예상 마진',money(data['margin'])+' 원'),('마진율',f"{data['rate']:.1f}%"),('정산 배율',f"{data['factor']:.4f}")]
        for title,value in vals:
            box=ttk.LabelFrame(self.margin_result,text=title,padding=14); box.pack(fill='x',pady=6)
            ttk.Label(box,text=value,style='CardValue.TLabel').pack(anchor='e')

    # ---------------- Stock ----------------
    def build_rocket_inventory(self):
        self.rocket_inventory_page = rocket_inventory.RocketInventoryPage(self.content, self)
        self.rocket_inventory_page.pack(fill='both', expand=True)

    # ---------------- Sales ----------------
    def build_sales(self):
        self.page_header("일일 매출", "API 갱신은 데이터 · 설정에서 실행합니다. 로켓그로스 주문은 결제일(한국시간)·옵션별로 기록합니다.")
        ttk.Label(self.content,text='주문 API: 출고분·잠정 금액(판매수량 × 단가) · 취소/반품 차감 순매출과 다를 수 있습니다.',foreground='#795200').pack(anchor='w',pady=(0,4))
        self.sales_api_status=ttk.Label(self.content,text='',foreground='#666',wraplength=1100)
        self.sales_api_status.pack(anchor='w',pady=(0,8))
        top=ttk.LabelFrame(self.content,text='매출 수동 추가',padding=10); top.pack(fill='x',pady=(0,10))
        self.sale_vars={
            'date':tk.StringVar(value=date.today().isoformat()),'model':tk.StringVar(),'method':tk.StringVar(value='로켓그로스'),
            'revenue':tk.StringVar(),'orders':tk.StringVar(),'qty':tk.StringVar()
        }
        specs=[('날짜','date',12),('모델명','model',18),('판매방식','method',13),('매출(원)','revenue',12),('주문','orders',8),('판매량','qty',8)]
        for i,(lab,key,wid) in enumerate(specs):
            ttk.Label(top,text=lab).grid(row=0,column=i*2,sticky='w',padx=(0,4))
            if key=='method': w=ttk.Combobox(top,textvariable=self.sale_vars[key],values=['로켓그로스','판매자배송'],state='readonly',width=wid)
            elif key=='model': w=ttk.Combobox(top,textvariable=self.sale_vars[key],values=self.visible_models(),width=wid)
            else: w=ttk.Entry(top,textvariable=self.sale_vars[key],width=wid)
            w.grid(row=0,column=i*2+1,padx=(0,10))
        ttk.Button(top,text='추가',style='Accent.TButton',command=self.add_sale).grid(row=0,column=12)
        bar=ttk.Frame(self.content); bar.pack(fill='x',pady=(0,8)); self.sales_q=tk.StringVar()
        ttk.Label(bar,text='모델/옵션ID 검색').pack(side='left'); e=ttk.Entry(bar,textvariable=self.sales_q,width=25); e.pack(side='left',padx=6); ttk.Button(bar,text='조회',command=self.refresh_sales).pack(side='left')
        ttk.Button(bar,text='쿠팡 일일매출 Excel',style='Accent.TButton',command=self.import_coupang_sales_excel).pack(side='right')
        ttk.Button(bar,text='기존 Excel 가져오기',command=self.import_excel_workbook).pack(side='right',padx=6)
        ttk.Button(bar,text='CSV 가져오기',command=self.import_sales_csv).pack(side='right'); ttk.Button(bar,text='CSV 내보내기',command=self.export_sales_csv).pack(side='right',padx=6); ttk.Button(bar,text='선택 삭제',style='Danger.TButton',command=self.delete_sale).pack(side='right')
        cols=[('id','ID'),('date','날짜'),('model','모델명'),('oid','옵션 ID'),('option','옵션명'),('method','판매방식'),('revenue','매출(원)'),('orders','주문'),('qty','판매량'),('source','자료 출처')]
        self.sales_tree=self.make_tree(self.content,cols,{'id':55,'date':105,'model':120,'oid':125,'option':230,'method':105,'revenue':120,'orders':65,'qty':65,'source':180})
        e.bind('<Return>',lambda x:self.refresh_sales()); self.refresh_sales()

    def add_sale(self):
        v=self.sale_vars
        try: datetime.strptime(v['date'].get(),'%Y-%m-%d')
        except Exception: return messagebox.showwarning(APP_NAME,'날짜는 YYYY-MM-DD 형식으로 입력하세요.')
        if not v['model'].get().strip(): return messagebox.showwarning(APP_NAME,'모델명을 입력하세요.')
        rocket_sales.prefer_external_sale(self.db.conn,v['date'].get(),v['model'].get().strip(),v['method'].get())
        self.db.execute('INSERT INTO sales(sale_date,model,method,revenue,orders,quantity,source) VALUES (?,?,?,?,?,?,?)',(v['date'].get(),v['model'].get().strip(),v['method'].get(),num(v['revenue'].get()),num(v['orders'].get()),num(v['qty'].get()),'manual'))
        v['revenue'].set('');v['orders'].set('');v['qty'].set(''); self.refresh_sales()

    def refresh_sales(self):
        term=self.sales_q.get().strip() if hasattr(self,'sales_q') else ''
        q=f"%{term}%"
        vendor=self.db.setting('api_vendor_id','').strip()
        api_source=rocket_sales.SOURCE_PREFIX+vendor
        missing_api="(COALESCE(model,'')='' AND source=?)"
        if self.multitap_only_enabled():
            allowed=self.visible_models()
            if allowed:
                marks=','.join('?' for _ in allowed)
                sql=f"SELECT * FROM sales WHERE (model LIKE ? OR COALESCE(option_id,'') LIKE ? OR COALESCE(option_name,'') LIKE ?) AND (model IN ({marks}) OR {missing_api}) ORDER BY sale_date DESC,id DESC LIMIT 2500"
                rows=self.db.query(sql,[q,q,q,*allowed,api_source])
            else:
                rows=self.db.query(f"SELECT * FROM sales WHERE {missing_api} AND (COALESCE(option_id,'') LIKE ? OR COALESCE(option_name,'') LIKE ?) ORDER BY sale_date DESC,id DESC LIMIT 2500",(api_source,q,q))
        else:
            rows=self.db.query("SELECT * FROM sales WHERE model LIKE ? OR COALESCE(option_id,'') LIKE ? OR COALESCE(option_name,'') LIKE ? ORDER BY sale_date DESC,id DESC LIMIT 2500",(q,q,q))
        self.sales_tree.delete(*self.sales_tree.get_children())
        for r in rows:
            self.sales_tree.insert('', 'end',iid=str(r['id']),values=(r['id'],r['sale_date'],r['model'] or '모델 연결 필요',r['option_id'] or '',r['option_name'] or '',r['method'],money(r['revenue']),money(r['orders']),money(r['quantity']),rocket_sales.source_label(r['source'])))
        stamp=self.db.setting('rg_orders_last_sync:'+vendor,'')
        status=('마지막 주문 조회: '+stamp[:19].replace('T',' ')+' (한국시간) · '+self.db.setting('rg_orders_last_result:'+vendor,'')) if stamp else '주문 API 조회 전 · 데이터 · 설정에서 로켓그로스 일일 매출을 갱신하세요.'
        missing=self.db.one("SELECT COUNT(*) n FROM sales WHERE source=? AND COALESCE(model,'')=''",(api_source,))['n']
        if missing:
            status+=f' · 모델 미연결 {missing}건은 함께 표시합니다. 마스터 데이터 > 모델·옵션 매핑에서 연결하세요.'
        error=self.db.setting('rg_orders_last_error:'+vendor,'')
        if error: status+=' · 최근 조회 실패: '+error[:150]
        self.sales_api_status.configure(text=status)

    def delete_sale(self):
        sel=self.sales_tree.selection() if hasattr(self,'sales_tree') else ()
        ids=sorted({int(str(i)) for i in sel if str(i).isdigit()})
        if not ids:
            return messagebox.showinfo(APP_NAME,'삭제할 매출 기록을 선택하세요.')
        if not messagebox.askyesno(APP_NAME,f'선택한 {len(ids)}개 매출 기록을 삭제할까요?'):
            return
        self.db.conn.executemany('DELETE FROM sales WHERE id=?',[(i,) for i in ids]); self.db.conn.commit(); self.refresh_sales()

    def import_sales_csv(self):
        p=filedialog.askopenfilename(title='매출 CSV 가져오기',filetypes=[('CSV','*.csv'),('모든 파일','*.*')])
        if not p:return
        encs=['utf-8-sig','cp949']; data=None
        for enc in encs:
            try:
                with open(p,'r',encoding=enc,newline='') as f:data=list(csv.DictReader(f));break
            except UnicodeDecodeError: pass
        if data is None:return messagebox.showerror(APP_NAME,'CSV 인코딩을 읽지 못했습니다.')
        count=0
        for row in data:
            d=row.get('날짜') or row.get('sale_date'); model=row.get('모델명') or row.get('옵션명') or row.get('model'); method=row.get('판매방식') or row.get('method') or '로켓그로스'; oid=row.get('옵션 ID') or row.get('옵션ID') or row.get('option_id') or ''
            if not d or not model: continue
            try: datetime.strptime(d,'%Y-%m-%d')
            except Exception: continue
            rocket_sales.prefer_external_sale(self.db.conn,d,model,method,oid)
            self.db.execute('INSERT INTO sales(sale_date,model,method,revenue,orders,quantity,option_id,option_name,source) VALUES (?,?,?,?,?,?,?,?,?)',(d,model,method,num(row.get('매출(원)') or row.get('매출') or row.get('revenue')),num(row.get('주문') or row.get('orders')),num(row.get('판매량') or row.get('quantity')),oid,row.get('쿠팡 옵션명') or row.get('option_name') or '', 'csv'));count+=1
        self.refresh_sales();messagebox.showinfo(APP_NAME,f'{count}건을 가져왔습니다.')

    def export_sales_csv(self):
        p=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='일일매출.csv',filetypes=[('CSV','*.csv')])
        if not p:return
        rows=self.db.query('SELECT sale_date,model,option_id,option_name,method,revenue,orders,quantity,source FROM sales ORDER BY sale_date,id')
        with open(p,'w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(['날짜','모델명','옵션 ID','쿠팡 옵션명','판매방식','매출(원)','주문','판매량','자료 출처']);w.writerows([tuple(r)[:-1]+(rocket_sales.source_label(r['source']),) for r in rows])
        messagebox.showinfo(APP_NAME,'CSV 내보내기가 완료되었습니다.')

    # ---------------- Excel import ----------------
    @staticmethod
    def _excel_text(value):
        if value is None:
            return ''
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    @staticmethod
    def _excel_number(value, default=None):
        if value in (None, ''):
            return default
        try:
            if isinstance(value, (int, float)):
                return float(value)
            return float(str(value).replace(',', '').strip())
        except Exception:
            return default

    @staticmethod
    def _excel_date(value, default_year):
        if value is None or value == '':
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text=str(value).strip()
        for fmt in ('%Y-%m-%d','%Y.%m.%d','%Y/%m/%d'):
            try:
                return datetime.strptime(text,fmt).date().isoformat()
            except Exception:
                pass
        try:
            if '월' in text and '일' in text:
                month=int(text.split('월',1)[0].strip())
                day=int(text.split('월',1)[1].split('일',1)[0].strip())
                return date(int(default_year),month,day).isoformat()
        except Exception:
            pass
        return None

    def _infer_excel_year(self, wb):
        if '일일 매출' in wb.sheetnames:
            ws=wb['일일 매출']
            years=[]
            for row in ws.iter_rows(min_row=2,max_col=1,values_only=True):
                v=row[0]
                if isinstance(v, datetime): years.append(v.year)
                elif isinstance(v, date): years.append(v.year)
                elif v:
                    try: years.append(datetime.strptime(str(v).strip()[:10],'%Y-%m-%d').year)
                    except Exception: pass
            if years:
                return max(years)
        return date.today().year

    def _excel_backup_before_import(self):
        backup_dir=DATA_DIR/'backups'; backup_dir.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
        target=backup_dir/f'pre_excel_import_{stamp}.db'
        self.db.backup_to(target)
        return target

    def import_excel_workbook(self):
        path=filedialog.askopenfilename(
            title='쿠팡 관리 Excel 가져오기',
            filetypes=[('Excel 통합문서','*.xlsx *.xlsm'),('Excel 매크로 통합문서','*.xlsm'),('Excel 통합문서','*.xlsx'),('모든 파일','*.*')]
        )
        if not path:
            return
        try:
            from openpyxl import load_workbook
        except Exception:
            return messagebox.showerror(APP_NAME,'Excel 가져오기에 필요한 openpyxl 모듈을 찾을 수 없습니다.\nBUILD_EXE.cmd로 새 EXE를 빌드하거나 openpyxl을 설치하세요.')
        try:
            wb=load_workbook(path,read_only=True,data_only=True)
        except Exception as e:
            LOGGER.exception('Excel 열기 실패: %s',path)
            return messagebox.showerror(APP_NAME,f'Excel 파일을 열지 못했습니다.\n\n{e}')
        available=[x for x in ('등록관리','그로스 입고 관리','일일 매출') if x in wb.sheetnames]
        if not available:
            wb.close()
            return messagebox.showwarning(APP_NAME,'지원하는 시트를 찾지 못했습니다.\n지원 시트: 등록관리 / 그로스 입고 관리 / 일일 매출')
        dlg=ExcelImportDialog(self,available,self._infer_excel_year(wb)); self.wait_window(dlg)
        if not dlg.result:
            wb.close(); return
        backup=self._excel_backup_before_import()
        results={}
        try:
            self.db.conn.execute('BEGIN')
            if '등록관리' in dlg.result['sheets']:
                results['등록관리']=self._import_excel_products(wb['등록관리'])
            if '그로스 입고 관리' in dlg.result['sheets']:
                results['그로스 입고 관리']=self._import_excel_inbounds(wb['그로스 입고 관리'],dlg.result['year'])
            if '일일 매출' in dlg.result['sheets']:
                results['일일 매출']=self._import_excel_sales(wb['일일 매출'])
            self.db.conn.commit()
        except Exception as e:
            self.db.conn.rollback()
            LOGGER.exception('Excel 가져오기 실패: %s',path)
            wb.close()
            return messagebox.showerror(APP_NAME,f'Excel 가져오기 중 오류가 발생했습니다.\n변경사항은 취소했습니다.\n\n{e}')
        finally:
            try: wb.close()
            except Exception: pass
        lines=[]
        for name,r in results.items():
            if name=='등록관리':
                lines.append(f"상품: 추가 {r['inserted']} / 갱신 {r['updated']} / 건너뜀 {r['skipped']}")
            elif name=='그로스 입고 관리':
                lines.append(f"입고: 추가 {r['inserted']} / 갱신 {r['updated']} / 동일 {r['skipped']} / 충돌 {r['conflicts']}")
            elif name=='일일 매출':
                lines.append(f"매출: 추가 {r['inserted']} / 동일 {r['skipped']}")
        LOGGER.info('Excel 가져오기 완료 file=%s result=%s backup=%s',path,results,backup)
        messagebox.showinfo(APP_NAME,'Excel 가져오기가 완료되었습니다.\n\n'+'\n'.join(lines)+f'\n\n자동 백업: {backup.name}')
        try:
            if hasattr(self,'prod_tree') and self.prod_tree.winfo_exists(): self.refresh_products()
        except Exception: pass
        try:
            if hasattr(self,'sales_tree') and self.sales_tree.winfo_exists(): self.refresh_sales()
        except Exception: pass

    def _import_excel_products(self, ws):
        rows=ws.iter_rows(values_only=True); headers=[self._excel_text(v) for v in next(rows)]; idx={h:i for i,h in enumerate(headers) if h}
        def val(row,name):
            i=idx.get(name); return row[i] if i is not None and i<len(row) else None
        valid_management={'판매중','로켓그로스','품절','판매중지'}; valid_sale={'미확인','판매중','품절','판매중지'}
        category=''; carry={'product_name':'','display_name':'','registered_product_id':'','link':''}; inserted=updated=skipped=0
        has_ft='배송방식' in idx or '배송' in idx; has_mgmt='관리상태' in idx; has_sale='판매상태' in idx
        for row in rows:
            legacy_status=self._excel_text(val(row,'상태')); model=self._excel_text(val(row,'모델명'))
            if not model:
                if legacy_status and legacy_status not in valid_management:
                    category=legacy_status; carry={'product_name':'','display_name':'','registered_product_id':'','link':''}
                continue
            management=self._excel_text(val(row,'관리상태')) if has_mgmt else legacy_status
            if management in ('묶음','묶임'): management=''
            elif management not in valid_management: management='판매중'
            explicit_ft=(self._excel_text(val(row,'배송방식')) or self._excel_text(val(row,'배송'))) if has_ft else ''
            if explicit_ft not in ('판매자배송','로켓그로스'): explicit_ft=''
            fulfillment=explicit_ft or ('로켓그로스' if management=='로켓그로스' else '판매자배송')
            explicit_sale=self._excel_text(val(row,'판매상태')) if has_sale else ''
            if explicit_sale not in valid_sale: explicit_sale=''
            mapping={'product_name':self._excel_text(val(row,'상품명')),'display_name':self._excel_text(val(row,'노출명')),'registered_product_id':self._excel_text(val(row,'등록상품 ID')),'link':self._excel_text(val(row,'링크'))}
            for k,v in mapping.items():
                if v: carry[k]=v
            product_name=mapping['product_name'] or carry['product_name']; display_name=mapping['display_name'] or carry['display_name']; regid=mapping['registered_product_id'] or carry['registered_product_id']
            option_name=self._excel_text(val(row,'옵션명')); option_id=self._excel_text(val(row,'옵션 ID')); exposed_id=self._excel_text(val(row,'노출상품 ID')); gift=self._excel_text(val(row,'사은품'))
            sale=self._excel_number(val(row,'판매가'),None); coupon=self._excel_number(val(row,'쿠폰적용가'),None); map_price=self._excel_number(val(row,'지도가'),None); diff=self._excel_number(val(row,'지도가대비판매가'),None)
            if diff is None and map_price is not None:
                base=coupon if coupon not in (None,0) else sale; diff=(base-map_price) if base is not None else None
            note=self._excel_text(val(row,'비고')); link=mapping['link'] or carry['link']
            if exposed_id and option_id:
                link=self._coupang_product_link(exposed_id,'',option_id)
            elif not link:
                link=''
            existing=None
            if option_id: existing=self.db.conn.execute('SELECT * FROM products WHERE option_id=? ORDER BY id LIMIT 1',(option_id,)).fetchone()
            if existing is None and regid and option_name:
                existing=self.db.conn.execute("""SELECT * FROM products WHERE registered_product_id=? AND model=? AND option_name=? AND COALESCE(NULLIF(fulfillment_type,''),'판매자배송')=? ORDER BY id LIMIT 1""",(regid,model,option_name,fulfillment)).fetchone()
            if existing:
                old=dict(existing); sale_status=explicit_sale or old.get('sale_status') or '미확인'
                values={'category':category or old.get('category') or '','fulfillment_type':fulfillment or old.get('fulfillment_type') or '판매자배송','status':management or old.get('status') or '판매중','sale_status':sale_status,'model':model or old.get('model') or '','product_name':product_name or old.get('product_name') or '','display_name':display_name or old.get('display_name') or '','registered_product_id':regid or old.get('registered_product_id') or '','exposed_product_id':exposed_id or old.get('exposed_product_id') or '','option_name':option_name or old.get('option_name') or '','option_id':option_id or old.get('option_id') or '','gift':gift if gift!='' else (old.get('gift') or ''),'sale_price':sale if sale is not None else old.get('sale_price'),'coupon_price':coupon,'map_price':map_price if map_price is not None else old.get('map_price'),'price_diff':diff,'link':link or old.get('link') or '','note':note or old.get('note') or ''}
                changed=any(str(values[k] if values[k] is not None else '')!=str(old.get(k) if old.get(k) is not None else '') for k in values)
                if changed:
                    self.db.conn.execute('''UPDATE products SET category=?,fulfillment_type=?,status=?,sale_status=?,model=?,product_name=?,display_name=?,registered_product_id=?,exposed_product_id=?,option_name=?,option_id=?,gift=?,sale_price=?,coupon_price=?,map_price=?,price_diff=?,link=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(values['category'],values['fulfillment_type'],values['status'],values['sale_status'],values['model'],values['product_name'],values['display_name'],values['registered_product_id'],values['exposed_product_id'],values['option_name'],values['option_id'],values['gift'],values['sale_price'],values['coupon_price'],values['map_price'],values['price_diff'],values['link'],values['note'],existing['id'])); updated+=1
                else: skipped+=1
                if exposed_id:
                    self._record_exposure(self.db,values['option_id'],old.get('seller_product_item_id',''),values['registered_product_id'],exposed_id,values['model'],values['option_name'],'excel-import',existing['id'])
                    if str(old.get('exposed_product_id') or '').strip() and str(old.get('exposed_product_id') or '').strip()!=str(exposed_id).strip():
                        self._record_product_event(self.db,'exposure_id',existing,old.get('exposed_product_id'),exposed_id,'excel-import')
            else:
                cur=self.db.conn.execute('''INSERT INTO products(category,fulfillment_type,status,sale_status,model,product_name,display_name,registered_product_id,exposed_product_id,option_name,option_id,gift,sale_price,coupon_price,map_price,price_diff,link,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(category,fulfillment,management,explicit_sale or '미확인',model,product_name,display_name,regid,exposed_id,option_name,option_id,gift,sale,coupon,map_price,diff,link,note)); inserted+=1
                if exposed_id: self._record_exposure(self.db,option_id,'',regid,exposed_id,model,option_name,'excel-import',cur.lastrowid)
        return {'inserted':inserted,'updated':updated,'skipped':skipped}

    def _import_excel_inbounds(self, ws, year):
        rows=list(ws.iter_rows(values_only=True))
        if not rows:
            return {'inserted':0,'updated':0,'skipped':0,'conflicts':0}
        header=rows[0]
        dates=[]
        for c in range(2,len(header)):
            dates.append((c,self._excel_date(header[c],year)))
        category=''; inserted=updated=skipped=conflicts=0
        for row in rows[1:]:
            cat=self._excel_text(row[0] if len(row)>0 else None)
            model=self._excel_text(row[1] if len(row)>1 else None)
            if cat: category=cat
            if not model: continue
            for c,d in dates:
                if not d or c>=len(row): continue
                qty=self._excel_number(row[c],None)
                if qty in (None,0): continue
                existing=self.db.conn.execute('SELECT * FROM inbounds WHERE inbound_date=? AND model=? ORDER BY id',(d,model)).fetchall()
                total=sum(float(x['quantity'] or 0) for x in existing)
                if abs(total-float(qty))<1e-9:
                    skipped+=1; continue
                if len(existing)==1 and not str(existing[0]['memo'] or '').strip():
                    self.db.conn.execute('UPDATE inbounds SET category=?,quantity=? WHERE id=?',(category,qty,existing[0]['id']))
                    updated+=1
                elif not existing:
                    self.db.conn.execute('INSERT INTO inbounds(inbound_date,category,model,quantity,memo) VALUES (?,?,?,?,?)',(d,category,model,qty,'Excel 가져오기'))
                    inserted+=1
                else:
                    conflicts+=1
                    LOGGER.warning('Excel 입고 충돌 date=%s model=%s excel=%s db_total=%s rows=%s',d,model,qty,total,len(existing))
        return {'inserted':inserted,'updated':updated,'skipped':skipped,'conflicts':conflicts}

    def _import_excel_sales(self, ws):
        rows=ws.iter_rows(values_only=True)
        try: headers=[self._excel_text(v) for v in next(rows)]
        except StopIteration: return {'inserted':0,'skipped':0}
        def col(names):
            for i,h in enumerate(headers):
                hs=h.replace(' ','')
                for n in names:
                    if hs.startswith(n.replace(' ','')):
                        return i
            return None
        ci_date=col(['날짜']); ci_model=col(['옵션명','모델명']); ci_method=col(['판매방식']); ci_rev=col(['매출']); ci_orders=col(['주문']); ci_qty=col(['판매량'])
        inserted=skipped=0
        for row in rows:
            if ci_date is None or ci_model is None: break
            d=self._excel_date(row[ci_date] if ci_date<len(row) else None,date.today().year)
            model=self._excel_text(row[ci_model] if ci_model<len(row) else None)
            if not d or not model: continue
            method=self._excel_text(row[ci_method] if ci_method is not None and ci_method<len(row) else None) or '로켓그로스'
            revenue=self._excel_number(row[ci_rev] if ci_rev is not None and ci_rev<len(row) else None,0) or 0
            orders=self._excel_number(row[ci_orders] if ci_orders is not None and ci_orders<len(row) else None,0) or 0
            qty=self._excel_number(row[ci_qty] if ci_qty is not None and ci_qty<len(row) else None,0) or 0
            rocket_sales.prefer_external_sale(self.db.conn,d,model,method)
            exists=self.db.conn.execute('''SELECT 1 FROM sales WHERE sale_date=? AND model=? AND COALESCE(method,'')=? AND revenue=? AND orders=? AND quantity=? LIMIT 1''',(d,model,method,revenue,orders,qty)).fetchone()
            if exists:
                skipped+=1; continue
            self.db.conn.execute('INSERT INTO sales(sale_date,model,method,revenue,orders,quantity) VALUES (?,?,?,?,?,?)',(d,model,method,revenue,orders,qty))
            inserted+=1
        return {'inserted':inserted,'skipped':skipped}

    # ---------------- Fees ----------------
    def _build_fulfillment_rates(self, parent):
        ttk.Label(parent,text='저장된 배송비·입출고비 요금표입니다. 그로스 마진과 로켓그로스 마진 계산에 함께 사용합니다.',foreground='#666',wraplength=850).pack(anchor='w',pady=(0,10))
        tree=self.make_tree(parent,[('tier','판매가 구간'),('size','사이즈'),('ship','배송비'),('inout','입출고비'),('shipreg','기본 배송비'),('inreg','기본 입출고비')],{'tier':190,'size':100})
        self.master_fees_tree=tree
        rows=self.db.query('SELECT * FROM fulfillment_rates ORDER BY min_price,size_type')
        for r in rows: tree.insert('', 'end',values=(r['tier_label'],r['size_type'],money(r['shipping_current']),money(r['inout_current']),money(r['shipping_regular']),money(r['inout_regular'])))

    # ---------------- Data / Settings ----------------
    def build_data(self):
        self.page_header("데이터 · 설정", "API 요청은 이 화면에서만 실행합니다. 다른 화면은 저장된 결과를 보여줍니다.")

        self.data_tabs=ttk.Notebook(self.content)
        self.data_tabs.pack(fill='both',expand=True)
        api_tab=ttk.Frame(self.data_tabs,padding=12)
        self.data_general=ttk.Frame(self.data_tabs,padding=12)
        self.data_tabs.add(api_tab,text='  API 갱신 · 로그  ')
        self.data_tabs.add(self.data_general,text='  파일 · 표시 설정  ')
        display=ttk.LabelFrame(self.data_general,text='표시 설정',padding=14); display.pack(fill='x',pady=(0,12))
        self.display_multitap_only=tk.BooleanVar(value=self.multitap_only_enabled())
        self.display_hide_rocket=tk.BooleanVar(value=self.product_hide_rocket_enabled())
        ttk.Checkbutton(display,text='멀티탭 관련 상품만 표시',variable=self.display_multitap_only,command=self.save_display_settings).pack(anchor='w')
        ttk.Checkbutton(display,text='상품 관리에서 로켓그로스 숨기기',variable=self.display_hide_rocket,command=self.save_display_settings).pack(anchor='w',pady=(9,0))
        ttk.Label(display,text='로켓그로스 숨기기는 상품 관리에만 적용합니다. 재고 현황·일일 매출·API 갱신은 유지됩니다.',foreground='#666').pack(anchor='w',pady=(7,0))
        ttk.Label(display,text='설정은 즉시 저장되며, 체크를 해제하면 다시 보입니다. 상품 데이터는 삭제되지 않습니다.',foreground='#666').pack(anchor='w',pady=(4,0))

        api=ttk.LabelFrame(api_tab,text='쿠팡 API · 이곳에서만 요청',padding=14); api.pack(fill='x',pady=(0,12))
        self.api_vendor_id=tk.StringVar(value=self.db.setting('api_vendor_id',''))
        self.api_access_key=tk.StringVar(value=unprotect_secret(self.db.setting('api_access_key_enc','')))
        self.api_secret_key=tk.StringVar(value=unprotect_secret(self.db.setting('api_secret_key_enc','')))
        ttk.Label(api,text='Vendor ID').grid(row=0,column=0,sticky='w',padx=(0,8),pady=4)
        ttk.Entry(api,textvariable=self.api_vendor_id,width=24).grid(row=0,column=1,sticky='w',pady=4)
        ttk.Label(api,text='Access Key').grid(row=0,column=2,sticky='w',padx=(18,8),pady=4)
        ttk.Entry(api,textvariable=self.api_access_key,width=42,show='*').grid(row=0,column=3,sticky='ew',pady=4)
        ttk.Label(api,text='Secret Key').grid(row=1,column=0,sticky='w',padx=(0,8),pady=4)
        ttk.Entry(api,textvariable=self.api_secret_key,width=54,show='*').grid(row=1,column=1,columnspan=3,sticky='ew',pady=4)
        api.columnconfigure(3,weight=1)
        ttk.Label(api,text='갱신 범위').grid(row=2,column=0,sticky='w',pady=8)
        self.api_scope=tk.StringVar(value=getattr(self,'api_scope',None).get() if hasattr(self,'api_scope') else '전체 (재고 + 매출 + 상품·판매상태)')
        ttk.Combobox(api,textvariable=self.api_scope,state='readonly',width=39,
                     values=['전체 (재고 + 매출 + 상품·판매상태)','로켓그로스 재고만','로켓그로스 일일 매출만','상품정보·판매상태만']).grid(row=2,column=1,columnspan=3,sticky='w')
        yesterday=rocket_sales.today_kst()-timedelta(days=1)
        self.api_sales_from=tk.StringVar(value=self.api_sales_from.get() if hasattr(self,'api_sales_from') else (yesterday-timedelta(days=6)).isoformat())
        self.api_sales_to=tk.StringVar(value=self.api_sales_to.get() if hasattr(self,'api_sales_to') else yesterday.isoformat())
        ttk.Label(api,text='매출 결제일').grid(row=3,column=0,sticky='w',pady=5)
        period=ttk.Frame(api); period.grid(row=3,column=1,columnspan=3,sticky='w')
        ttk.Entry(period,textvariable=self.api_sales_from,width=12).pack(side='left')
        ttk.Label(period,text=' ~ ').pack(side='left')
        ttk.Entry(period,textvariable=self.api_sales_to,width=12).pack(side='left')
        def set_period(days):
            last=rocket_sales.today_kst()-timedelta(days=1)
            self.api_sales_from.set((last-timedelta(days=days-1)).isoformat())
            self.api_sales_to.set(last.isoformat())
        ttk.Button(period,text='전일',command=lambda:set_period(1)).pack(side='left',padx=(8,4))
        ttk.Button(period,text='최근 7일',command=lambda:set_period(7)).pack(side='left')
        ttk.Label(period,text=' YYYY-MM-DD · 최대 30일',foreground='#666').pack(side='left')
        buttons=ttk.Frame(api); buttons.grid(row=4,column=0,columnspan=4,sticky='w',pady=(4,8))
        self.api_save_button=ttk.Button(buttons,text='API 설정 저장',command=self.save_api_settings)
        self.api_save_button.pack(side='left')
        self.api_sync_button=ttk.Button(buttons,text='API 갱신 실행',style='Accent.TButton',command=self.api_sync_all)
        self.api_sync_button.pack(side='left',padx=8)
        self.api_cancel_button=ttk.Button(buttons,text='작업 취소',command=self.api_cancel_sync)
        self.api_cancel_button.pack(side='left')
        self.api_progress=ttk.Progressbar(api,mode='indeterminate')
        self.api_progress.grid(row=5,column=0,columnspan=4,sticky='ew',pady=5)
        ttk.Label(api,textvariable=self.api_status_var,foreground='#245',wraplength=880).grid(row=6,column=0,columnspan=4,sticky='w',pady=(5,8))
        ttk.Label(api,text='전체: 재고 → 일일 매출 → 상품정보·판매상태. 시작 시·주기적 자동 요청은 하지 않습니다.',foreground='#666').grid(row=7,column=0,columnspan=4,sticky='w')
        ttk.Label(api,text='기본 기간은 최근 7일입니다. 재조회로 늦게 출고된 주문을 보충합니다. 하루만 원하면 전일을 누르세요.',foreground='#666').grid(row=8,column=0,columnspan=4,sticky='w',pady=(5,0))
        self._update_api_controls()

        info=ttk.LabelFrame(self.data_general,text='저장 위치',padding=14);info.pack(fill='x',pady=(0,12))
        ttk.Label(info,text=str(DB_PATH)).pack(side='left');ttk.Button(info,text='폴더 열기',command=self.open_data_folder).pack(side='right')
        actions=ttk.LabelFrame(self.data_general,text='백업 / 내보내기',padding=14);actions.pack(fill='x',pady=(0,12))
        ttk.Button(actions,text='DB 백업',command=self.backup_db).pack(side='left',padx=4);ttk.Button(actions,text='Excel 가져오기',style='Accent.TButton',command=self.import_excel_workbook).pack(side='left',padx=4);ttk.Button(actions,text='상품 CSV 내보내기',command=self.export_products_csv).pack(side='left',padx=4);ttk.Button(actions,text='매출 CSV 내보내기',command=self.export_sales_csv).pack(side='left',padx=4)
        ttk.Label(actions,text='현재 쿠팡 관리.xlsm의 등록관리 / 그로스 입고 관리 / 일일 매출 시트를 읽어 병합합니다.',foreground='#666').pack(side='left',padx=(14,0))

        logs=ttk.LabelFrame(api_tab,text='실행 / API 로그',padding=10);logs.pack(fill='both',expand=True,pady=(0,12))
        logbtn=ttk.Frame(logs);logbtn.pack(fill='x',pady=(0,6))
        ttk.Button(logbtn,text='새로고침',command=self.refresh_log_view).pack(side='left')
        ttk.Button(logbtn,text='로그 전체 복사',command=self.copy_log_view).pack(side='left',padx=6)
        ttk.Button(logbtn,text='오늘 로그 열기',command=self.open_log_file).pack(side='left')
        ttk.Button(logbtn,text='로그 폴더 열기',command=self.open_log_folder).pack(side='left',padx=6)
        ttk.Label(logbtn,text=f'오늘 로그 · 2MB x 3개 · 7일 보관  |  {LOG_FILE.name}',foreground='#666').pack(side='right')
        self.log_text=tk.Text(logs,height=8,wrap='none',font=('Consolas',9))
        self.log_text.pack(fill='both',expand=True)
        self.refresh_log_view()

        danger=ttk.LabelFrame(self.data_general,text='초기화',padding=14);danger.pack(fill='x')
        ttk.Label(danger,text='원본 엑셀에서 만든 초기 데이터로 DB를 되돌립니다. 현재 수정 내용은 사라집니다.',foreground='#a00').pack(anchor='w',pady=(0,8));ttk.Button(danger,text='원본 데이터로 초기화',command=self.reset_database).pack(anchor='w')

    def save_display_settings(self):
        enabled = bool(self.display_multitap_only.get()) if hasattr(self,'display_multitap_only') else True
        self.db.set_setting('display_multitap_only', '1' if enabled else '0')
        hidden=bool(self.display_hide_rocket.get())
        self.db.set_setting('product_hide_rocket', '1' if hidden else '0')
        LOGGER.info('표시 설정 변경: 멀티탭 전용=%s, 상품관리 로켓그로스 숨김=%s', enabled, hidden)
        self.show_page('data')
        self.data_tabs.select(self.data_general)

    def _read_log_tail(self, max_lines=250):
        try:
            if not LOG_FILE.exists():
                return "로그가 아직 없습니다."
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-max_lines:])
        except Exception as e:
            return f"로그 읽기 실패: {e}"

    def refresh_log_view(self):
        if not hasattr(self, "log_text") or not self.log_text.winfo_exists():
            return
        text = self._read_log_tail()
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)
        self.log_text.see("end")

    def copy_log_view(self):
        text = self._read_log_tail(2000)
        if self._clipboard_set(text):
            self._set_api_status("최근 로그를 클립보드에 복사했습니다.")

    def open_log_file(self):
        try:
            LOG_FILE.touch(exist_ok=True)
            if os.name == "nt":
                os.startfile(LOG_FILE)
            elif sys.platform == "darwin":
                os.system(f'open "{LOG_FILE}"')
            else:
                os.system(f'xdg-open "{LOG_FILE}" >/dev/null 2>&1 &')
        except Exception:
            LOGGER.exception("로그 파일 열기 실패")
            messagebox.showerror(APP_NAME, f"로그 파일을 열 수 없습니다.\n{LOG_FILE}")

    def open_log_folder(self):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(LOG_DIR)
            elif sys.platform == "darwin":
                os.system(f'open "{LOG_DIR}"')
            else:
                os.system(f'xdg-open "{LOG_DIR}" >/dev/null 2>&1 &')
        except Exception:
            LOGGER.exception("로그 폴더 열기 실패")
            messagebox.showerror(APP_NAME, f"로그 폴더를 열 수 없습니다.\n{LOG_DIR}")

    def _credentials_from_db(self):
        return (
            self.db.setting('api_vendor_id','').strip(),
            unprotect_secret(self.db.setting('api_access_key_enc','')),
            unprotect_secret(self.db.setting('api_secret_key_enc','')),
        )

    def _credentials_from_form(self):
        if hasattr(self,'api_vendor_id'):
            return (self.api_vendor_id.get().strip(), self.api_access_key.get().strip(), self.api_secret_key.get().strip())
        return self._credentials_from_db()

    def save_api_settings(self, silent=False):
        if self.api_busy:
            return False
        vendor, access, secret = self._credentials_from_form()
        if not vendor or not access or not secret:
            if not silent: messagebox.showwarning(APP_NAME,'Vendor ID, Access Key, Secret Key를 모두 입력하세요.')
            return False
        self.db.set_setting('api_vendor_id',vendor)
        self.db.set_setting('api_access_key_enc',protect_secret(access))
        self.db.set_setting('api_secret_key_enc',protect_secret(secret))
        self.db.set_setting('api_auto_sync_minutes',0)
        if not silent: messagebox.showinfo(APP_NAME,'API 설정을 저장했습니다.')
        return True

    def _set_api_status(self, text):
        self.api_last_status = str(text)
        self._update_api_status()

    def _update_api_status(self):
        text = self.api_last_status
        if self.api_busy:
            elapsed = int(time.monotonic()-self.api_started)
            text += f' · 경과 {elapsed}초'
        self.api_status_var.set(text[:500])
        page = getattr(self, 'rocket_inventory_page', None)
        if getattr(self, 'current_page', '') == 'rocket_inventory' and page and page.winfo_exists():
            page.set_status(text[:280])

    def _update_api_controls(self):
        controls = [('api_sync_button', not self.api_busy), ('api_cancel_button', self.api_busy),
                    ('api_save_button', not self.api_busy)]
        for name, enabled in controls:
            widget = getattr(self, name, None)
            if widget is not None and widget.winfo_exists():
                widget.configure(state='normal' if enabled else 'disabled')
        progress = getattr(self, 'api_progress', None)
        if progress is not None and progress.winfo_exists():
            if self.api_busy:
                progress.start(100)
            else:
                progress.stop()

    def api_cancel_sync(self):
        if self.api_busy:
            self.api_cancel_event.set()
            self._set_api_status('취소 요청됨 · 진행 중인 네트워크 요청이 끝나면 중단합니다')

    def api_sync_all(self):
        """The only UI entry point for API requests; one worker owns the whole run."""
        if self.api_busy:
            return
        if not self.save_api_settings(silent=True):
            messagebox.showwarning(APP_NAME, '데이터 · 설정에서 Vendor ID와 API 키를 확인하세요.')
            return
        vendor, access, secret = self._credentials_from_db()
        scope = self.api_scope.get()
        all_scopes=scope.startswith('전체 ')
        do_inventory=all_scopes or scope=='로켓그로스 재고만'
        do_orders=all_scopes or scope=='로켓그로스 일일 매출만'
        do_products=all_scopes or scope=='상품정보·판매상태만'
        if not any((do_inventory,do_orders,do_products)):
            messagebox.showwarning(APP_NAME,'갱신 범위를 선택하세요.')
            return
        sales_from=self.api_sales_from.get().strip()
        sales_to=self.api_sales_to.get().strip()
        if do_orders:
            try: rocket_sales.validate_period(sales_from,sales_to)
            except ValueError as exc:
                messagebox.showwarning(APP_NAME,str(exc)); return
        self.db.conn.commit()
        self.api_busy = True
        self.api_started = time.monotonic()
        self.api_cancel_event = threading.Event()
        cancel_event = self.api_cancel_event
        self._set_api_status('API 갱신 시작')
        self._update_api_controls()
        def worker():
            db2 = None
            results = {}
            cancelled = False
            try:
                api = CoupangAPI(vendor, access, secret)
                api.cancel_event = cancel_event
                api.progress = lambda text: self.api_queue.put(('status', text))
                db2 = Database(DB_PATH, ensure_schema=False)
                # Fetch stock first so hundreds of product requests never delay it.
                if do_inventory:
                    try:
                        api.phase = '로켓그로스 재고'
                        api.progress('로켓그로스 재고 조회 시작')
                        rows = rocket_inventory.fetch_inventory(api, api.progress)
                        api.check_cancelled()
                        stamp = rocket_inventory.save_snapshot(db2.conn, vendor, rows)
                        results['inventory'] = {'ok':True, 'text':f'재고 {len(rows):,}개 옵션 수신'}
                        self.api_queue.put(('inventory_saved', stamp))
                    except CoupangAPICancelled:
                        raise
                    except Exception as exc:
                        LOGGER.exception('통합 갱신: 로켓그로스 재고 실패')
                        db2.set_setting('rg_last_error:'+vendor, str(exc)[:2000])
                        results['inventory'] = {'ok':False, 'text':'재고 실패: '+str(exc)[:350]}
                        api.progress('재고 조회 실패 · 이전 재고 유지')
                api.check_cancelled()
                if do_orders:
                    try:
                        api.progress(f'로켓그로스 일일 매출 조회 {sales_from} ~ {sales_to}')
                        orders=rocket_sales.fetch_orders(api,sales_from,sales_to,api.progress)
                        api.check_cancelled()
                        saved=rocket_sales.save_orders(db2.conn,vendor,orders,sales_from,sales_to)
                        results['orders']={'ok':True,'text':rocket_sales.summary(saved)}
                        LOGGER.info('로켓그로스 일일 매출 저장 기간=%s~%s 결과=%s',sales_from,sales_to,saved)
                        self.api_queue.put(('orders_saved',))
                    except CoupangAPICancelled:
                        raise
                    except Exception as exc:
                        LOGGER.exception('통합 갱신: 로켓그로스 일일 매출 실패')
                        db2.set_setting('rg_orders_last_error:'+vendor,str(exc)[:2000])
                        results['orders']={'ok':False,'text':'일일 매출 실패: '+str(exc)[:350]}
                        api.progress('일일 매출 조회 실패 · 이전 매출 유지')
                api.check_cancelled()
                if do_products:
                    try:
                        api.phase = '상품정보'
                        result = self._sync_product_data(api, db2)
                        results['products'] = result
                        with db2.conn:
                            linked=rocket_sales.rebuild_sales(db2.conn,vendor)
                        if results.get('orders',{}).get('ok'):
                            saved['updated']+=linked['updated']
                            # Recount the selected window after newly imported model links.
                            with db2.conn:
                                selected=rocket_sales.rebuild_sales(db2.conn,vendor,sales_from,sales_to)
                            saved['unmapped']=selected['unmapped']
                            saved['conflicts']=selected['conflicts']
                            results['orders']['text']=rocket_sales.summary(saved)
                            db2.set_setting('rg_orders_last_result:'+vendor,results['orders']['text'])
                    except CoupangAPICancelled:
                        raise
                    except Exception as exc:
                        LOGGER.exception('통합 갱신: 상품정보 실패')
                        results['products'] = {'ok':False, 'text':'상품정보 실패: '+str(exc)[:350]}
            except CoupangAPICancelled:
                cancelled = True
            except Exception as exc:
                LOGGER.exception('통합 API 작업 실패')
                results['job'] = {'ok':False, 'text':'작업 실패: '+str(exc)[:350]}
            finally:
                if db2 is not None:
                    try:
                        db2.close()
                    except Exception:
                        LOGGER.exception('API 작업 DB 닫기 실패')
                self.api_queue.put(('sync_complete', results, cancelled or cancel_event.is_set()))
        self.api_worker = threading.Thread(target=worker, daemon=True, name='CoupangSync')
        self.api_worker.start()

    def _refresh_after_api(self):
        page = getattr(self, 'current_page', '')
        if page == 'rocket_inventory':
            self.rocket_inventory_page.refresh()
        elif page == 'products':
            self.refresh_products()
        elif page == 'product_dashboard':
            self._refresh_product_dashboard_cards()
            self._refresh_product_dashboard_events()
        elif page == 'margin' and getattr(self, 'gross_margin_tree', None):
            if self.gross_margin_tree.winfo_exists():
                self.refresh_gross_margin()
        elif page == 'master':
            self.refresh_model_master()
        elif page == 'data':
            self.refresh_log_view()
        elif page == 'sales':
            self.refresh_sales()

    def _handle_api_message(self, msg):
        kind = msg[0]
        if kind == 'status':
            if not self.api_cancel_event.is_set():
                self._set_api_status(msg[1])
        elif kind == 'inventory_saved':
            if getattr(self, 'current_page', '') == 'rocket_inventory':
                self.rocket_inventory_page.refresh()
        elif kind == 'orders_saved':
            if getattr(self,'current_page','')=='sales':
                self.refresh_sales()
        elif kind == 'sync_complete':
            _, results, cancelled = msg
            # Release busy state before any optional UI refresh can fail.
            self.api_busy = False
            self._update_api_controls()
            parts = [r['text'] for r in results.values()]
            failures = any(not r['ok'] for r in results.values())
            title = 'API 작업 취소' if cancelled else 'API 갱신 일부 실패' if failures else 'API 갱신 완료'
            detail = '\n'.join(parts) or '저장된 새 결과가 없습니다.'
            self._set_api_status(title+' · '+' / '.join(parts))
            try:
                self._refresh_after_api()
            except Exception:
                LOGGER.exception('API 완료 후 화면 갱신 실패')
            self._set_api_status(title+' · '+' / '.join(parts))
            if cancelled:
                detail += '\n이미 완료된 단계의 결과는 저장되어 있습니다.'
            if failures:
                messagebox.showwarning(title, detail+'\n\n실패한 조회의 이전 자료를 유지합니다. 상세 사유는 실행 / API 로그에서 확인하세요.')
            else:
                messagebox.showinfo(title, detail)

    def _poll_api_queue(self):
        try:
            # Keep redraw and input responsive even if many progress events arrive.
            for _ in range(50):
                try:
                    msg = self.api_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_api_message(msg)
                except Exception:
                    LOGGER.exception('API 결과 화면 처리 실패: %s', msg[0])
            if self.api_busy:
                self._update_api_status()
        finally:
            # A view error must never permanently stop delivery of API results.
            try:
                self.after(200, self._poll_api_queue)
            except tk.TclError:
                pass

    @staticmethod
    def _usable_internal_category(value):
        """Return an internal category label, rejecting raw Coupang numeric category codes."""
        v = str(value or '').strip()
        if not v or v.isdigit():
            return ''
        return v

    def _sibling_business_row(self, db, seller_id, option_name, fulfillment_type):
        """Find the counterpart fulfillment-channel row for the same purchase option."""
        if not seller_id or not option_name:
            return None
        if fulfillment_type == '로켓그로스':
            return db.one("""SELECT * FROM products
                           WHERE registered_product_id=? AND option_name=?
                             AND COALESCE(NULLIF(fulfillment_type,''),'판매자배송')='판매자배송'
                           ORDER BY CASE WHEN COALESCE(note,'') LIKE '쿠팡 API%' THEN 1 ELSE 0 END, id
                           LIMIT 1""", (seller_id, option_name))
        return db.one("""SELECT * FROM products
                       WHERE registered_product_id=? AND option_name=?
                         AND COALESCE(fulfillment_type,'')='로켓그로스'
                       ORDER BY CASE WHEN COALESCE(note,'') LIKE '쿠팡 API%' THEN 1 ELSE 0 END, id
                       LIMIT 1""", (seller_id, option_name))

    def _cleanup_legacy_api_duplicates(self, db):
        """Delete only clearly stale duplicate rows created by older API versions."""
        deleted = 0
        api_rows = db.query("""SELECT * FROM products
                             WHERE COALESCE(note,'') LIKE '쿠팡 API%'
                               AND COALESCE(registered_product_id,'')<>''
                               AND COALESCE(option_name,'')<>''
                             ORDER BY id""")
        for row in api_rows:
            fulfillment=str(row['fulfillment_type'] or '판매자배송')
            peers = db.query("""SELECT * FROM products WHERE id<>? AND registered_product_id=?
                              AND option_name=? AND COALESCE(NULLIF(fulfillment_type,''),'판매자배송')=? ORDER BY id""",
                             (row['id'], row['registered_product_id'], row['option_name'], fulfillment))
            rid = str(row['option_id'] or ''); rsid = str(row['seller_product_item_id'] or ''); stale = False
            for peer in peers:
                pid = str(peer['option_id'] or ''); psid = str(peer['seller_product_item_id'] or '')
                if rid and pid and rid == pid: stale = True; break
                if rsid and psid and rsid == psid: stale = True; break
                if not rid and not rsid and (pid or psid): stale = True; break
            if stale:
                db.conn.execute('DELETE FROM products WHERE id=?', (row['id'],)); deleted += 1
                LOGGER.info('구버전 API 중복행 정리 id=%s sellerProductId=%s option=%s',row['id'], row['registered_product_id'], row['option_name'])
        if deleted: db.conn.commit()
        return deleted

    @staticmethod
    def _make_exposure_key(option_id='', seller_item_id='', row_id=None):
        oid=str(option_id or '').strip()
        sid=str(seller_item_id or '').strip()
        if oid:
            return 'v:' + oid
        if sid:
            return 's:' + sid
        return ('r:' + str(row_id)) if row_id is not None else ''

    def _record_product_event(self, db, event_type, row, old_value, new_value, source=''):
        old=str(old_value or '').strip(); new=str(new_value or '').strip()
        if not old or not new or old==new:
            return False
        # Initial population from '미확인' is not a state-change alert; otherwise a
        # first full sync would create thousands of noisy notifications.
        if event_type=='sale_status' and old in ('미확인',''):
            return False
        try:
            row_id=row['id']
        except Exception:
            row_id=None
        def rv(name, default=''):
            try: return row[name]
            except Exception: return default
        option_id=str(rv('option_id') or '').strip()
        seller_item_id=str(rv('seller_product_item_id') or '').strip()
        key=self._make_exposure_key(option_id,seller_item_id,row_id)
        # The current row value actually changed, so one event row is enough. A
        # repeated sync with the same current value never reaches this insert.
        db.conn.execute("""INSERT INTO product_events(
            event_type,product_row_id,option_key,option_id,registered_product_id,
            model,option_name,old_value,new_value,source
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",(
            str(event_type or ''),row_id,key,option_id,str(rv('registered_product_id') or ''),
            str(rv('model') or ''),str(rv('option_name') or ''),old,new,str(source or '')))
        LOGGER.warning('상품 변경 이벤트 type=%s option=%s old=%s new=%s source=%s',event_type,option_id,old,new,source)
        return True

    def _record_exposure(self, db, option_id, seller_item_id, registered_id, exposed_id,
                         model='', option_name='', source='api', row_id=None):
        pid=str(exposed_id or '').strip()
        if not pid:
            return False
        key=self._make_exposure_key(option_id, seller_item_id, row_id)
        if not key:
            return False
        existing=db.query("SELECT exposed_product_id FROM product_exposure_history WHERE option_key=? AND source<>'api-list' ORDER BY id", (key,))
        before={str(x['exposed_product_id'] or '') for x in existing if x['exposed_product_id']}
        db.conn.execute("""INSERT INTO product_exposure_history(
            option_key,option_id,seller_product_item_id,registered_product_id,exposed_product_id,model,option_name,source
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(option_key,exposed_product_id) DO UPDATE SET
            last_seen=CURRENT_TIMESTAMP,
            option_id=excluded.option_id,
            seller_product_item_id=excluded.seller_product_item_id,
            registered_product_id=excluded.registered_product_id,
            model=CASE WHEN excluded.model<>'' THEN excluded.model ELSE product_exposure_history.model END,
            option_name=CASE WHEN excluded.option_name<>'' THEN excluded.option_name ELSE product_exposure_history.option_name END,
            source=CASE
                WHEN product_exposure_history.source='manual' THEN product_exposure_history.source
                WHEN excluded.source='manual' THEN excluded.source
                WHEN product_exposure_history.source LIKE 'revenue%' THEN product_exposure_history.source
                WHEN excluded.source LIKE 'revenue%' THEN excluded.source
                WHEN product_exposure_history.source IN ('existing','sibling','excel-import','api-item','api-single')
                     AND excluded.source='api-list' THEN product_exposure_history.source
                ELSE excluded.source END""",
        (key,str(option_id or ''),str(seller_item_id or ''),str(registered_id or ''),pid,str(model or ''),str(option_name or ''),source))
        changed=bool(before and pid not in before)
        if changed:
            LOGGER.warning('노출상품 ID 변경 감지 option=%s sellerItem=%s old=%s new=%s', option_id, seller_item_id, ','.join(sorted(before)), pid)
        return changed

    def _best_trusted_exposure(self, db, option_id='', seller_item_id='', row_id=None):
        """Return the best known per-option productId, excluding list-level guesses."""
        key=self._make_exposure_key(option_id, seller_item_id, row_id)
        if not key:
            return '', ''
        row=db.one("""SELECT exposed_product_id,source,last_seen FROM product_exposure_history
                      WHERE option_key=? AND COALESCE(exposed_product_id,'')<>'' AND source<>'api-list'
                      ORDER BY CASE
                          WHEN source='manual' THEN 0
                          WHEN source LIKE 'revenue%' THEN 1
                          WHEN source IN ('existing','sibling','excel-import','api-item','api-single') THEN 2
                          ELSE 5 END,
                          last_seen DESC,id DESC LIMIT 1""", (key,))
        if not row:
            return '', ''
        return str(row['exposed_product_id'] or '').strip(), str(row['source'] or '')

    @staticmethod
    def _coupang_product_link(product_id, item_id='', vendor_item_id=''):
        """Build the canonical Coupang product URL from exposed product ID + vendorItemId only."""
        pid=str(product_id or '').strip()
        vid=str(vendor_item_id or '').strip()
        if not pid:
            return ''
        link=f'https://www.coupang.com/vp/products/{pid}'
        if vid:
            link += '?' + urllib.parse.urlencode({'vendorItemId':vid})
        return link

    def _merge_api_products(self, db, details):
        inserted = updated = 0
        fallback_option_ids=set()

        def clean_existing_model(existing, product_name, display_name, general_name):
            """Keep real/manual models, but clear the old API bug that copied product names into model."""
            if not existing:
                return ""
            current = str(existing['model'] or '').strip()
            if not current:
                return ""
            note = str(existing['note'] or '')
            # Only auto-created API rows are eligible for automatic cleanup.
            if not note.startswith('쿠팡 API'):
                return current
            names = {
                str(product_name or '').strip(),
                str(display_name or '').strip(),
                str(general_name or '').strip(),
                str(existing['product_name'] or '').strip(),
                str(existing['display_name'] or '').strip(),
            }
            names.discard('')
            if current in names:
                return ""
            return current

        def channels_from_item(item):
            # Rocket Growth 신규 응답 스키마에서는 한 구매옵션(item) 안에
            # marketplaceItemData와 rocketGrowthItemData가 각각 존재할 수 있다.
            channels = []
            # Coupang has used both spellings in responses/docs. The current
            # hybrid list response uses `marketPlaceItemData` (capital P).
            mp = item.get("marketPlaceItemData") or item.get("marketplaceItemData")
            rg = item.get("rocketGrowthItemData")
            if isinstance(mp, dict) and mp:
                channels.append(("판매자배송", mp, "marketplace"))
            if isinstance(rg, dict) and rg:
                channels.append(("로켓그로스", rg, "rocketGrowth"))
            if not channels:
                # 기존 마켓플레이스 응답 스키마 호환
                channels.append((None, item, "legacy"))
            return channels

        def merged_product_items(detail):
            """Merge detail items with the hybrid list's channel identifiers.

            The seller-product detail endpoint is the primary source. For hybrid
            products its response may contain only rocketGrowthItemData, while the
            Rocket Growth list response contains both marketPlaceItemData and
            rocketGrowthItemData. We borrow only those channel/item identifiers
            from the list response; list-level productId is never promoted to an
            option-level exposed product ID.
            """
            detail_items=[x for x in (detail.get("items") or []) if isinstance(x,dict)]
            list_items=[x for x in (detail.get("_listItems") or []) if isinstance(x,dict)]
            if not list_items:
                return detail_items or [None]
            pools={}
            for d in detail_items:
                pools.setdefault(str(d.get("itemName") or ""), []).append(d)
            merged=[]
            used=set()
            for li in list_items:
                name=str(li.get("itemName") or "")
                candidates=pools.get(name) or []
                di=None
                for cand in candidates:
                    marker=id(cand)
                    if marker not in used:
                        di=cand; used.add(marker); break
                row=dict(li)
                if di:
                    # Detail fields win because detail is the canonical product source.
                    for k,v in di.items():
                        if v not in (None,"",[],{}):
                            row[k]=v
                merged.append(row)
            for d in detail_items:
                if id(d) not in used:
                    merged.append(d)
            return merged or [None]

        for detail in details:
            seller_id = str(detail.get("sellerProductId") or "")
            # If a previous sync could only create a list-level placeholder,
            # remove it once the full detail becomes available.
            if seller_id:
                db.conn.execute(
                    "DELETE FROM products WHERE registered_product_id=? AND note LIKE '쿠팡 API 상세조회 실패%'",
                    (seller_id,)
                )
            detail_product_id = str(detail.get("productId") or "")
            list_product_id = str(detail.get("_listProductId") or "")
            product_name = detail.get("sellerProductName") or ""
            display_name = detail.get("displayProductName") or ""
            category = str(detail.get("productGroup") or "").strip()
            api_status = detail.get("status") or detail.get("statusName") or ""
            items = merged_product_items(detail)
            multi_option = len(items) > 1
            single_top_product_id = (detail_product_id or list_product_id) if not multi_option else ''

            for item in items:
                item = item or {}
                option_name = item.get("itemName") or ""
                for forced_fulfillment, source, channel in channels_from_item(item):
                    vendor_item = source.get("vendorItemId") or item.get("vendorItemId")
                    seller_item = source.get("sellerProductItemId") or item.get("sellerProductItemId")
                    option_id = str(vendor_item or seller_item or "")
                    model = (source.get("modelNo") or source.get("externalVendorSku") or item.get("modelNo") or item.get("externalVendorSku") or "").strip()
                    price_data = source.get("priceData") if isinstance(source.get("priceData"), dict) else {}
                    sale_price = num(price_data.get("salePrice", source.get("salePrice", item.get("salePrice"))), None)
                    # A hybrid list item may omit itemId in marketPlaceItemData while
                    # the same purchase option's rocketGrowthItemData carries it. The
                    # item object represents one purchase option, so share that itemId
                    # across its two fulfillment channels when necessary.
                    mp_data = item.get("marketPlaceItemData") or item.get("marketplaceItemData") or {}
                    rg_data = item.get("rocketGrowthItemData") or {}
                    shared_item_id = item.get("itemId") or mp_data.get("itemId") or rg_data.get("itemId")
                    item_id = source.get("itemId") or shared_item_id
                    # Only item/channel-level productId is unambiguous for an option.
                    # A seller-product/list-level productId cannot represent split/merged
                    # multi-option products, so only single-item products may use it.
                    item_product_id = str(source.get("productId") or item.get("productId") or "").strip()
                    product_id = item_product_id
                    if not product_id and not single_top_product_id and vendor_item:
                        # The primary product-detail API did not resolve an option-level
                        # productId. Mark only this option for the supplemental fallback.
                        fallback_option_ids.add(str(vendor_item))
                    fulfillment_type = forced_fulfillment or "판매자배송"
                    existing = None

                    if vendor_item or seller_item:
                        existing = db.one(
                            """SELECT * FROM products WHERE
                               COALESCE(NULLIF(fulfillment_type,''),'판매자배송')=? AND (
                               (?<>'' AND option_id=?) OR
                               (?<>'' AND COALESCE(seller_product_item_id,'')=?))
                               ORDER BY id LIMIT 1""",
                            (fulfillment_type, str(vendor_item or ''), str(vendor_item or ''), str(seller_item or ''), str(seller_item or ''))
                        )
                    if not existing and seller_id and option_name:
                        if fulfillment_type == "로켓그로스":
                            existing = db.one("SELECT * FROM products WHERE registered_product_id=? AND option_name=? AND COALESCE(fulfillment_type,'')='로켓그로스' ORDER BY id LIMIT 1", (seller_id, option_name))
                        else:
                            existing = db.one("SELECT * FROM products WHERE registered_product_id=? AND option_name=? AND COALESCE(NULLIF(fulfillment_type,''),'판매자배송')='판매자배송' ORDER BY id LIMIT 1", (seller_id, option_name))
                    if not existing and seller_id and len(items) == 1:
                        if fulfillment_type == "로켓그로스":
                            existing = db.one("SELECT * FROM products WHERE registered_product_id=? AND COALESCE(fulfillment_type,'')='로켓그로스' ORDER BY id LIMIT 1", (seller_id,))
                        else:
                            existing = db.one("SELECT * FROM products WHERE registered_product_id=? AND COALESCE(NULLIF(fulfillment_type,''),'판매자배송')='판매자배송' ORDER BY id LIMIT 1", (seller_id,))

                    link = self._coupang_product_link(product_id, item_id, vendor_item)

                    sibling = self._sibling_business_row(db, seller_id, option_name, fulfillment_type)
                    sibling_model = str(sibling['model'] or '').strip() if sibling else ''
                    sibling_category = self._usable_internal_category(sibling['category']) if sibling else ''
                    sibling_gift = sibling['gift'] if sibling else None
                    sibling_coupon = sibling['coupon_price'] if sibling else None
                    sibling_map = sibling['map_price'] if sibling else None
                    sibling_pid = ''
                    if sibling:
                        sibling_pid, _ = self._best_trusted_exposure(
                            db, sibling['option_id'], sibling['seller_product_item_id'], sibling['id'])

                    if existing:
                        # Model column must contain a real model number / vendor SKU only.
                        # Never fall back to sellerProductName/generalProductName.
                        preserved_model = clean_existing_model(existing, product_name, display_name, detail.get('generalProductName'))
                        final_model = model or preserved_model or sibling_model
                        final_category = (self._usable_internal_category(existing['category'])
                                          or sibling_category or self._usable_internal_category(category))
                        existing_pid = str(existing['exposed_product_id'] or '').strip()
                        trusted_pid, trusted_source = self._best_trusted_exposure(
                            db, existing['option_id'], existing['seller_product_item_id'], existing['id'])
                        # Multi-option list/top-level productId is ambiguous. Prefer an
                        # item-level API value, trusted history/manual value, or the
                        # counterpart channel's trusted value.
                        final_product_id = product_id or trusted_pid or sibling_pid or single_top_product_id
                        exposure_source = ('api-item' if product_id else
                                           (trusted_source if trusted_pid else
                                            ('sibling' if sibling_pid else
                                             ('api-single' if single_top_product_id else ''))))
                        link = self._coupang_product_link(final_product_id, item_id, vendor_item)
                        existing_link = str(existing['link'] or '').strip()
                        if link and (not existing_link or 'coupang.com/vp/products/' in existing_link):
                            final_link = link
                        else:
                            final_link = existing_link or link
                        final_gift = existing['gift'] if existing['gift'] not in (None, '') else sibling_gift
                        final_coupon = existing['coupon_price'] if existing['coupon_price'] is not None else sibling_coupon
                        final_map = existing['map_price'] if existing['map_price'] not in (None, 0) else sibling_map
                        effective_sale = sale_price if sale_price is not None else existing['sale_price']
                        final_diff = existing['price_diff']
                        if effective_sale is not None and final_map is not None:
                            final_diff = effective_sale - final_map
                        final_status = str(existing['status'] or ('로켓그로스' if fulfillment_type=='로켓그로스' else '판매중'))
                        db.conn.execute('''UPDATE products SET category=?,fulfillment_type=?,status=?,model=?,product_name=?,display_name=?,registered_product_id=?,exposed_product_id=?,option_name=?,option_id=?,seller_product_item_id=?,item_id=?,gift=?,sale_price=COALESCE(?,sale_price),coupon_price=?,map_price=?,price_diff=?,link=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                                        (final_category, fulfillment_type, final_status, final_model, product_name, display_name, seller_id, final_product_id, option_name, str(vendor_item or option_id or ''), str(seller_item or ''), str(item_id or ''), final_gift, sale_price, final_coupon, final_map, final_diff, final_link, existing['id']))
                        if final_product_id and exposure_source:
                            self._record_exposure(db, str(vendor_item or option_id or ''), str(seller_item or ''),
                                                  seller_id, final_product_id, final_model, option_name, exposure_source, existing['id'])
                            if existing_pid and existing_pid != final_product_id and exposure_source in ('api-item','api-single'):
                                self._record_product_event(db,'exposure_id',existing,existing_pid,final_product_id,exposure_source)
                        updated += 1
                        LOGGER.debug("상품 업데이트 channel=%s sellerProductId=%s option=%s vendorItemId=%s sellerProductItemId=%s productId=%s listProductId=%s finalProductId=%s row=%s", channel, seller_id, option_name, vendor_item, seller_item, product_id, list_product_id, final_product_id, existing['id'])
                    else:
                        # A Rocket Growth row is another fulfillment channel for the same option.
                        # Inherit our internal management fields from the marketplace/manual sibling.
                        final_model = model or sibling_model
                        final_category = sibling_category or self._usable_internal_category(category)
                        final_product_id = product_id or sibling_pid or single_top_product_id
                        exposure_source = ('api-item' if product_id else
                                           ('sibling' if sibling_pid else
                                            ('api-single' if single_top_product_id else '')))
                        link = self._coupang_product_link(final_product_id, item_id, vendor_item)
                        final_gift = sibling_gift
                        final_coupon = sibling_coupon
                        final_map = sibling_map
                        final_diff = None
                        if sale_price is not None and final_map is not None:
                            final_diff = sale_price - final_map
                        note = '쿠팡 API 자동등록' + (' (로켓그로스)' if fulfillment_type == '로켓그로스' else '')
                        if not final_model:
                            note += ' / 모델번호·판매자상품코드 미등록'
                        cur = db.conn.execute('''INSERT INTO products(category,fulfillment_type,status,model,product_name,display_name,registered_product_id,exposed_product_id,option_name,option_id,seller_product_item_id,item_id,gift,sale_price,coupon_price,map_price,price_diff,link,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                        (final_category, fulfillment_type, ('로켓그로스' if fulfillment_type=='로켓그로스' else '판매중'), final_model, product_name, display_name, seller_id, final_product_id, option_name, str(vendor_item or option_id or ''), str(seller_item or ''), str(item_id or ''), final_gift, sale_price, final_coupon, final_map, final_diff, link, note))
                        if final_product_id and exposure_source:
                            self._record_exposure(db, str(vendor_item or option_id or ''), str(seller_item or ''),
                                                  seller_id, final_product_id, final_model, option_name, exposure_source, cur.lastrowid)
                        inserted += 1
                        LOGGER.info("상품 신규 channel=%s sellerProductId=%s option=%s vendorItemId=%s sellerProductItemId=%s productId=%s listProductId=%s finalProductId=%s", channel, seller_id, option_name, vendor_item, seller_item, product_id, list_product_id, final_product_id)
                    image_url = rocket_inventory.product_image_url(item)
                    if image_url:
                        db.conn.execute('UPDATE products SET image_url=? WHERE id=?',
                                        (image_url, existing['id'] if existing else cur.lastrowid))
        db.conn.commit()
        cleaned = self._cleanup_legacy_api_duplicates(db)
        if cleaned:
            LOGGER.info('구버전 API 중복행 총 %s건 정리', cleaned)
        return inserted, updated, sorted(fallback_option_ids)

    def _apply_revenue_exposure_map(self, db, exposure_result):
        """Apply option-level productIds learned from revenue history.

        Every observed mapping is retained in product_exposure_history. The most
        recent mapping becomes the currently displayed exposed product ID.
        """
        latest=(exposure_result or {}).get('latest') or {}
        history=(exposure_result or {}).get('history') or {}
        changed=0; touched=0
        for vid, by_pid in history.items():
            rows=db.query('SELECT * FROM products WHERE option_id=? ORDER BY id', (str(vid),))
            if not rows:
                continue
            for pid, seen_date in (by_pid or {}).items():
                if not pid or str(pid)=='0':
                    continue
                for row in rows:
                    self._record_exposure(
                        db, row['option_id'], row['seller_product_item_id'],
                        row['registered_product_id'], str(pid), row['model'], row['option_name'],
                        'revenue:' + str(seen_date or ''), row['id'])
            current=(latest.get(str(vid)) or {})
            new_pid=str(current.get('productId') or '').strip()
            seen_date=str(current.get('seenDate') or '')
            if not new_pid:
                continue
            for row in rows:
                # An explicit manual correction remains authoritative until the
                # user changes it; otherwise newest revenue mapping wins.
                manual=db.one("""SELECT exposed_product_id FROM product_exposure_history
                                 WHERE option_key=? AND source='manual'
                                 ORDER BY last_seen DESC,id DESC LIMIT 1""",
                              (self._make_exposure_key(row['option_id'], row['seller_product_item_id'], row['id']),))
                if manual and str(manual['exposed_product_id'] or '').strip() and str(row['exposed_product_id'] or '').strip()==str(manual['exposed_product_id'] or '').strip():
                    continue
                old_pid=str(row['exposed_product_id'] or '').strip()
                item_id=str(row['item_id'] or '').strip() if 'item_id' in row.keys() else ''
                new_link=self._coupang_product_link(new_pid, item_id, row['option_id'])
                old_link=str(row['link'] or '').strip()
                final_link=new_link if new_link and (not old_link or 'coupang.com/vp/products/' in old_link) else old_link
                db.conn.execute('UPDATE products SET exposed_product_id=?,link=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                (new_pid, final_link, row['id']))
                touched += 1
                if old_pid != new_pid:
                    changed += 1
                    if old_pid:
                        self._record_product_event(db,'exposure_id',row,old_pid,new_pid,'revenue:' + seen_date)
                    LOGGER.info('매출내역 노출ID 보정 option=%s old=%s new=%s seen=%s row=%s',
                                vid, old_pid, new_pid, seen_date, row['id'])
        db.conn.commit()
        return touched, changed

    def _merge_failed_list_products(self, db, failed_products):
        """Preserve list-level product info when seller-product detail temporarily fails."""
        inserted = updated = 0
        for entry in failed_products:
            p = entry.get('product') or {}
            sid = str(p.get('sellerProductId') or '')
            if not sid: continue
            name = p.get('sellerProductName') or p.get('displayProductName') or ''
            product_id = str(p.get('productId') or '')
            category = ''
            api_status = p.get('statusName') or p.get('status') or ''  # 등록/승인상태일 뿐 판매상태가 아님
            source_list=list(p.get('_sources') or []); sources=','.join(source_list)
            rows = db.query('SELECT * FROM products WHERE registered_product_id=? ORDER BY id', (sid,))
            if rows:
                for row in rows:
                    current_model = str(row['model'] or '').strip(); row_note = str(row['note'] or '')
                    if row_note.startswith('쿠팡 API') and current_model and current_model in {name, str(row['product_name'] or '').strip(), str(row['display_name'] or '').strip()}:
                        current_model = ''
                    db.conn.execute(
                        """UPDATE products SET
                           model=?,
                           product_name=CASE WHEN ?<>'' THEN ? ELSE product_name END,
                           category=CASE WHEN COALESCE(category,'')='' AND ?<>'' THEN ? ELSE category END,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (current_model, name, name, category, category, row['id'])
                    )
                    updated += 1
                LOGGER.warning('상세조회 실패 상품 기존행 보존 sellerProductId=%s name=%s registrationStatus=%s sources=%s rows=%s error=%s',sid, name, api_status, sources, len(rows), entry.get('error',''))
            else:
                fulfillment = '로켓그로스' if source_list == ['rocketGrowth'] else '판매자배송'
                model = ''; note = '쿠팡 API 상세조회 실패 - 목록정보 임시등록 / 모델명 미확인'
                if sources: note += f' ({sources})'
                db.conn.execute(
                    """INSERT INTO products(
                       category,fulfillment_type,status,model,product_name,display_name,registered_product_id,
                       exposed_product_id,option_name,option_id,link,note
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (category, fulfillment, ('로켓그로스' if fulfillment=='로켓그로스' else '판매중'), model, name, '', sid, product_id, '[상세조회 실패]', '', '', note)
                )
                inserted += 1
                LOGGER.warning('상세조회 실패 상품 placeholder 생성 sellerProductId=%s name=%s registrationStatus=%s sources=%s error=%s',sid, name, api_status, sources, entry.get('error',''))
        db.conn.commit()
        return inserted, updated

    @staticmethod
    def _sales_status_from_inventory(fulfillment_type, data):
        """Translate the option inventory/status response into the app's sale status."""
        if not isinstance(data, dict) or 'onSale' not in data:
            return '미확인'
        raw_on_sale=data.get('onSale')
        if isinstance(raw_on_sale,str):
            t=raw_on_sale.strip().lower()
            if t in ('true','1','y','yes'): on_sale=True
            elif t in ('false','0','n','no'): on_sale=False
            else: return '미확인'
        elif isinstance(raw_on_sale,(bool,int)):
            on_sale=bool(raw_on_sale)
        else:
            return '미확인'
        if not on_sale:
            return '판매중지'
        # Marketplace options with an explicitly reported zero sellable inventory are
        # sold out. Rocket Growth warehouse stock is tracked by the separate RG inventory
        # API, so do not infer RG 품절 from this generic field alone.
        if str(fulfillment_type or '판매자배송') == '판매자배송' and data.get('amountInStock') is not None:
            try:
                if int(data.get('amountInStock')) <= 0:
                    return '품절'
            except Exception:
                pass
        return '판매중'

    def _refresh_sale_status_from_api(self, api, db, option_ids=None, progress=None):
        """Query vendorItem sale status and persist successful results immediately.

        Inventory API errors are *not* sale statuses.  In particular, Coupang can
        intermittently return "유효한 옵션이 없습니다" for a vendorItemId that is
        still present in product data.  Keep the last known sale_status and record
        the query error separately instead of converting the row to a fake state.
        """
        if option_ids is None:
            rows=db.query("""SELECT DISTINCT option_id FROM products
                             WHERE COALESCE(option_id,'')<>''
                             ORDER BY option_id""")
            option_ids=[str(r['option_id']) for r in rows]
        option_ids=list(dict.fromkeys(str(x or '').strip() for x in option_ids if str(x or '').strip()))
        checked=changed=errors=0
        stamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total=len(option_ids)
        for idx,vid in enumerate(option_ids,1):
            api.check_cancelled()
            api.phase=f'판매상태 {idx}/{total}'
            try:
                data=api.get_item_inventory(vid)
                target_rows=db.query("SELECT * FROM products WHERE option_id=?",(vid,))
                for row in target_rows:
                    fulfillment=str(row['fulfillment_type'] or '판매자배송')
                    new_status=self._sales_status_from_inventory(fulfillment,data)
                    old_status=str(row['sale_status'] or '')
                    qty=data.get('amountInStock')
                    try: qty=int(qty) if qty is not None else None
                    except Exception: qty=None
                    db.conn.execute("""UPDATE products
                                       SET sale_status=?,api_stock_quantity=?,sale_status_checked_at=?,
                                           sale_status_error=NULL,sale_status_error_at=NULL,updated_at=CURRENT_TIMESTAMP
                                       WHERE id=?""",
                                    (new_status,qty,stamp,row['id']))
                    if old_status != new_status:
                        changed += 1
                        self._record_product_event(db,'sale_status',row,old_status,new_status,'inventory-api')
                        LOGGER.info('판매상태 변경 option=%s fulfillment=%s old=%s new=%s stock=%s',vid,fulfillment,old_status,new_status,qty)
                checked += 1
                db.conn.commit()
            except Exception as e:
                if getattr(e, 'stop_sync', False):
                    raise
                errors += 1
                message=str(e)[:500]
                target_rows=db.query("SELECT id FROM products WHERE option_id=?",(vid,))
                for row in target_rows:
                    db.conn.execute("""UPDATE products
                                       SET sale_status_error=?,sale_status_error_at=?,updated_at=CURRENT_TIMESTAMP
                                       WHERE id=?""",(message,stamp,row['id']))
                db.conn.commit()
                LOGGER.warning('옵션 판매상태 조회 실패 - 기존 상태 유지 vendorItemId=%s error=%s',vid,e)
            if progress and (idx==1 or idx==total or idx%10==0):
                progress(f'판매상태 확인 {idx}/{total} · 변경 {changed} · 조회오류 {errors}')
            api.wait(0.04)
        db.conn.commit()
        return {'checked':checked,'changed':changed,'errors':errors,'total':total,'checkedAt':stamp}

    def _sync_product_data(self, api, db2):
        LOGGER.info("상품 동기화 시작 vendor=%s", api.vendor_id)
        marketplace=api.get_all_products(lambda msg:self.api_queue.put(('status',msg)), label="판매자배송/하이브리드")
        rocket=api.get_all_products(lambda msg:self.api_queue.put(('status',msg)), business_types=["rocketGrowth"], label="로켓그로스/하이브리드")
        by_id={}
        for source_name, rows in (("marketplace", marketplace), ("rocketGrowth", rocket)):
            for p in rows:
                sid=str(p.get('sellerProductId') or '')
                if not sid:
                    continue
                if sid not in by_id:
                    by_id[sid]=dict(p)
                    by_id[sid]['_sources']=[source_name]
                    first_pid=str(p.get('productId') or '').strip()
                    by_id[sid]['_listProductIds']=[first_pid] if first_pid else []
                else:
                    cur=by_id[sid]
                    srcs=cur.setdefault('_sources',[])
                    pid=str(p.get('productId') or '').strip()
                    pids=cur.setdefault('_listProductIds',[])
                    if pid and pid not in pids:
                        pids.append(pid)
                    if source_name not in srcs:
                        srcs.append(source_name)
                    for k,v in p.items():
                        if v not in (None,'',[],{}) and cur.get(k) in (None,'',[],{}):
                            cur[k]=v
        products=list(by_id.values())
        LOGGER.info("목록 합산 marketplace=%s rocketGrowth=%s unique=%s", len(marketplace), len(rocket), len(products))
        details=[]; errors=[]; failed_products=[]
        total=len(products)
        for i,p in enumerate(products,1):
            api.check_cancelled()
            api.phase=f'상품 상세 {i}/{total}'
            sid=p.get('sellerProductId')
            if not sid: continue
            try:
                detail=api.get_product(sid)
                list_pids=[str(x) for x in (p.get('_listProductIds') or []) if str(x).strip()]
                if len(list_pids)>1:
                    LOGGER.warning("목록 API 노출상품 ID 복수 sellerProductId=%s ids=%s", sid, ','.join(list_pids))
                detail['_listProductId'] = list_pids[0] if list_pids else str(p.get('productId') or '')
                detail['_listProductIds'] = list_pids
                detail['_listItems'] = p.get('items') or []
                details.append(detail)
            except Exception as e:
                if getattr(e, 'stop_sync', False):
                    raise
                sources=','.join(p.get('_sources') or [])
                name=p.get('sellerProductName') or ''
                status=p.get('statusName') or p.get('status') or ''
                err=f'{sid}: {e} / name={name} / status={status} / sources={sources}'
                errors.append(err)
                failed_products.append({'product': p, 'error': str(e)})
                LOGGER.exception(
                    "상품 상세조회 실패 sellerProductId=%s name=%s status=%s sources=%s",
                    sid, name, status, sources
                )
            if i==1 or i==total or i%10==0:
                self.api_queue.put(('status',f'상품 상세 {i}/{total} 조회 중...'))
            # 상세조회 burst를 조금 완화해 쿠팡 게이트웨이의 간헐적 DEFAULT 400을 줄인다.
            api.wait(0.08)

        # 첫 패스에서 끝까지 실패한 상품은 전체 순회가 끝난 뒤 한 번 더 시도한다.
        # 같은 sellerProductId가 이전 동기화에서는 성공하고 다음 동기화에서 400이
        # 발생한 로그가 있어, 고정 불량 상품보다 일시적인 게이트웨이 오류 가능성이 크다.
        if failed_products:
            self.api_queue.put(('status',f'상세조회 실패 {len(failed_products)}건 재확인 중...'))
            LOGGER.info("상세조회 2차 재시도 시작 count=%s", len(failed_products))
            retry_failed=[]; retry_errors=[]
            api.wait(2.0)
            for idx,entry in enumerate(failed_products,1):
                p=entry.get('product') or {}
                sid=p.get('sellerProductId')
                try:
                    detail=api.get_product(sid)
                    list_pids=[str(x) for x in (p.get('_listProductIds') or []) if str(x).strip()]
                    detail['_listProductId'] = list_pids[0] if list_pids else str(p.get('productId') or '')
                    detail['_listProductIds'] = list_pids
                    detail['_listItems'] = p.get('items') or []
                    details.append(detail)
                    LOGGER.info("상세조회 2차 재시도 성공 sellerProductId=%s", sid)
                except Exception as e:
                    if getattr(e, 'stop_sync', False):
                        raise
                    sources=','.join(p.get('_sources') or [])
                    name=p.get('sellerProductName') or ''
                    status=p.get('statusName') or p.get('status') or ''
                    retry_failed.append({'product':p,'error':str(e)})
                    retry_errors.append(f'{sid}: {e} / name={name} / status={status} / sources={sources}')
                    LOGGER.warning("상세조회 2차 재시도 실패 sellerProductId=%s name=%s", sid, name)
                if idx % 5 == 0:
                    self.api_queue.put(('status',f'실패 상품 재확인 {idx}/{len(failed_products)}...'))
                api.wait(0.25)
            failed_products=retry_failed
            errors=retry_errors
            LOGGER.info("상세조회 2차 재시도 완료 최종실패=%s", len(failed_products))

        api.check_cancelled()
        ins,upd,fallback_option_ids=self._merge_api_products(db2,details)
        fb_ins,fb_upd=self._merge_failed_list_products(db2,failed_products)
        ins += fb_ins
        upd += fb_upd
        try:
            model_repair=self._auto_repair_model_links(db2)
            LOGGER.info('상품 동기화 후 모델 자동연결 linked=%s pending=%s',model_repair.get('linked',0),model_repair.get('pendingLinked',0))
        except Exception:
            LOGGER.exception('상품 동기화 후 모델 자동연결 실패')

        # 상품조회 API가 메인 데이터 소스다. 상품조회가 옵션/channel 레벨
        # productId를 직접 주지 못한 옵션만 fallback 검증 대상으로 보낸다.
        # 기존 엑셀 ID가 있더라도 최근 매출에서 더 최신 productId가 확인되면
        # 머지/분리 변경을 반영할 수 있다.
        exposure_touched=0; exposure_changed=0; warnings=[]
        try:
            fallback_ids=[str(x) for x in fallback_option_ids if str(x).strip()]
            if db2.setting('display_multitap_only','1') == '1' and fallback_ids:
                clause=self._multitap_product_clause('p')
                allowed={str(r['option_id']) for r in db2.query(
                    f"SELECT DISTINCT p.option_id FROM products p WHERE COALESCE(p.option_id,'')<>'' AND {clause}")
                    if str(r['option_id'] or '').strip()}
                fallback_ids=[x for x in fallback_ids if x in allowed]
            if fallback_ids:
                self.api_queue.put(('status',f'상품조회 완료 / 옵션별 productId 미제공 {len(fallback_ids)}개만 fallback 검증 중...'))
                recent=api.get_revenue_exposure_map(
                    fallback_ids, lookback_days=31,
                    progress=lambda msg:self.api_queue.put(('status','fallback · '+msg)))
                t,c=self._apply_revenue_exposure_map(db2,recent)
                exposure_touched += t; exposure_changed += c

                # 최근 매출에도 없고 현재 노출ID까지 비어 있는 옵션만 최초 1회
                # 186일까지 확장한다. 기존 값이 있는 옵션 때문에 긴 과거 조회를
                # 매번 돌지는 않는다.
                if db2.setting('exposure_fallback_bootstrap_v0213_done','0') != '1':
                    unresolved_recent={str(x) for x in (recent.get('unresolved') or []) if str(x).strip()}
                    remaining=[]
                    for oid in fallback_ids:
                        if oid not in unresolved_recent:
                            continue
                        row=db2.one("SELECT exposed_product_id FROM products WHERE option_id=? ORDER BY id LIMIT 1", (oid,))
                        if not row or not str(row['exposed_product_id'] or '').strip():
                            remaining.append(oid)
                    if remaining:
                        self.api_queue.put(('status',f'미확인 노출ID 초기 보완: {len(remaining)}개 과거 내역 확인 중...'))
                        historic=api.get_revenue_exposure_map(
                            remaining, lookback_days=186,
                            progress=lambda msg:self.api_queue.put(('status','fallback · '+msg)))
                        t,c=self._apply_revenue_exposure_map(db2,historic)
                        exposure_touched += t; exposure_changed += c
                    db2.set_setting('exposure_fallback_bootstrap_v0213_done','1')
                    db2.set_setting('exposure_fallback_bootstrap_v0213_at',datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                LOGGER.info('상품조회 미제공 옵션 fallback 완료 candidates=%s touched=%s changed=%s', len(fallback_ids), exposure_touched, exposure_changed)
            else:
                LOGGER.info('상품조회 API가 옵션별 노출상품 ID를 직접 제공했거나 fallback 대상 없음')
        except Exception as e:
            if getattr(e, 'stop_sync', False):
                raise
            # Fallback failure must never invalidate the product-detail sync.
            LOGGER.exception('옵션별 노출ID fallback 실패: %s', e)
            warnings.append('노출 ID 보완 실패')
            self.api_queue.put(('status','상품조회 동기화 완료 / 옵션별 노출ID fallback만 실패하여 기존 값 유지'))

        sale_status_result={'checked':0,'changed':0,'errors':0,'total':0,'checkedAt':''}
        try:
            self.api_queue.put(('status','상품정보 반영 완료 / 옵션별 판매상태 확인 중...'))
            sale_status_result=self._refresh_sale_status_from_api(
                api,db2,progress=lambda msg:self.api_queue.put(('status','판매상태 · '+msg)))
            db2.set_setting('api_last_sale_status_sync',sale_status_result.get('checkedAt',''))
        except Exception as e:
            if getattr(e, 'stop_sync', False):
                raise
            LOGGER.exception('상품 동기화 후 판매상태 확인 실패: %s',e)
            warnings.append('판매상태 확인 실패')
            self.api_queue.put(('status','상품정보 동기화 완료 / 판매상태 확인만 실패하여 기존 상태 유지'))

        stamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db2.set_setting('api_last_sync',stamp)
        db2.set_setting('api_last_sync_total',len(details))
        db2.set_setting('api_last_sync_errors',len(errors))
        db2.set_setting('api_last_exposure_changed',exposure_changed)
        if errors:
            LOGGER.error("동기화 상세조회 오류 %s건:\n%s", len(errors), "\n".join(errors))
        LOGGER.info(
            "상품 동기화 완료 신규=%s 업데이트=%s 상세=%s 오류=%s fallback신규=%s fallback보존=%s",
            ins, upd, len(details), len(errors), fb_ins, fb_upd
        )
        return {'ok':not errors and not sale_status_result.get('errors',0) and not warnings,
                'text':f'상품 {len(details)}개 · 상세 오류 {len(errors)}개 · 판매상태 오류 {sale_status_result.get("errors",0)}개' + (' · '+', '.join(warnings) if warnings else '')}

    def open_data_folder(self):
        if os.name=='nt': os.startfile(DATA_DIR)
        elif sys.platform=='darwin': os.system(f'open "{DATA_DIR}"')
        else: os.system(f'xdg-open "{DATA_DIR}" >/dev/null 2>&1 &')

    def backup_db(self):
        p=filedialog.asksaveasfilename(defaultextension='.db',initialfile=f'coupang_backup_{date.today().isoformat()}.db',filetypes=[('SQLite DB','*.db')])
        if p: self.db.backup_to(p);messagebox.showinfo(APP_NAME,'백업이 완료되었습니다.')

    def export_products_csv(self):
        p=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='등록상품.csv',filetypes=[('CSV','*.csv')])
        if not p:return
        rows=self.db.query('SELECT fulfillment_type,sale_status,category,model,product_name,display_name,registered_product_id,exposed_product_id,option_name,option_id,seller_product_item_id,gift,sale_price,coupon_price,map_price,price_diff,link,note FROM products ORDER BY model,id')
        headers=['배송방식','판매상태','분류','모델명','상품명','노출명','등록상품 ID','노출상품 ID','옵션명','옵션 ID','업체옵션 ID','사은품','판매가','쿠폰적용가','지도가','지도가대비판매가','링크','비고']
        with open(p,'w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(headers);w.writerows([tuple(r) for r in rows])
        messagebox.showinfo(APP_NAME,'상품 CSV 내보내기가 완료되었습니다.')

    def reset_database(self):
        if not messagebox.askyesno(APP_NAME,'정말 원본 엑셀 기준 초기 데이터로 되돌릴까요?\n현재 수정한 데이터는 삭제됩니다.'):
            return
        self.db.close(); shutil.copy2(STARTER_DB,DB_PATH); self.db=Database(DB_PATH); messagebox.showinfo(APP_NAME,'초기화했습니다.'); self.show_page('product_dashboard')

    def on_close(self):
        if hasattr(self, 'rg_thumbnails'):
            self.rg_thumbnails.close()
        self.api_cancel_event.set()
        for job in self.tk.call('after', 'info'):
            try: self.after_cancel(job)
            except tk.TclError: pass
        try:self.db.close()
        finally:self.destroy()


if __name__ == '__main__':
    try:
        app=CoupangManager(); app.mainloop()
    except Exception as e:
        LOGGER.exception("프로그램 실행 중 치명적 오류")
        try: messagebox.showerror(APP_NAME,f'프로그램 실행 중 오류가 발생했습니다.\n\n{e}\n\n로그: {LOG_FILE}')
        except Exception: print(e)
        raise
