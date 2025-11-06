"""
Bot_22_A.py - Enhanced Trading Bot with Comprehensive Fixes

LATEST CHANGES (2025-09-27 - Comprehensive Fixes):
1. ESL PLACEMENT: Fixed silent ESL placement failures with proper error handling
2. FLAT SIGNAL SPAM: Added deduplication to prevent "FLAT SIGNAL: Cancelling X orders" spam
3. POSITION STATUS TP: Enhanced to show calculated trailing TP when active  
4. TRAILING ACTIVATION: Added trailing activation price display when not activated
5. ENHANCED LOGGING: Better error visibility and monitoring

PREVIOUS CHANGES:
- Signal deduplication to prevent "SIGNAL IGNORED" spam (lines ~1672-1690)
- Fixed exit order partial fill calculation (lines ~2463-2490)
- Added ESL fill circuit breaker to prevent race conditions
- Enhanced logging for partial fill monitoring

BACKUPS: 
- bot_22_A.py.backup_before_comprehensive_fixes
- bot_22_A.py.backup_before_signal_dedup_fix
- bot_22_A.py.backup_before_partial_fill_fix
"""

import asyncio, json, hmac, logging, time, os, urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from asyncio import Lock
import math

import aiohttp
from maker_engine import PegPriceResolver, MakerOrderController, MakerTuning  # type: ignore
from volatility_monitor import VolatilityMonitor, OrderStateTracker, VolatilityConfig  # type: ignore


class AggressiveRestLimiter:
	"""Aggressive but safe REST API rate limiter - 85% of Binance limits"""
	def __init__(self):
		# 85% of Binance limits for safety margin
		self.requests_per_second = 17.0    # 85% of 20/sec
		self.orders_per_second = 25.0      # 85% of 30/sec  
		self.weight_per_second = 34.0      # 85% of 40/sec
		
		# Small burst allowance for immediate responses
		self.burst_requests = 5
		self.burst_orders = 3
		self.burst_weight = 10
		
		# Token buckets
		self.request_tokens = float(self.burst_requests)
		self.order_tokens = float(self.burst_orders)
		self.weight_tokens = float(self.burst_weight)
		
		self.last_update = time.time()
		self.lock = asyncio.Lock()
	
	async def acquire(self, is_order: bool = False, weight: float = 1.0):
		"""Acquire permission for REST API call with micro-delays only when needed"""
		async with self.lock:
			now = time.time()
			elapsed = now - self.last_update
			
			# Replenish tokens
			self.request_tokens = min(self.burst_requests, self.request_tokens + elapsed * self.requests_per_second)
			self.order_tokens = min(self.burst_orders, self.order_tokens + elapsed * self.orders_per_second)
			self.weight_tokens = min(self.burst_weight, self.weight_tokens + elapsed * self.weight_per_second)
			self.last_update = now
			
			# Check if we can proceed immediately
			can_proceed = (self.request_tokens >= 1.0 and 
						  self.weight_tokens >= weight and
						  (not is_order or self.order_tokens >= 1.0))
			
			if can_proceed:
				# Consume tokens and proceed immediately
				self.request_tokens -= 1.0
				self.weight_tokens -= weight
				if is_order:
					self.order_tokens -= 1.0
				return
			
			# Calculate micro-delay (50-100ms only when limits hit)
			wait_time = 0.0
			if self.request_tokens < 1.0:
				wait_time = max(wait_time, (1.0 - self.request_tokens) / self.requests_per_second)
			if self.weight_tokens < weight:
				wait_time = max(wait_time, (weight - self.weight_tokens) / self.weight_per_second)
			if is_order and self.order_tokens < 1.0:
				wait_time = max(wait_time, (1.0 - self.order_tokens) / self.orders_per_second)
			
			if wait_time > 0:
				# Minimal delay only when necessary
				await asyncio.sleep(min(wait_time, 0.1))  # Cap at 100ms
				# Consume tokens after wait
				self.request_tokens = max(0, self.request_tokens - 1.0)
				self.weight_tokens = max(0, self.weight_tokens - weight)
				if is_order:
					self.order_tokens = max(0, self.order_tokens - 1.0)


# Global aggressive rate limiter
_aggressive_rate_limiter = AggressiveRestLimiter()


BINANCE_FAPI_BASE = "https://fapi.binance.com"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SIGNALS_PATH = BASE_DIR / "signals.json"
USER_STATE_DIR = BASE_DIR / "user_state"
LOG_DIR = BASE_DIR / "logs"

USER_STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

SIGNAL_LOCK = Lock()  # for concurrent signals.json access

# Central detailed logger
DETAILED_LOG = logging.getLogger("bot_detailed")
DETAILED_LOG.setLevel(logging.DEBUG)
fh = logging.FileHandler(LOG_DIR / "bot_detailed.log")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
DETAILED_LOG.addHandler(fh)

# NEW: Enhanced exit order tracking logger
EXIT_LOG = logging.getLogger("exit_orders")
EXIT_LOG.setLevel(logging.INFO)
exit_fh = logging.FileHandler(LOG_DIR / "exit_orders.log")
exit_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
EXIT_LOG.addHandler(exit_fh)

# NEW: Enhanced entry order tracking logger
ENTRY_LOG = logging.getLogger("entry_orders")
ENTRY_LOG.setLevel(logging.INFO)
entry_fh = logging.FileHandler(LOG_DIR / "entry_orders.log")
entry_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
ENTRY_LOG.addHandler(entry_fh)

# NEW: Structured trade events logger
TRADE_EVENTS_LOG = logging.getLogger("trade_events")
TRADE_EVENTS_LOG.setLevel(logging.INFO)
trade_events_fh = logging.FileHandler(LOG_DIR / "trade_events.log")
trade_events_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
TRADE_EVENTS_LOG.addHandler(trade_events_fh)

# NEW: Position tracking logger
POSITION_LOG = logging.getLogger("positions")
POSITION_LOG.setLevel(logging.INFO)
position_fh = logging.FileHandler(LOG_DIR / "positions.log")
position_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
POSITION_LOG.addHandler(position_fh)

SUMMARY_LOG = logging.getLogger("summary")
SUMMARY_LOG.setLevel(logging.INFO)
summary_fh = logging.FileHandler(LOG_DIR / "summary.log")
summary_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
SUMMARY_LOG.addHandler(summary_fh)

def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any):
    with path.open("w") as f:
        json.dump(data, f, separators=(",", ":"))


def now() -> int:
    return int(time.time())


@dataclass
class PairCfg:
    delta: float
    precision: int
    trailing: float
    maxer: float
    quantity_step: float
    stop_loss: float
    emergency_stop_loss: float


@dataclass
class UserCfg:
    uid: int
    api_key: str
    secret_key: str
    ip: str = ""
    parts: int = 1000


# NEW: Global REST API rate limiter to prevent startup bursts
# REST API rate limiting removed - using error-based backoff only
# Binance allows 2400 req/min (40 req/sec) - our usage is well under limits


class BinanceREST:
    def __init__(self, api_key: str, secret_key: str, session: aiohttp.ClientSession, ip: str = ""):
        self.k, self.s, self.session, self.ip = api_key, secret_key, session, ip
        # Shared cache for positionRisk per user
        self._positions_cache: Dict[str, Any] = {"data": None, "ts": 0}
        self._positions_ttl_ms: int = 800  # max refresh ~1.25 Hz
        # Cache position mode to avoid repeated calls
        self._one_way_mode_set: bool = False

    async def _call(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, signed=False, retries=2, weight: float = 1.0):
        # Apply aggressive rate limiting before making REST calls
        is_order = "order" in path.lower()
        await _aggressive_rate_limiter.acquire(is_order=is_order, weight=weight)
        
        for attempt in range(retries):
            try:
                params = params or {}
                if signed:
                    params["timestamp"] = int(time.time() * 1000)
                    # Create a clean copy for signature generation
                    sign_params = {k: v for k, v in params.items() if k != "signature"}
                    # Sort parameters alphabetically for signature generation
                    sorted_params = sorted(sign_params.items())
                    # Use proper URL encoding for signature
                    qs = "&".join(f"{k}={v}" for k, v in sorted_params)
                    params["signature"] = hmac.new(self.s.encode(), qs.encode(), "sha256").hexdigest()
                    DETAILED_LOG.debug(f"Signature generated for: {qs}")
                
                headers = {"X-MBX-APIKEY": self.k}
                url = f"{BINANCE_FAPI_BASE}{path}"
                
                # For POST requests, send data in body, not URL params
                if method == "POST":
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    # Use the SAME order as signature generation (critical!)
                    body_data = f"{qs}&signature={params['signature']}" if signed else params
                    async with self.session.request(method, url, data=body_data, headers=headers) as r:
                        text = await r.text()
                        if r.status != 200:
                            raise RuntimeError(f"{method} {path} -> {r.status}: {text}")
                        return json.loads(text)
                else:
                    # GET and DELETE - construct URL manually to preserve parameter order
                    if signed:
                        # Use the same order as signature generation
                        full_url = f"{url}?{qs}&signature={params['signature']}"
                    else:
                        # For unsigned requests, use params normally
                        full_url = url
                        params_to_use = params
                    
                    if signed:
                        async with self.session.request(method, full_url, headers=headers) as r:
                            text = await r.text()
                            if r.status != 200:
                                raise RuntimeError(f"{method} {path} -> {r.status}: {text}")
                            return json.loads(text)
                    else:
                        async with self.session.request(method, full_url, params=params_to_use, headers=headers) as r:
                            text = await r.text()
                            if r.status != 200:
                                raise RuntimeError(f"{method} {path} -> {r.status}: {text}")
                            return json.loads(text)
            except Exception as e:
                DETAILED_LOG.error(f"API call failed (attempt {attempt+1}/{retries}): {e}")
                # More aggressive backoff for rate limit errors (418/429)
                msg = str(e)
                if ("-> 418:" in msg or "-> 429:" in msg) and attempt < retries - 1:
                    # Aggressive backoff for rate limit: 3s, 6s, 12s
                    backoff_time = 3.0 * (2 ** attempt)
                    DETAILED_LOG.info(f"Rate limit detected, backing off for {backoff_time:.1f}s")
                    await asyncio.sleep(backoff_time)
                    continue
                if attempt < retries - 1:
                    # Standard backoff for other errors: 0.5s, 1s, 2s  
                    await asyncio.sleep(0.5 * (2 ** attempt))
                else:
                    raise

    async def account_equity(self):
        d = await self._call("GET", "/fapi/v2/account", {}, True, weight=5.0)  # Account info has weight 5
        return float(d["totalWalletBalance"])

    async def set_leverage(self, symbol: str, lev: int):
        await self._call("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev, "recvWindow": 60000}, True, weight=1.0)

    async def set_one_way_mode(self):
        if self._one_way_mode_set:
            return
        try:
            await self._call("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false", "recvWindow": 60000}, True, weight=1.0)
            self._one_way_mode_set = True
        except Exception as e:
            if "No need to change position side" in str(e):
                DETAILED_LOG.debug("Account already in one-way mode, skipping")
                self._one_way_mode_set = True
                return
            else:
                raise

    async def test_api_connection(self):
        """Test API connection with a simple call"""
        try:
            result = await self._call("GET", "/fapi/v2/account", {}, True, weight=5.0)
            return result is not None
        except Exception as e:
            DETAILED_LOG.error(f"API connection test failed: {e}")
            return False

    async def position_risk(self, symbol: Optional[str] = None):
        # Use cached data if recent enough and for the same symbol request
        now_ts = int(time.time() * 1000)
        if symbol is None and self._positions_cache["data"] and (now_ts - self._positions_cache["ts"]) < self._positions_ttl_ms:
            return self._positions_cache["data"]
        
        data = await self._call("GET", "/fapi/v2/positionRisk", {}, True, weight=5.0)  # Position risk has weight 5
        
        # Cache all positions data for future use
        if symbol is None:
            self._positions_cache = {"data": data, "ts": now_ts}
        
        if symbol:
            return [d for d in data if d["symbol"] == symbol]
        return data

    async def open_orders(self, symbol: str):
        return await self._call("GET", "/fapi/v1/openOrders", {"symbol": symbol}, True, weight=1.0)

    async def place_limit(self, symbol: str, side: str, qty: str, price: str, cid: str, reduce: bool = False):
        return await self._call("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "LIMIT", "quantity": qty,
            "price": price, "timeInForce": "GTX", "newClientOrderId": cid,
            "reduceOnly": str(reduce).lower(), "selfTradePreventionMode": "EXPIRE_TAKER"
        }, True, weight=1.0)

    async def place_market(self, symbol: str, side: str, qty: float, reduce: bool = False):
        return await self._call("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty, "reduceOnly": str(reduce).lower()
        }, True, weight=1.0)

    async def get_order(self, symbol: str, cid: str):
        return await self._call("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid}, True, weight=1.0)

    async def cancel(self, symbol, cid):
        """Cancel order by client order id"""
        try:
            return await self._call("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid}, True, weight=1.0)
        except Exception as e:
            # Handle position doesn't exist error gracefully
            if "-2011" in str(e):  # Unknown order
                data = await self.position_risk(symbol)
                # If no position exists, the order might have been filled or already cancelled
                return {"msg": "Order not found, possibly filled or cancelled"}
            raise

    async def get_listen_key(self):
        """Get listen key for user data stream"""
        d = await self._call("POST", "/fapi/v1/listenKey", {}, signed=False, weight=1.0)
        return d["listenKey"]

    async def ping_listen_key(self, listen_key: str):
        """Ping listen key to keep it alive"""
        return await self._call("PUT", "/fapi/v1/listenKey", {"listenKey": listen_key}, signed=False, weight=1.0)
    
    async def keepalive_listen_key(self, listen_key: str):
        """Alias for compatibility with user data stream"""
        return await self.ping_listen_key(listen_key)

    async def close_listen_key(self, listen_key: str):
        """Close listen key"""
        return await self._call("DELETE", "/fapi/v1/listenKey", {"listenKey": listen_key}, signed=False, weight=1.0)

    async def get_exchange_info(self, symbol: str):
        """Get exchange information for a specific symbol"""
        data = await self._call("GET", "/fapi/v1/exchangeInfo", {"symbol": symbol}, signed=False, weight=1.0)
        return data


class SymbolBot:
    def __init__(self, pair: str, pc: PairCfg, uc: UserCfg, rest: BinanceREST, prices: Dict[str, float], order_manager=None):
        self.pair, self.pc, self.uc, self.rest, self.prices = pair, pc, uc, rest, prices
        self.order_manager = order_manager
        self._symbol_filters: Optional[Dict[str, float]] = None
        self.state_file = USER_STATE_DIR / f"user_{uc.uid}.json"
        if not self.state_file.exists():
            save_json(self.state_file, {})
        saved_state = load_json(self.state_file, {}).get(pair, {"last": 0, "tp": 0.0, "sl": 0.0})
        self.state = {"last": saved_state.get("last", 0), "tp": saved_state.get("tp", 0.0), "sl": saved_state.get("sl", 0.0)}
        self.log = self._logger()
        self.entry_id: Optional[str] = None
        self.exit_id: Optional[str] = None
        self.emergency_triggered = False
        self.last_order_time = 0
        self.position_id = saved_state.get("position_id", 0)
        self.trade_log = saved_state.get("trade_log", [])
        
        # NEW: Add max_price_reached for trailing TP logic
        self.max_price_reached = saved_state.get("max_price_reached", 0.0)
        self.min_price_reached = saved_state.get("min_price_reached", 0.0)
        self.trailing_activated = saved_state.get("trailing_activated", False)
        self.tp_armed_once = saved_state.get("tp_armed_once", False)
        
        # NEW: Emergency SL tracking variables
        self.emergency_sl_price = saved_state.get("emergency_sl_price", 0.0)
        self.emergency_sl_activated = saved_state.get("emergency_sl_activated", False)
        self.emergency_sl_tp_reference = saved_state.get("emergency_sl_tp_reference", 0.0)
        
        # NEW: Track on-exchange emergency stop order client id if any
        self.emergency_cid = saved_state.get("emergency_cid")
        
        # SIGNAL DEDUPLICATION: Track last ignored signal to prevent spam
        self.last_ignored_signal_type = saved_state.get("last_ignored_signal_type", None)
        # FLAT SIGNAL DEDUPLICATION: Track if flat signal cleanup was already logged
        self.flat_signal_processed = saved_state.get("flat_signal_processed", False)
        
        # NEW: Logging mode (production/testing) loaded from config; default to testing
        try:
            cfg = load_json(CONFIG_PATH, {}) or {}
            self._logging_mode = str((cfg.get("logging", {}) or {}).get("mode", "testing")).lower()
        except Exception:
            self._logging_mode = "testing"
        
        # NEW: Load enhanced tracking variables
        self.current_trade_id = saved_state.get("current_trade_id")
        self.trade_start_time = saved_state.get("trade_start_time")
        
        # Restore startup synchronization primitives
        self.startup_complete = False
        self.initialization_lock = asyncio.Lock()
        
        # Initialize runtime/order lifecycle attributes
        self.signal_time = None
        self.entry_order_time = None
        self.entry_fill_time = None
        self.tp_activation_time = None
        self.exit_order_time = None
        self.exit_fill_time = None
        self.drift_count = 0
        self.last_drift_time = None
        self.exit_order_history = []
        self.exit_order_status_checks = 0
        self.last_exit_status_check = 0
        self.exit_order_fill_detected = False
        self.position_detection_count = 0
        self.last_position_check = 0
        self.last_intelligent_check = 0  # For fallback position checking
        self.last_extremum_log = 0  # For extremum update logging (max 1/minute)
        self.last_status_log = 0  # For periodic position status updates (every 30 minutes)
        self.last_opposite_signal_log = 0  # For opposite signal logging throttling (max 1/second)
        self.emergency_sl_initialized = False
        self.exit_controller_type = None  # Track which controller placed the current exit order
        
        # NEW: WS metrics tracking
        self.ws_metrics = {
            "attempts": 0,
            "successes": 0,
            "errors": 0,
            "total_latency": 0,
            "last_20_attempts": []  # Sliding window for recent attempts
        }
        
        # NEW: Order placement tracking for metrics
        self.order_placement_history = []
        
        # Maker engine wiring
        self._maker_tuning = MakerTuning()
        self._peg_resolver = PegPriceResolver(self._maker_tuning)
        self.entry_ctrl = MakerOrderController(self, "entry", self._peg_resolver, self._maker_tuning)
        self.exit_ctrl = MakerOrderController(self, "exit", self._peg_resolver, self._maker_tuning)
        self.tp_ctrl = MakerOrderController(self, "exit", self._peg_resolver, self._maker_tuning, exit_kind="tp")
        self.sl_ctrl = MakerOrderController(self, "exit", self._peg_resolver, self._maker_tuning, exit_kind="sl")
        
        # NEW: Volatility monitoring and order state tracking
        self.volatility_monitor = VolatilityMonitor(VolatilityConfig())
        self.order_state_tracker = OrderStateTracker()
        
        # Store last price for volatility monitoring
        self._last_monitored_price = 0.0



    def _prepare_order_params(self, symbol: str, side: str, qty: float, price: float, reduce_only: bool = False) -> Tuple[float, float, str]:
        """
        Centralized order preparation: rounds price/qty to proper precision/step,
        applies exchange filters when available, and returns (final_price, final_qty, bump_note).
        """
        bump_note = ""
        # Prefer exchange filters if loaded (set during initialization)
        if getattr(self, "_symbol_filters", None):
            raw_price, raw_qty = float(price), float(qty)
            final_price, final_qty = self._apply_exchange_filters(raw_price, raw_qty)
            if not reduce_only and final_qty > raw_qty:
                new_notional = final_price * final_qty
                bump_note = f"MIN_NOTIONAL_BUMP: old_qty={raw_qty:.6f} new_qty={final_qty:.6f} price={final_price:.6f} notional={new_notional:.2f}"
                self._summary_log(f"📈 {bump_note}")
        else:
            # Legacy rounding path using configured precision/step and static min-notional
            final_price = round(price, self.pc.precision)
            step = self.pc.quantity_step
            final_qty = round(qty / step) * step
            min_notional = 20.0
            current_notional = final_price * final_qty
            if current_notional < min_notional and not reduce_only:
                min_qty = min_notional / max(final_price, 1e-12)
                min_qty = math.ceil(min_qty / step) * step
                old_qty = final_qty
                final_qty = min_qty
                new_notional = final_price * final_qty
                bump_note = f"MIN_NOTIONAL_BUMP: old_qty={old_qty:.6f} new_qty={final_qty:.6f} price={final_price:.6f} notional={new_notional:.2f}"
                self._summary_log(f"📈 {bump_note}")
                self._detailed_log(f"Quantity bumped from {old_qty:.6f} to {final_qty:.6f} to meet min notional $20.0")
        return final_price, final_qty, bump_note

    def _reload_pair_params_if_changed(self) -> None:
        """Hot-reload selected per-pair params from config.json if file changed.
        Only updates: trailing, stop_loss, emergency_stop_loss, maker_engine.
        Legacy keys delta/maxer are ignored by pricing but left compatible for trailing logic.
        """
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except Exception:
            return
        if not hasattr(self, "_cfg_mtime"):
            self._cfg_mtime = 0.0
        if mtime == getattr(self, "_cfg_mtime", 0.0):
            return
        try:
            cfg = load_json(CONFIG_PATH, {}) or {}
            pcfg = (cfg.get("pairs", {}) or {}).get(self.pair, {}) or {}
            changes: Dict[str, Dict[str, float]] = {}
            # mapping: config key -> attribute on self.pc
            for key in ("trailing", "stop_loss", "emergency_stop_loss"):
                if key in pcfg:
                    try:
                        new_val = float(pcfg.get(key))
                    except Exception:
                        continue
                    old_val = getattr(self.pc, key, new_val)
                    if old_val != new_val:
                        setattr(self.pc, key, new_val)
                        changes[key] = {"old": old_val, "new": new_val}
            # maker_engine tuning (global defaults or per-pair override)
            maker_cfg = (cfg.get("maker_engine", {}) or {}).copy()
            maker_pair_cfg = (pcfg.get("maker_engine", {}) or {}).copy()
            if maker_pair_cfg:
                maker_cfg.update(maker_pair_cfg)
            # Apply into controller tuning if provided
            try:
                if maker_cfg:
                    lt = maker_cfg.get("ladder_ticks")
                    if isinstance(lt, list) and all(isinstance(x, int) for x in lt):
                        self._maker_tuning.ladder_ticks = lt
                    v = int(maker_cfg.get("ladder_debounce_ms", self._maker_tuning.ladder_debounce_ms))
                    self._maker_tuning.ladder_debounce_ms = max(0, v)
                    v = int(maker_cfg.get("amend_time_budget_ms", self._maker_tuning.amend_time_budget_ms))
                    self._maker_tuning.amend_time_budget_ms = max(100, v)
                    v = int(maker_cfg.get("ws_retries_before_rest", self._maker_tuning.ws_retries_before_rest))
                    self._maker_tuning.ws_retries_before_rest = max(0, v)
                    v = int(maker_cfg.get("wide_spread_extra_ticks", self._maker_tuning.wide_spread_extra_ticks))
                    self._maker_tuning.wide_spread_extra_ticks = max(0, v)
                    # size thresholds
                    lst = maker_cfg.get("large_size_threshold_ticks")
                    if isinstance(lst, list):
                        parsed: list[tuple[float,int]] = []
                        for it in lst:
                            try:
                                ratio = float(it.get("ratio"))
                                ticks = int(it.get("ticks"))
                                parsed.append((ratio, ticks))
                            except Exception:
                                continue
                        if parsed:
                            self._maker_tuning.large_size_threshold_ticks = parsed
                    changes["maker_engine"] = {"applied": True}
            except Exception as _:
                pass
            self._cfg_mtime = mtime
            if changes:
                # Structured log for auditing
                self._log_trade_event("CONFIG_RELOAD_APPLIED", {"pair": self.pair, "changes": changes})
                self._detailed_log(f"Config reloaded for {self.pair}: {changes}")
        except Exception as e:
            # Non-fatal
            self._detailed_log(f"Config reload failed: {e}")

    def _update_ws_metrics(self, success: bool, latency_ms: float, method: str = "place"):
        """Update WS metrics and log on every attempt"""
        self.ws_metrics["attempts"] += 1
        self.ws_metrics["total_latency"] += latency_ms
        
        if success:
            self.ws_metrics["successes"] += 1
        else:
            self.ws_metrics["errors"] += 1
        
        # Add to sliding window (keep last 20 attempts)
        self.ws_metrics["last_20_attempts"].append({
            "success": success,
            "latency": latency_ms,
            "method": method,
            "timestamp": time.time()
        })
        
        # Keep only last 20
        if len(self.ws_metrics["last_20_attempts"]) > 20:
            self.ws_metrics["last_20_attempts"] = self.ws_metrics["last_20_attempts"][-20:]
        
        # Calculate metrics for last 20 attempts
        recent = self.ws_metrics["last_20_attempts"]
        ok_count = sum(1 for r in recent if r["success"]) if recent else 0
        err_count = len(recent) - ok_count if recent else 0
        avg_latency = (sum(r["latency"] for r in recent) / len(recent)) if recent else 0
        metrics_msg = f"WS {method} ok={ok_count} err={err_count} avg={avg_latency:.0f}ms pair={self.pair} window={len(recent)} gtx=true"
        self._summary_log(f"📊 {metrics_msg}")
        self._detailed_log(f"WS metrics: {metrics_msg}")

    def _logger(self):
        lg = logging.getLogger(f"{self.pair}_{self.uc.uid}")
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            fh = logging.FileHandler(LOG_DIR / f"{self.pair}_{self.uc.uid}.log")
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(fh)
        return lg

    def _detailed_log(self, msg):
        # Hot reload logging mode from config
        try:
            cfg = load_json(CONFIG_PATH, {}) or {}
            current_mode = str((cfg.get("logging", {}) or {}).get("mode", "testing")).lower()
        except Exception:
            current_mode = "testing"
        
        if current_mode != "production":
            self.log.info(msg)
            DETAILED_LOG.info(f"[{self.pair}-{self.uc.uid}] {msg}")

    def _summary_log(self, msg):
        self.log.info(msg)
        SUMMARY_LOG.info(f"[{self.pair}-{self.uc.uid}] {msg}")
    
    # NEW: Structured trade event logging methods
    def _log_trade_event(self, event_type: str, data: dict):
        """Log structured trade events for easy analysis"""
        event_data = {
            "timestamp": now(),
            "pair": self.pair,
            "user_id": self.uc.uid,
            "trade_id": self.current_trade_id,
            "event_type": event_type,
            **data
        }
        TRADE_EVENTS_LOG.info(f"TRADE_EVENT: {json.dumps(event_data)}")
        self._detailed_log(f"TRADE_EVENT [{event_type}]: {data}")
    
    def _log_position_event(self, event_type: str, data: dict):
        """Log position-related events"""
        event_data = {
            "timestamp": now(),
            "pair": self.pair,
            "user_id": self.uc.uid,
            "event_type": event_type,
            **data
        }
        POSITION_LOG.info(f"POSITION_EVENT: {json.dumps(event_data)}")
        self._detailed_log(f"POSITION_EVENT [{event_type}]: {data}")
    
    def _log_exit_order_event(self, event_type: str, data: dict):
        """Log exit order events with enhanced tracking"""
        event_data = {
            "timestamp": now(),
            "pair": self.pair,
            "user_id": self.uc.uid,
            "trade_id": self.current_trade_id,
            "event_type": event_type,
            **data
        }
        EXIT_LOG.info(f"EXIT_ORDER_EVENT: {json.dumps(event_data)}")
        self._detailed_log(f"EXIT_ORDER_EVENT [{event_type}]: {data}")
    
    def _log_entry_order_event(self, event_type: str, data: dict):
        """Log entry order events"""
        event_data = {
            "timestamp": now(),
            "pair": self.pair,
            "user_id": self.uc.uid,
            "trade_id": self.current_trade_id,
            "event_type": event_type,
            **data
        }
        ENTRY_LOG.info(f"ENTRY_ORDER_EVENT: {json.dumps(event_data)}")
        self._detailed_log(f"ENTRY_ORDER_EVENT [{event_type}]: {data}")
    
    def _log_trade_summary(self):
        """Log comprehensive trade summary for verification"""
        if not self.trade_log:
            return
            
        self._summary_log("=" * 60)
        self._summary_log("📈 TRADE SUMMARY REPORT")
        self._summary_log("=" * 60)
        
        total_pnl = 0
        for i, trade in enumerate(self.trade_log, 1):
            entry_price = trade.get("entry_price", 0)
            exit_price = trade.get("exit_price", 0)
            pnl = trade.get("pnl", 0)
            total_pnl += pnl
            
            self._summary_log(f"Trade #{i} (ID: {trade.get('position_id', 'N/A')}):")
            self._summary_log(f"  📊 {trade.get('side', 'N/A')} {trade.get('entry_qty', 0)} @ {entry_price:.5f}")
            self._summary_log(f"  🛡️ SL: {trade.get('sl_price', 0):.5f} | 🎯 TP: {trade.get('tp_price', 0):.5f}")
            if exit_price > 0:
                self._summary_log(f"  🏁 Exit: {exit_price:.5f} ({trade.get('exit_reason', 'N/A')})")
                self._summary_log(f"  💰 P&L: {pnl:+.2f} USDT")
            else:
                self._summary_log(f"  🔄 Status: OPEN")
            self._summary_log("")
        
        self._summary_log(f"💰 TOTAL P&L: {total_pnl:+.2f} USDT")
        self._summary_log("=" * 60)
    
    def generate_trade_report(self, trade_index: int = None) -> dict:
        """Generate comprehensive trade report for analysis"""
        if not self.trade_log:
            return {"error": "No trades found"}
        
        if trade_index is not None:
            if trade_index >= len(self.trade_log):
                return {"error": f"Trade index {trade_index} out of range"}
            trades = [self.trade_log[trade_index]]
        else:
            trades = self.trade_log
        
        report = {
            "pair": self.pair,
            "user_id": self.uc.uid,
            "total_trades": len(self.trade_log),
            "trades": []
        }
        
        for i, trade in enumerate(trades):
            trade_report = {
                "trade_number": i + 1,
                "position_id": trade.get("position_id"),
                "side": trade.get("side"),
                "entry_price": trade.get("entry_price"),
                "entry_qty": trade.get("entry_qty"),
                "sl_price": trade.get("sl_price"),
                "tp_price": trade.get("tp_price"),
                "exit_price": trade.get("exit_price"),
                "exit_qty": trade.get("exit_qty"),
                "exit_reason": trade.get("exit_reason"),
                "pnl": trade.get("pnl"),
                "entry_time": trade.get("entry_time"),
                "exit_time": trade.get("exit_time"),
                "duration_seconds": trade.get("exit_time", 0) - trade.get("entry_time", 0) if trade.get("exit_time") else None
            }
            
            # Add drift information if available
            if hasattr(self, 'drift_count'):
                trade_report["drift_count"] = self.drift_count
                trade_report["last_drift_time"] = self.last_drift_time
            
            # Add exit order history if available
            if hasattr(self, 'exit_order_history'):
                trade_report["exit_order_history"] = self.exit_order_history
            
            report["trades"].append(trade_report)
        
        return report
    
    def export_trade_events(self, output_file: str = None) -> str:
        """Export all trade events to a structured file for analysis"""
        if not output_file:
            output_file = f"trade_events_{self.pair}_{self.uc.uid}_{now()}.json"
        
        events = []
        
        # Read from trade_events.log
        try:
            with open(LOG_DIR / "trade_events.log", "r") as f:
                for line in f:
                    if f"TRADE_EVENT: " in line:
                        # Extract JSON from log line
                        json_start = line.find("TRADE_EVENT: ") + len("TRADE_EVENT: ")
                        json_str = line[json_start:].strip()
                        try:
                            event = json.loads(json_str)
                            if event.get("pair") == self.pair and event.get("user_id") == self.uc.uid:
                                events.append(event)
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        
        # Read from exit_orders.log
        try:
            with open(LOG_DIR / "exit_orders.log", "r") as f:
                for line in f:
                    if f"EXIT_ORDER_EVENT: " in line:
                        json_start = line.find("EXIT_ORDER_EVENT: ") + len("EXIT_ORDER_EVENT: ")
                        json_str = line[json_start:].strip()
                        try:
                            event = json.loads(json_str)
                            if event.get("pair") == self.pair and event.get("user_id") == self.uc.uid:
                                events.append(event)
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        
        # Sort events by timestamp
        events.sort(key=lambda x: x.get("timestamp", 0))
        
        # Save to file
        with open(output_file, "w") as f:
            json.dump(events, f, indent=2)
        
        return output_file

    def _save_state(self):
        data = load_json(self.state_file, {})
        state_with_trades = self.state.copy()
        state_with_trades["trade_log"] = self.trade_log
        state_with_trades["position_id"] = self.position_id
        # NEW: Save trailing TP state
        state_with_trades["max_price_reached"] = self.max_price_reached
        state_with_trades["min_price_reached"] = self.min_price_reached
        state_with_trades["trailing_activated"] = self.trailing_activated
        state_with_trades["tp_armed_once"] = getattr(self, "tp_armed_once", False)
        
        # NEW: Save emergency SL state
        state_with_trades["emergency_sl_price"] = self.emergency_sl_price
        state_with_trades["emergency_sl_activated"] = self.emergency_sl_activated
        state_with_trades["emergency_sl_tp_reference"] = self.emergency_sl_tp_reference
        state_with_trades["emergency_cid"] = self.emergency_cid
        
        # NEW: Save enhanced tracking state
        state_with_trades["current_trade_id"] = self.current_trade_id
        state_with_trades["trade_start_time"] = self.trade_start_time
        state_with_trades["signal_time"] = self.signal_time
        state_with_trades["entry_order_time"] = self.entry_order_time
        state_with_trades["entry_fill_time"] = self.entry_fill_time
        state_with_trades["tp_activation_time"] = self.tp_activation_time
        state_with_trades["exit_order_time"] = self.exit_order_time
        state_with_trades["exit_fill_time"] = self.exit_fill_time
        state_with_trades["drift_count"] = self.drift_count
        state_with_trades["last_drift_time"] = self.last_drift_time
        state_with_trades["exit_order_history"] = self.exit_order_history
        state_with_trades["exit_order_status_checks"] = self.exit_order_status_checks
        state_with_trades["last_exit_status_check"] = self.last_exit_status_check
        state_with_trades["exit_order_fill_detected"] = self.exit_order_fill_detected
        state_with_trades["position_detection_count"] = self.position_detection_count
        state_with_trades["last_position_check"] = self.last_position_check
        
        # SIGNAL DEDUPLICATION: Save ignored signal tracking state
        state_with_trades["last_ignored_signal_type"] = self.last_ignored_signal_type
        state_with_trades["flat_signal_processed"] = self.flat_signal_processed
        
        data[self.pair] = state_with_trades
        save_json(self.state_file, data)

    def _trail_tp(self, side: str, price: float, current_tp: float, entry: float) -> float:
        """Trailing take profit logic: derive activation from continuous extremum; place TP on reversal."""
        if side == "LONG":
            activation_price = round(entry * (1 + self.pc.trailing), self.pc.precision)
            if self.max_price_reached <= 0:
                self.max_price_reached = price
            elif price > self.max_price_reached:
                old_max = self.max_price_reached
                self.max_price_reached = price
                self._detailed_log(f"   📈 NEW HIGH: {old_max:.5f} → {self.max_price_reached:.5f}")
                # Enhanced extremum logging for summary logs (max 1/minute)
                trailing_tp_level = round(self.max_price_reached * (1 - self.pc.trailing), self.pc.precision) if self.trailing_activated else 0.0
                self._log_extremum_update("LONG", old_max, self.max_price_reached, self.trailing_activated, trailing_tp_level)
            activated = self.max_price_reached >= activation_price
            self.trailing_activated = activated
            trailing_tp_level = round(self.max_price_reached * (1 - self.pc.trailing), self.pc.precision) if activated else 0.0
            self._detailed_log(f"🎯 TRAILING_SNAPSHOT (LONG): price={price:.5f} entry={entry:.5f} activ_threshold={activation_price:.5f} extremum={self.max_price_reached:.5f} activated={activated} level={trailing_tp_level:.5f}")
            if activated and not self.tp_armed_once:
                self.tp_armed_once = True
                self.tp_activation_time = now()
                self._log_trade_event("TP_TRAILING_ARMED", {"activation_price": activation_price, "extremum_at_activation": self.max_price_reached, "time": self.tp_activation_time, "side": side})
            if not activated:
                return current_tp
            if price <= trailing_tp_level:
                order_tp = round(trailing_tp_level, self.pc.precision)
                self._detailed_log(f"   🎯 PRICE REVERSAL DETECTED: Price {price:.5f} <= Trailing TP Level {trailing_tp_level:.5f}")
                self._detailed_log(f"   🎯 TP ARMED at: {order_tp:.5f} (reversal detected)")
                # Enhanced price reversal logging for summary logs
                profit_potential = order_tp - entry if side == "LONG" else entry - order_tp
                self._summary_log(f"🔄 PRICE_REVERSAL: {self.max_price_reached:.5f}→{price:.5f} | TP triggered at {order_tp:.5f} | Profit potential: +{profit_potential:.5f}")
                return order_tp
            return current_tp
        else:
            activation_price = round(entry * (1 - self.pc.trailing), self.pc.precision)
            if self.min_price_reached <= 0:
                self.min_price_reached = price
            elif price < self.min_price_reached:
                old_min = self.min_price_reached
                self.min_price_reached = price
                self._detailed_log(f"   📉 NEW LOW: {old_min:.5f} → {self.min_price_reached:.5f}")
                # Enhanced extremum logging for summary logs (max 1/minute)  
                trailing_tp_level = round(self.min_price_reached * (1 + self.pc.trailing), self.pc.precision) if self.trailing_activated else 0.0
                self._log_extremum_update("SHORT", old_min, self.min_price_reached, self.trailing_activated, trailing_tp_level)
            activated = self.min_price_reached <= activation_price
            self.trailing_activated = activated
            trailing_tp_level = round(self.min_price_reached * (1 + self.pc.trailing), self.pc.precision) if activated else 0.0
            self._detailed_log(f"🎯 TRAILING_SNAPSHOT (SHORT): price={price:.5f} entry={entry:.5f} activ_threshold={activation_price:.5f} extremum={self.min_price_reached:.5f} activated={activated} level={trailing_tp_level:.5f}")
            if activated and not self.tp_armed_once:
                self.tp_armed_once = True
                self.tp_activation_time = now()
                self._log_trade_event("TP_TRAILING_ARMED", {"activation_price": activation_price, "extremum_at_activation": self.min_price_reached, "time": self.tp_activation_time, "side": side})
            if not activated:
                return current_tp
            if price >= trailing_tp_level:
                order_tp = round(trailing_tp_level, self.pc.precision)
                self._detailed_log(f"   🎯 PRICE REVERSAL DETECTED: Price {price:.5f} >= Trailing TP Level {trailing_tp_level:.5f}")
                self._detailed_log(f"   🎯 TP ARMED at: {order_tp:.5f} (reversal detected)")
                # Enhanced price reversal logging for summary logs
                profit_potential = entry - order_tp if side == "SHORT" else order_tp - entry
                self._summary_log(f"🔄 PRICE_REVERSAL: {self.min_price_reached:.5f}→{price:.5f} | TP triggered at {order_tp:.5f} | Profit potential: +{profit_potential:.5f}")
                return order_tp
            return current_tp

    def _calc_sl(self, side: str, entry: float) -> float:
        """SIMPLIFIED: Fixed percentage stop loss from entry price"""
        # Use configurable stop loss percentage from pair config
        sl_percentage = self.pc.stop_loss
        
        if side == "LONG":
            return round(entry * (1 - sl_percentage), self.pc.precision)
        return round(entry * (1 + sl_percentage), self.pc.precision)

    def _calc_emergency_sl(self, side: str, reference_price: float) -> float:
        """Calculate emergency stop loss price based on reference price"""
        # Use configurable emergency stop loss percentage from pair config
        emergency_sl_percentage = self.pc.emergency_stop_loss
        
        if side == "LONG":
            emergency_sl = round(reference_price * (1 - emergency_sl_percentage), self.pc.precision)
            # For LONG positions, emergency SL should never move down (lock in highest level)
            # Only apply this logic if we already have an emergency SL set (not during initialization)
            if self.emergency_sl_price > 0 and hasattr(self, 'emergency_sl_initialized') and self.emergency_sl_initialized:
                emergency_sl = max(emergency_sl, self.emergency_sl_price)
        else:  # SHORT
            emergency_sl = round(reference_price * (1 + emergency_sl_percentage), self.pc.precision)
            # For SHORT positions, emergency SL should never move up (lock in lowest level)
            # Only apply this logic if we already have an emergency SL set (not during initialization)
            if self.emergency_sl_price > 0 and hasattr(self, 'emergency_sl_initialized') and self.emergency_sl_initialized:
                emergency_sl = min(emergency_sl, self.emergency_sl_price)
        
        return emergency_sl

    async def _bet(self, price: float) -> float:
        try:
            eq = await self.rest.account_equity()
            raw = (eq / self.uc.parts * 100 / price) / self.pc.quantity_step
            return int(raw) * self.pc.quantity_step
        except Exception as e:
            if "Signature" in str(e):
                self._detailed_log("Skipping account equity check due to signature issue, using default")
                return 1000
            else:
                raise

    def _should_check_position(self, signal: dict) -> bool:
        """Intelligent decision: when should we check position?"""
        current_time = now()
        
        # Always check if:
        signal_type = signal.get('type', 'flat').upper()
        has_active_signal = signal_type != 'FLAT'
        has_unprocessed_signal = not signal.get("processed", True)
        has_open_orders = (self.entry_id is not None or self.exit_id is not None or self.emergency_cid is not None)
        
        # Fallback: check every 60 seconds maximum
        fallback_needed = (current_time - self.last_intelligent_check) > 60.0
        
        should_check = (has_active_signal or has_unprocessed_signal or has_open_orders or fallback_needed)
        
        if should_check:
            self.last_intelligent_check = current_time
            
        return should_check
    
    def _should_log_extremum(self) -> bool:
        """Check if we should log extremum update (max 1 per minute to prevent spam)"""
        current_time = now()
        if current_time - self.last_extremum_log >= 60.0:  # 1 minute minimum
            self.last_extremum_log = current_time
            return True
        return False
    
    def _log_extremum_update(self, side: str, old_value: float, new_value: float, trailing_active: bool, tp_level: float):
        """Log significant extremum updates with context"""
        if not self._should_log_extremum():
            return
            
        change = new_value - old_value
        direction = "HIGH" if side == "LONG" else "LOW"
        arrow = "📈" if side == "LONG" else "📉"
        trailing_status = "✅" if trailing_active else "❌"
        
        self._summary_log(f"{arrow} NEW_EXTREMUM: {direction} {old_value:.5f}→{new_value:.5f} ({change:+.5f}) | Trailing={trailing_status} | TP_Level={tp_level:.5f}")
    
    def _should_log_periodic_status(self) -> bool:
        """Check if we should log periodic position status (every 30 minutes)"""
        current_time = now()
        if current_time - self.last_status_log >= 1800.0:  # 30 minutes = 1800 seconds
            self.last_status_log = current_time
            return True
        return False
    
    def _log_periodic_position_status(self, pos: dict, current_price: float):
        """Log comprehensive position status every 30 minutes"""
        if not self._should_log_periodic_status() or not pos:
            return
            
        try:
            side = pos.get("side", "").upper()
            qty = float(pos.get("qty", 0))
            entry = float(pos.get("entry", 0))
            
            # Get current state
            sl_price = self.state.get("sl", 0)
            tp_price = self.state.get("tp", 0)
            
            # ENHANCED: Show calculated trailing TP level when trailing is active
            if self.trailing_activated and tp_price == 0:
                try:
                    # Use the same calculation as NEW_EXTREMUM messages
                    if side == "LONG":
                        # For LONG: TP level is below max_price_reached
                        tp_price = round(self.max_price_reached * (1 - self.pc.trailing), self.pc.precision) if self.max_price_reached > 0 else 0.0
                    else:  # SHORT
                        # For SHORT: TP level is above min_price_reached
                        tp_price = round(self.min_price_reached * (1 + self.pc.trailing), self.pc.precision) if self.min_price_reached > 0 else 0.0
                except Exception:
                    pass  # Keep tp_price as 0 if calculation fails
            
            esl_price = self.emergency_sl_price or 0
            trailing_status = "✅" if self.trailing_activated else "❌"
            extremum = self.max_price_reached if side == "LONG" else self.min_price_reached
            extremum = extremum if extremum > 0 else entry
            
            # Calculate unrealized P&L
            if side == "LONG":
                unrealized = (current_price - entry) * abs(qty)
            else:  # SHORT
                unrealized = (entry - current_price) * abs(qty)
            
            # Format unrealized P&L with + or - sign
            unrealized_str = f"{unrealized:+.2f}"
            
            # ENHANCED: Add trailing activation price when not yet activated
            if not self.trailing_activated:
                try:
                    # Calculate trailing activation price
                    if side == "LONG":
                        activation_price = entry * (1 + (self.pc.trailing_activation_pct or 0.01))  # Default 1% if not set
                    else:  # SHORT
                        activation_price = entry * (1 - (self.pc.trailing_activation_pct or 0.01))  # Default 1% if not set
                    
                    status_msg = f"📊 POSITION_STATUS: {side} {abs(qty)}@{entry:.5f} | Price={current_price:.5f} | SL={sl_price:.5f} | TP={tp_price:.5f} | ESL={esl_price:.5f} | Trailing={trailing_status} | ActivationPrice={activation_price:.5f} | Extremum={extremum:.5f} | Unrealized={unrealized_str}"
                except Exception:
                    # Fallback to original format if calculation fails
                    status_msg = f"📊 POSITION_STATUS: {side} {abs(qty)}@{entry:.5f} | Price={current_price:.5f} | SL={sl_price:.5f} | TP={tp_price:.5f} | ESL={esl_price:.5f} | Trailing={trailing_status} | Extremum={extremum:.5f} | Unrealized={unrealized_str}"
            else:
                # Keep existing format when trailing is active
                status_msg = f"📊 POSITION_STATUS: {side} {abs(qty)}@{entry:.5f} | Price={current_price:.5f} | SL={sl_price:.5f} | TP={tp_price:.5f} | ESL={esl_price:.5f} | Trailing={trailing_status} | Extremum={extremum:.5f} | Unrealized={unrealized_str}"
            
            self._summary_log(status_msg)
            
        except Exception as e:
            self._detailed_log(f"Error in periodic status logging: {e}")

    async def _position(self):
        """Get current position with enhanced error handling and validation"""
        try:
            data = await (self.order_manager.position_risk(self.pair) if self.order_manager else self.rest.position_risk(self.pair))
            for d in data:
                amt = float(d["positionAmt"])
                if amt:
                    position = {
                        "side": "LONG" if amt > 0 else "SHORT", 
                        "qty": abs(amt), 
                        "entry": float(d["entryPrice"])
                    }
                    # CRITICAL FIX: Validate position data
                    if position["entry"] <= 0:
                        self._detailed_log(f"Invalid entry price: {position['entry']}, ignoring position")
                        return None
                    if position["qty"] <= 0:
                        self._detailed_log(f"Invalid quantity: {position['qty']}, ignoring position")
                        return None
                    
                    # NEW: Enhanced position tracking
                    self.position_detection_count += 1
                    current_time = now()
                    
                    # NEW: Log position detection event
                    self._log_position_event("POSITION_DETECTED", {
                        "side": position["side"],
                        "qty": position["qty"],
                        "entry": position["entry"],
                        "detection_count": self.position_detection_count,
                        "time_since_last_check": current_time - self.last_position_check if self.last_position_check else None
                    })
                    
                    # NEW: Track trade start if this is a new position
                    if not self.current_trade_id:
                        self.current_trade_id = f"{self.pair}_{self.uc.uid}_{self.position_id}_{current_time}"
                        self.trade_start_time = current_time
                        self.entry_fill_time = current_time
                        
                        self._log_trade_event("TRADE_STARTED", {
                            "trade_id": self.current_trade_id,
                            "position_id": self.position_id,
                            "side": position["side"],
                            "entry_price": position["entry"],
                            "entry_qty": position["qty"],
                            "signal_time": self.signal_time,
                            "entry_order_time": self.entry_order_time,
                            "time_from_signal": current_time - self.signal_time if self.signal_time else None,
                            "time_from_entry_order": current_time - self.entry_order_time if self.entry_order_time else None
                        })
                        # FILE MODE: mark signal processed=true when entry/position is confirmed
                        try:
                            cfg = load_json(CONFIG_PATH, {}) or {}
                            src = str(cfg.get("signals", {}).get("source", "file")).lower()
                        except Exception:
                            src = "file"
                        if src == "file":
                            try:
                                signals = load_json(SIGNALS_PATH, {}) or {}
                                sig = signals.get(self.pair, {"type": "flat", "processed": False})
                                sig["processed"] = True
                                signals[self.pair] = sig
                                save_json(SIGNALS_PATH, signals)
                                self._summary_log(f"📋 SIGNAL PROCESSED: {self.pair} {sig.get('type', 'unknown').upper()} marked as processed=true")
                            except Exception as e:
                                self._summary_log(f"⚠️ Failed to mark signal processed: {e}")
                    
                    self.last_position_check = current_time
                    self._detailed_log(f"Position found: {position}")
                    return position
            
            # No position found
            self._detailed_log("No position found on exchange")
            self.last_position_check = now()
            return None
            
        except Exception as e:
            if "Signature" in str(e):
                self._detailed_log("Skipping position check due to signature issue, assuming no position")
                return None
            else:
                self._detailed_log(f"Position check error: {e}")
                # Don't assume anything on other errors - let it bubble up
                raise

    async def _validate_position_state(self, pos):
        """Validate that our internal state matches the actual exchange position"""
        if not pos:
            # No position on exchange, but we think we have one
            if self.state["sl"] != 0 or self.state["tp"] != 0:
                self._summary_log("⚠️ STATE MISMATCH: No exchange position but internal state suggests position exists")
                self._detailed_log(f"Evaluating reset: SL={self.state['sl']}, TP={self.state['tp']}")
                # Verify exit order status first
                if self.exit_id:
                    self._summary_log(f"🔍 CHECKING EXIT ORDER STATUS: {self.exit_id}")
                    try:
                        order_status = await self.get_order_status_with_retry(self.exit_id, max_retries=3)
                        if order_status and str(order_status.get("status", "")).upper() in ("NEW", "PARTIALLY_FILLED"):
                            self._detailed_log("Open exit order exists; skipping state reset")
                            return True
                    except Exception as e:
                        self._detailed_log(f"Exit order status check failed: {e}")
                # Debounce and confirm
                try:
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
                confirm = await self._position()
                if confirm is None:
                    # Cancel on-exchange emergency SL if present
                    try:
                        if self.emergency_cid and self.order_manager:
                            await self.order_manager.cancel(self.pair, self.emergency_cid)
                            self._summary_log(f"🛡️ EMERGENCY STOP ORDER CANCELED: cid={self.emergency_cid}")
                            self.emergency_cid = None
                    except Exception:
                        pass
                    # NEW: Orphan ESL sweeper — remove any stray esl-* cids not tied to a live position
                    try:
                        if self.order_manager:
                            open_orders = await self.order_manager.open_orders(self.pair)
                            for o in open_orders:
                                coid = str(o.get("clientOrderId") or o.get("origClientOrderId") or "")
                                if coid.startswith("esl-"):
                                    try:
                                        await self.order_manager.cancel(self.pair, coid)
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                    self.state.update({"tp": 0, "sl": 0, "last": now()})
                    self.exit_id = None
                    # CRITICAL FIX: Clear controller tracking when exit_id is cleared
                    self.exit_controller_type = None
                    self._detailed_log("State cleared after confirmed no-position")
                return True
        else:
            # Position exists; ensure state is sane
            if self.state["sl"] == 0 and self.state["tp"] == 0:
                self._detailed_log("Position exists with zeroed SL/TP; leaving as-is (controller-driven exits)")
            return True
        # Default: allow processing to continue
        return True

    async def _get_price(self):
        price = self.prices.get(self.pair, 0)
        if price == 0:
            # Avoid aggressive REST fallback; rely on WS prices unless WS declared down
            use_rest = True
            if hasattr(self, 'order_manager') and self.order_manager is not None:
                # Access internal flag via helper policy when available
                try:
                    # If WS is allowed by order manager, skip REST fallback here
                    use_rest = not getattr(self.order_manager, '_ws_allowed')()
                except Exception:
                    use_rest = True
            if use_rest:
                price = await self.rest.mark_price(self.pair)
                self._detailed_log(f"Fallback to REST mark price: {price}")
        return price

    def _get_order_book(self) -> Dict[str, float]:
        """Return latest order book snapshot used by maker engine.
        Expects ws_prices to populate prices[f"{PAIR}_BOOK"] with best bid/ask and sizes.
        Fallback to last ask price with a synthetic bid one tick below if needed.
        """
        try:
            key = f"{self.pair}_BOOK"
            ob = self.prices.get(key)
            if isinstance(ob, dict) and ob.get("best_bid") and ob.get("best_ask"):
                return ob
            # Fallback: synthesize minimal book from last price
            last_ask = float(self.prices.get(self.pair, 0) or 0)
            if last_ask > 0:
                tick = 0.0
                try:
                    if getattr(self, "_symbol_filters", None):
                        tick = float(self._symbol_filters.get("tickSize", 0) or 0)
                except Exception:
                    tick = 0.0
                if tick <= 0:
                    try:
                        tick = 10 ** (-(self.pc.precision or 1))
                    except Exception:
                        tick = 0.01
                return {
                    "best_bid": max(0.0, last_ask - tick),
                    "best_ask": last_ask,
                    "bid_qty": 0.0,
                    "ask_qty": 0.0,
                    "ts": time.time(),
                }
        except Exception:
            pass
        return {}

    async def _initialize_bot(self):
        """Complete bot initialization with proper startup sequence"""
        async with self.initialization_lock:
            if self.startup_complete:
                return
                
            self._summary_log("🔧 INITIALIZING BOT...")
            
            # Step 1: Clean up any stale orders from previous sessions
            await self._cleanup_stale_orders()
            
            # Step 2: Load exchange filters early for price/qty rounding
            try:
                await self._ensure_symbol_filters()
            except Exception:
                pass
            
            # Step 3: Reconcile exchange state with local state
            await self._reconcile_exchange_state()
            
            # Step 3a: Wait for WS snapshots and adopt current position and open ESLs
            # Retry for up to ~5s to avoid missing late WS snapshots
            self.position_confirmed = False
            adopted_side = None
            adopted_qty = 0.0
            adopted_entry = 0.0
            try:
                for _ in range(20):  # 20 * 0.25s = 5s
                    try:
                        pos_list = await (self.order_manager.position_risk(self.pair) if self.order_manager else self.rest.position_risk(self.pair))
                    except Exception:
                        pos_list = None
                    found = False
                    for d in pos_list or []:
                        try:
                            amt = float(d.get("positionAmt", 0) or 0)
                            if abs(amt) > 0:
                                adopted_side = "LONG" if amt > 0 else "SHORT"
                                adopted_qty = abs(amt)
                                adopted_entry = float(d.get("entryPrice", 0) or 0.0)
                                self.state["sl"] = self.state.get("sl", 0)
                                self.state["tp"] = self.state.get("tp", 0)
                                self.state["side"] = adopted_side
                                self.position_confirmed = True
                                found = True
                                break
                        except Exception:
                            continue
                    if found:
                        # Enhanced position adoption logging with comprehensive status
                        sl_price = self.state.get("sl", 0)
                        tp_price = self.state.get("tp", 0) 
                        esl_price = self.emergency_sl_price or 0
                        trailing_status = "✅" if self.trailing_activated else "❌"
                        extremum = self.max_price_reached if adopted_side == "LONG" else self.min_price_reached
                        extremum = extremum if extremum > 0 else adopted_entry
                        
                        # CRITICAL FIX: Recalculate ESL for adopted position direction
                        # Don't use stale ESL values from previous positions with different directions
                        if sl_price > 0:
                            # Recalculate ESL based on current position's SL
                            self.emergency_sl_price = self._calc_emergency_sl(adopted_side, sl_price)
                            self.emergency_sl_initialized = True
                            esl_price = self.emergency_sl_price
                            self._summary_log(f"🔄 ESL_RECALCULATED: {adopted_side} position | SL={sl_price:.5f} → ESL={esl_price:.5f}")
                        else:
                            # If no SL set, use entry price as reference for emergency calculation
                            self.emergency_sl_price = self._calc_emergency_sl(adopted_side, adopted_entry)
                            self.emergency_sl_initialized = True
                            esl_price = self.emergency_sl_price
                            self._summary_log(f"🔄 ESL_INITIALIZED: {adopted_side} position | Entry={adopted_entry:.5f} → ESL={esl_price:.5f}")
                        
                        self._summary_log(f"📥 POSITION_ADOPTED: {adopted_side} {adopted_qty}@{adopted_entry:.5f} | SL={sl_price:.5f} | ESL={esl_price:.5f} | TP={tp_price:.5f} | Trailing={trailing_status} | Extremum={extremum:.5f}")
                        break
                    await asyncio.sleep(0.25)
            except Exception:
                self.position_confirmed = False
            
            # Step 3b: Adopt existing emergency STOP_MARKET order if present
            try:
                open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
                # ESL singleton resolution: collect all STOP_MARKET closePosition orders
                esls = []
                for o in open_orders:
                    try:
                        if str(o.get("type", "")).upper() == "STOP_MARKET" and bool(o.get("closePosition", False)):
                            esls.append(o)
                    except Exception:
                        continue
                if esls:
                    # Pick one to keep: closest stopPrice to target; fallback to latest updateTime
                    target = float(self.emergency_sl_price or 0.0)
                    def score(o):
                        try:
                            sp = float(o.get("stopPrice") or o.get("sp") or 0)
                            return abs(sp - target) if target else -(float(o.get("updateTime") or 0))
                        except Exception:
                            return 0
                    esls.sort(key=score)
                    keep = esls[0]
                    self.emergency_cid = keep.get("clientOrderId") or keep.get("origClientOrderId")
                    # Cancel extras
                    extras = esls[1:]
                    canceled = []
                    for ex in extras:
                        xc = ex.get("clientOrderId") or ex.get("origClientOrderId")
                        try:
                            await self.order_manager.cancel(self.pair, xc)
                            canceled.append(xc)
                        except Exception:
                            continue
                    if canceled:
                        self._summary_log(f"🧹 ESL_SINGLETON resolved: kept={self.emergency_cid}, canceled={canceled}")
                    else:
                        try:
                            sp = float(keep.get("stopPrice") or keep.get("sp") or 0)
                            self._summary_log(f"🛡️ ESL ADOPTED: cid={self.emergency_cid} stopPrice={sp:.5f}")
                        except Exception:
                            self._summary_log(f"🛡️ ESL ADOPTED: cid={self.emergency_cid}")
                    # Optionally amend kept to current target
                    if self.emergency_cid and self.emergency_sl_price > 0 and self.order_manager:
                        side = "SELL" if (self.state.get("side") or "LONG") == "LONG" else "BUY"
                        try:
                            await self.order_manager.modify_emergency_stop(self.pair, self.emergency_cid, side, float(self.emergency_sl_price))
                            self._summary_log(f"🛡️ EMERGENCY STOP ORDER ADOPTED & AMENDED: cid={self.emergency_cid} stopPrice={self.emergency_sl_price:.5f}")
                        except Exception:
                            self._summary_log(f"🛡️ EMERGENCY STOP ORDER ADOPTED: cid={self.emergency_cid} (amendment failed)")
                else:
                    # If we have a confirmed position but no ESL, bootstrap one now
                    if self.position_confirmed and self.order_manager:
                        try:
                            side = self.state.get("side") or ("LONG" if adopted_qty > 0 else "SHORT")
                            eside = "SELL" if side == "LONG" else "BUY"
                            cid = f"esl-{self.pair}-{int(time.time()*1000)}"
                            # Validate stop trigger direction using entry as proxy when current price unavailable
                            base_price = adopted_entry or 0.0
                            valid_stop = float(self.emergency_sl_price) if self.emergency_sl_price else base_price
                            tick = float((self._symbol_filters or {}).get("tickSize", 0) or 0)
                            if valid_stop and base_price:
                                if eside == "BUY" and valid_stop <= base_price:
                                    valid_stop = round(base_price * (1 + max(0.001, tick)), self.pc.precision)
                                elif eside == "SELL" and valid_stop >= base_price:
                                    valid_stop = round(base_price * (1 - max(0.001, tick)), self.pc.precision)
                            
                            # CRITICAL FIX: Check ESL placement result
                            esl_result = await self._ensure_single_esl(side, float(valid_stop))
                            if esl_result:
                                self.emergency_cid = esl_result
                                self._summary_log(f"🛡️ ESL BOOTSTRAP SUCCESS: cid={esl_result} stopPrice={valid_stop:.5f}")
                            else:
                                self._summary_log(f"❌ ESL BOOTSTRAP FAILED: _ensure_single_esl returned None")
                        except Exception as e:
                            self._summary_log(f"⚠️ ESL BOOTSTRAP FAILED: {e}")
            except Exception:
                pass
            
            # Step 4: Mark initialization complete
            self.startup_complete = True
            self._summary_log("✅ BOT INITIALIZATION COMPLETE")

    async def _cleanup_stale_orders(self):
        """Remove ALL stale orders from previous sessions with rate limiting"""
        try:
            open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
            stale_orders = [order for order in open_orders if order['clientOrderId'].startswith(f"{self.pair}_")]
            
            if stale_orders:
                self._summary_log(f"🧹 CLEANING UP {len(stale_orders)} STALE ORDERS from previous sessions")
                # NEW: Add delays between cancellations to prevent REST burst
                for i, order in enumerate(stale_orders):
                    try:
                        await (self.order_manager.cancel(self.pair, order['clientOrderId']) if self.order_manager else self.rest.cancel(self.pair, order['clientOrderId']) )
                        self._detailed_log(f"Cleaned stale order: {order['clientOrderId']} @ {order.get('price', order.get('stopPrice', order.get('avgPrice', 'N/A')))}")
                        
                        # Add small delay between cancellations to avoid burst (except for last order)
                        if i < len(stale_orders) - 1:
                            # Immediate cancellation - no delay needed
                            pass
                            
                    except Exception as e:
                        self._detailed_log(f"Failed to clean stale order {order['clientOrderId']}: {e}")
            else:
                self._detailed_log("No stale orders found to clean")
                
        except Exception as e:
            self._detailed_log(f"Error during stale order cleanup: {e}")

    async def _reconcile_exchange_state(self):
        """Ensure local state matches exchange state after cleanup with rate limiting"""
        try:
            open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
            entry_orders = [order for order in open_orders if order['clientOrderId'].startswith(f"{self.pair}_open")]
            
            if entry_orders:
                # This should not happen after cleanup, but handle it
                self._summary_log(f"⚠️ UNEXPECTED: Found {len(entry_orders)} orders after cleanup")
                # NEW: Add delays between cancellations to prevent REST burst
                for i, order in enumerate(entry_orders):
                    await (self.order_manager.cancel(self.pair, order['clientOrderId']) if self.order_manager else self.rest.cancel(self.pair, order['clientOrderId']))
                    
                    # Add small delay between cancellations (except for last order)
                    if i < len(entry_orders) - 1:
                        # Immediate cancellation - no delay needed
                        pass
                    
            # Ensure clean state
            self.entry_id = None
            self.exit_id = None
            self._detailed_log("Exchange state reconciled - clean slate confirmed")
            
        except Exception as e:
            self._detailed_log(f"Error during state reconciliation: {e}")

    async def run(self):
        # Initialize bot properly before starting trading loop
        await self._initialize_bot()
        
        consecutive_errors = 0
        while True:
            try:
                # Only start trading after initialization is complete
                if self.startup_complete:
                    await self.tick()
                    consecutive_errors = 0
                else:
                    # Wait for initialization
                    await asyncio.sleep(1)
                    continue
                    
            except Exception as e:
                consecutive_errors += 1
                self.log.error(f"Tick error (attempt {consecutive_errors}): {e}")
                self._detailed_log(f"Tick error: {e}")
                
                if consecutive_errors >= 3:
                    self.log.warning(f"3 consecutive errors, waiting 60 seconds before retry")
                    await asyncio.sleep(60)
                    consecutive_errors = 0
            
            await asyncio.sleep(0.5)  # Track price movements twice per second

    async def tick(self):
        price = await self._get_price()
        if not price:
            return
        
        # NEW: Update volatility monitor with price changes
        if self._last_monitored_price > 0 and price > 0:
            self.volatility_monitor.on_price_update(self.pair, self._last_monitored_price, price)
            
            # Update spread if we have order book data
            try:
                book = self._get_order_book()
                if book and book.get('best_bid') and book.get('best_ask'):
                    spread = float(book['best_ask']) - float(book['best_bid'])
                    spread_pct = spread / price if price > 0 else 0
                    self.volatility_monitor.on_spread_update(self.pair, spread_pct)
            except Exception:
                pass
        
        self._last_monitored_price = price
        
        # Check volatility mode and log transitions
        volatility_mode = self.volatility_monitor.get_mode(self.pair)
        volatility_score = self.volatility_monitor.get_score(self.pair)
        
        # Log mode changes
        if not hasattr(self, '_last_volatility_mode'):
            self._last_volatility_mode = {}
        
        if self._last_volatility_mode.get(self.pair) != volatility_mode:
            if volatility_mode == "HIGH_VOLATILITY":
                self._summary_log(f"⚡ ENTERING HIGH VOLATILITY MODE for {self.pair} (score: {volatility_score:.1f})")
                self._summary_log(f"   HV Mode Settings: min_tick_diff_exit={self._maker_tuning.hv_min_tick_diff_exit}, " +
                                f"amend_interval={self._maker_tuning.hv_min_amend_interval}s, max_amendments={self._maker_tuning.hv_max_amendments_per_order}")
            elif self._last_volatility_mode.get(self.pair) == "HIGH_VOLATILITY":
                self._summary_log(f"✅ EXITING HIGH VOLATILITY MODE for {self.pair} (score: {volatility_score:.1f})")
            self._last_volatility_mode[self.pair] = volatility_mode
        
        # Get current signal state
        signals = load_json(SIGNALS_PATH, {})
        signal = signals.get(self.pair, {"type": "flat", "processed": True})
        
        # ENHANCED LOGGING: Tick summary
        self._detailed_log(f"🔄 TICK SUMMARY:")
        self._detailed_log(f"   Current Price: {price:.5f}")
        self._detailed_log(f"   Signal: {signal.get('type', 'flat').upper()}")
        self._detailed_log(f"   Signal Processed: {signal.get('processed', True)}")
        self._detailed_log(f"   Entry Order ID: {self.entry_id}")
        self._detailed_log(f"   Exit Order ID: {self.exit_id}")
        self._detailed_log(f"   Current TP: {self.state['tp']:.5f}")
        self._detailed_log(f"   Current SL: {self.state['sl']:.5f}")
        
        # Check for new unprocessed signal and process it immediately
        new_signal_detected = not signal.get("processed", True)
        if new_signal_detected:
            async with SIGNAL_LOCK:
                signals = load_json(SIGNALS_PATH, {})
                signal = signals.get(self.pair, {"type": "flat", "processed": True})
                
                if not signal.get("processed", True):
                    # CRITICAL FIX: Check position FIRST before logging anything to avoid "NEW SIGNAL RECEIVED" spam
                    if self._should_check_position(signal):
                        pos = await self._position()
                    else:
                        pos = None
                    
                    # CRITICAL FIX: Check for same-direction signal BEFORE logging "NEW SIGNAL RECEIVED"
                    if pos and self.state["sl"] != 0 and signal["type"].upper() == pos["side"]:
                        # This is a same-direction signal - handle as ignored (will log "SIGNAL IGNORED")
                        await self._on_position(pos, price, signal)
                        self._save_state()
                        return
                    
                    # NEW: Track signal time
                    self.signal_time = now()
                    
                    # Only log "NEW SIGNAL RECEIVED" if it's actually a new/different direction signal
                    self._summary_log(f"🔔 NEW SIGNAL RECEIVED: {signal['type'].upper()}")
                    self._detailed_log(f"Signal processing: {signal}")
                    
                    # NEW: Log signal event
                    self._log_trade_event("SIGNAL_RECEIVED", {
                        "signal_type": signal['type'].upper(),
                        "signal_time": self.signal_time,
                        "price": price
                    })
                    
                    # FILE MODE: keep processed=false until entry is filled; WS mode unchanged
                    try:
                        cfg = load_json(CONFIG_PATH, {}) or {}
                        src = str(cfg.get("signals", {}).get("source", "file")).lower()
                    except Exception:
                        src = "file"
                    if src != "file":
                        signal["processed"] = True
                        signals[self.pair] = signal
                        save_json(SIGNALS_PATH, signals)
                        self._detailed_log(f"Signal marked as processed")
                    
                    # Reset state for new signal
                    if signal['type'].upper() != 'FLAT':
                        self.state.update({"tp": 0, "sl": 0, "last": 0})
                        self.position_id += 1
                        # SIGNAL DEDUPLICATION: Reset ignored signal tracking for new signal
                        self.last_ignored_signal_type = None
                        # FLAT SIGNAL DEDUPLICATION: Reset flat signal processing for new non-flat signal
                        self.flat_signal_processed = False
                        self._detailed_log(f"State reset for new signal, position_id: {self.position_id}")
                    
                    # CRITICAL FIX: Process the new signal immediately in this tick
                    # NOTE: pos was already checked above, reuse it
                    
                    # CRITICAL FIX: Validate position state before processing
                    # FIX: Reset entry order state if position exists but entry order is still active
                    if pos and self.entry_id:
                        self._detailed_log(f"Position detected but entry order still active. Cancelling entry order and resetting state.")
                        try:
                            await self._cancel(self.entry_id)
                            self._detailed_log(f"Successfully cancelled entry order: {self.entry_id}")
                        except Exception as e:
                            self._detailed_log(f"Failed to cancel entry order {self.entry_id}: {e}")
                        self.entry_id = None
                        self._detailed_log(f"Entry order state reset - ready for new trades")
                    if not await self._validate_position_state(pos):
                        self._detailed_log("Position state validation failed, skipping tick")
                        self._save_state()
                        return
                    
                    signal_type = signal.get('type', 'flat').upper()
                    self._detailed_log(f"TICK (NEW SIGNAL): Price={price:.5f} | Signal={signal_type} | HasPosition={pos is not None} | EntryOrder={self.entry_id is not None}")
                    
                    if pos:
                        await self._on_position(pos, price, signal)
                    else:
                        if self.entry_id:
                            # Maintain existing entry: re-peg to edge even while processed=false
                            stype = str(signal.get('type', 'flat')).upper()
                            await self._adapt_entry(price, stype)
                        else:
                            try:
                                open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
                            except Exception:
                                open_orders = []
                            has_open_entry = any((o.get("clientOrderId", "").startswith(f"{self.pair}_open")) for o in open_orders)
                            if has_open_entry:
                                self._detailed_log("Venue already has an entry order; maintaining edge")
                                stype = str(signal.get('type', 'flat')).upper()
                                await self._adapt_entry(price, stype)
                            else:
                                stype = str(signal.get('type', 'flat')).upper()
                                await self._place_new_entry_order(price, stype)
                    self._save_state()
                    return
        
        # Normal tick processing for existing signals - intelligent position checking
        if self._should_check_position(signal):
            pos = await self._position()
            
            # HIGH PRIORITY FIX: Proactive position state synchronization
            # Check if we have a position state mismatch and fix it immediately
            # CRITICAL: Be very conservative - don't clear state on single position check failure
            if not pos and (self.state["sl"] != 0 or self.state["tp"] != 0):
                # No position but we think we have one - this could be temporary API issue
                # Only clear state if we're very confident the position is actually closed
                if not self.exit_id and not self.emergency_cid:  
                    # Double-check with a second position query to avoid false positives
                    await asyncio.sleep(0.5)  # Brief delay to avoid rate limits
                    pos_recheck = await self._position()
                    if not pos_recheck:
                        # Still no position after recheck - but be even more conservative
                        # Only clear if we haven't had a position for multiple checks
                        if not hasattr(self, '_no_position_count'):
                            self._no_position_count = 0
                        self._no_position_count += 1
                        
                        # Require 3 consecutive "no position" detections before clearing state
                        if self._no_position_count >= 3:
                            self._summary_log("🔄 PROACTIVE_SYNC: Clearing stale position state after multiple confirmations")
                            self.state.update({"tp": 0, "sl": 0, "last": now()})
                            self._no_position_count = 0
                        else:
                            self._detailed_log(f"PROACTIVE_SYNC: No position detected ({self._no_position_count}/3 checks) - waiting for confirmation")
                    else:
                        # Position found on recheck - reset counter
                        self._no_position_count = 0
                        pos = pos_recheck  # Use the rechecked position
            elif pos:
                # Reset the no-position counter when we have a position
                self._no_position_count = 0
                if self.state["sl"] == 0 and self.state["tp"] == 0:
                    # We have a position but no internal state - this shouldn't happen in normal operation
                    # but can occur after bot restarts or state corruption
                    self._summary_log("⚠️ PROACTIVE_SYNC: Position detected but no internal state - investigation needed")
                    self._detailed_log(f"Position: {pos}, Internal SL: {self.state['sl']}, Internal TP: {self.state['tp']}")
        else:
            # Skip expensive position check for inactive pairs
            self._detailed_log("SKIP: Inactive pair - no position check needed")
            await asyncio.sleep(2.0)  # Sleep longer for inactive pairs
            return
        
        # Periodic position status logging (every 30 minutes)
        if pos:
            self._log_periodic_position_status(pos, price)
        
        # CRITICAL FIX: Cancel entry order if position exists but entry order is still active
        if pos and self.entry_id:
            self._detailed_log(f"Position detected but entry order still active. Cancelling entry order and resetting state.")
            try:
                await self._cancel(self.entry_id)
                self._detailed_log(f"Successfully cancelled entry order: {self.entry_id}")
            except Exception as e:
                self._detailed_log(f"Failed to cancel entry order {self.entry_id}: {e}")
            self.entry_id = None
            self._detailed_log(f"Entry order state reset - ready for new trades")
        
        # CRITICAL FIX: Validate position state before processing
        if not await self._validate_position_state(pos):
            self._detailed_log("Position state validation failed during normal tick")
            self._save_state()
            return
        
        signal_type = signal.get('type', 'flat').upper()
        self._detailed_log(f"TICK: Price={price:.5f} | Signal={signal_type} | HasPosition={pos is not None} | EntryOrder={self.entry_id is not None}")
        
        if pos:
            await self._on_position(pos, price, signal)
        else:
            await self._no_position(price, signal)
        self._save_state()

    def _should_update_tp(self, side: str, current_tp: float, new_tp: float, current_price: float) -> bool:
        """
        Check if TP should be updated based on maxer threshold.
        For LONG: |current_order_price - new_price| > maxer
        For SHORT: |new_price - current_order_price| > maxer
        
        This prevents excessive TP updates due to small price movements.
        """
        if current_tp == 0 or new_tp == 0:
            return True  # Always update if no TP set yet
        
        # Calculate the effective order price (TP target)
        current_order_price = current_tp
        new_order_price = new_tp
        
        if side == "LONG":
            price_difference = abs(current_order_price - new_order_price)
        else:  # SHORT
            price_difference = abs(new_order_price - current_order_price)
        
        should_update = price_difference >= self.pc.maxer
        
        # CONCISE: Simple status showing TP drift check
        if should_update:
            self._summary_log(f"📈 TP DRIFT: Current {current_tp:.5f} | New {new_tp:.5f} | Drift {price_difference:.5f} >= {self.pc.maxer:.5f}")
        else:
            self._summary_log(f"📊 TP OK: Current {current_tp:.5f} | New {new_tp:.5f} | Drift {price_difference:.5f} < {self.pc.maxer:.5f}")
        
        return should_update

    async def _on_position(self, pos, price, signal):
        side = pos["side"]
        opp = "SHORT" if side == "LONG" else "LONG"
        qty = pos["qty"]

        # Check if this is a same-direction signal for an existing position
        if self.state["sl"] != 0 and signal["type"].upper() == side:
            # SIGNAL DEDUPLICATION: Only log "SIGNAL IGNORED" once per signal transition
            current_signal_type = signal["type"].upper()
            if self.last_ignored_signal_type != current_signal_type:
                # This is a NEW signal transition, log it once
                self.last_ignored_signal_type = current_signal_type
                
                # Calculate position age and unrealized PnL
                position_age = 0
                if self.trade_log:
                    entry_time = self.trade_log[-1].get("entry_time", now())
                    position_age = int(now() - entry_time)
                
                # Calculate unrealized PnL
                entry_price = pos["entry"]
                if side == "LONG":
                    unrealized_pnl = (price - entry_price) * qty
                else:
                    unrealized_pnl = (entry_price - price) * qty
                
                # Log the cleaner 2-line format for same-direction signals (ONCE per transition)
                self._summary_log(f"ℹ️ SIGNAL IGNORED: {side} position already active | Entry={entry_price:.2f}@{qty:.3f} | Unrealized={unrealized_pnl:+.2f} | Age={position_age}s")
            
            # Always return (whether we logged or not) to prevent processing same-direction signal
            return

        # CRITICAL FIX: Only set SL and create trade record ONCE per position
        if self.state["sl"] == 0:
            # NEW: Reset trailing TP state on fresh position to avoid stale extremums
            try:
                base_price = float(price)
            except Exception:
                base_price = float(pos.get("entry", 0) or 0)
            self.max_price_reached = base_price
            self.min_price_reached = base_price
            self.trailing_activated = False
            self.tp_armed_once = False
            self.state["tp"] = 0
            calculated_sl = self._calc_sl(side, pos["entry"])
            self.state["sl"] = calculated_sl
            self._summary_log(f"🛡️ STOP LOSS SET: {self.state['sl']:.5f}")
            self._detailed_log(f"SL CALCULATION: side={side}, entry={pos['entry']}, stop_loss_percentage={self.pc.stop_loss:.3f}")
            self._detailed_log(f"SL FORMULA: {pos['entry']} * (1 - {self.pc.stop_loss:.3f}) = {calculated_sl}")
            
            # FIX: Initialize emergency SL based on regular stop loss (not entry price)
            initial_emergency_sl = self._calc_emergency_sl(side, self.state["sl"])
            self.emergency_sl_price = initial_emergency_sl
            self.emergency_sl_initialized = True  # Mark as initialized
            self._summary_log(f"🛡️ EMERGENCY STOP LOSS INITIALIZED: {self.emergency_sl_price:.5f}")
            self._detailed_log(f"EMERGENCY SL CALCULATION: side={side}, sl_reference={self.state['sl']}, emergency_sl_percentage={self.pc.emergency_stop_loss:.3f}")
            self._detailed_log(f"EMERGENCY SL FORMULA: {self.state['sl']} * (1 - {self.pc.emergency_stop_loss:.3f}) = {initial_emergency_sl}")
            
            # CRITICAL FIX: Only create ONE trade record per position
            trade_record = {
                "position_id": self.position_id,
                "entry_price": pos["entry"],
                "entry_qty": pos["qty"],
                "side": side,
                "sl_price": self.state["sl"],
                "emergency_sl_price": self.emergency_sl_price,
                "entry_time": now()
            }
            self.trade_log.append(trade_record)
            self._summary_log(f"📊 POSITION OPENED: ID={self.position_id} | {side} {pos['qty']}@{pos['entry']:.5f} | SL={self.state['sl']:.5f} | Emergency SL={self.emergency_sl_price:.5f}")
            self._detailed_log(f"Trade record created: {trade_record}")
            
            # CRITICAL FIX: Set position_confirmed for new positions to enable ESL placement
            self.position_confirmed = True
            
            # Place on-exchange emergency stop loss order only if position was confirmed/adopted
            try:
                if getattr(self, "position_confirmed", False):
                    cid = f"esl-{self.pair}-{int(time.time()*1000)}"
                    eside = "SELL" if side == "LONG" else "BUY"
                    # NEW: Validate ESL trigger direction vs current price to avoid 'would immediately trigger'
                    valid_stop = float(self.emergency_sl_price)
                    try:
                        # Use last known price as guard; if missing, keep as-is
                        current_price = float(pos["entry"]) if pos and "entry" in pos else float(self.emergency_sl_price)
                        if eside == "BUY" and valid_stop <= current_price:
                            # For short protection, BUY stop must be above market
                            valid_stop = round(current_price * (1 + max(0.001, self.pc.tick_size or 0)), self.pc.precision)
                        elif eside == "SELL" and valid_stop >= current_price:
                            # For long protection, SELL stop must be below market
                            valid_stop = round(current_price * (1 - max(0.001, self.pc.tick_size or 0)), self.pc.precision)
                    except Exception:
                        pass

                    # CRITICAL FIX: Check if ESL was actually placed successfully
                    esl_result = await self._ensure_single_esl(side, float(valid_stop))
                    if esl_result:
                        self.emergency_cid = esl_result
                        self._summary_log(f"🛡️ EMERGENCY STOP ORDER PLACED: cid={esl_result} stopPrice={valid_stop:.5f}")
                    else:
                        self._summary_log(f"❌ EMERGENCY STOP PLACEMENT FAILED: _ensure_single_esl returned None for stopPrice={valid_stop:.5f}")
                        # CRITICAL: Don't set emergency_cid if placement failed
                        self.emergency_cid = None
                else:
                    self._detailed_log("ESL placement skipped: position not yet confirmed by WS snapshot")
            except Exception as e:
                self._summary_log(f"❌ EMERGENCY STOP PLACE FAILED: {e}")

        # Update trailing TP only if TP is already set (after activation)
        if self.state["tp"] > 0:
            new_tp = self._trail_tp(side, price, self.state["tp"], pos["entry"])
            
            # FIX: Only update TP if the price difference exceeds maxer threshold
            if new_tp != self.state["tp"] and self._should_update_tp(side, self.state["tp"], new_tp, price):
                # CONCISE: Clear drift detection for TP updates
                price_difference = abs(self.state["tp"] - new_tp) if side == "LONG" else abs(new_tp - self.state["tp"])
                self._summary_log(f"📈 TP DRIFT: Current {self.state['tp']:.5f} | New {new_tp:.5f} | Drift {price_difference:.5f} >= {self.pc.maxer:.5f}")
                old_tp = self.state["tp"]
                self.state["tp"] = new_tp
                profit_potential = new_tp - pos["entry"] if side == "LONG" else pos["entry"] - new_tp
                
                # ENHANCED LOGGING: Trailing TP update details
                self._summary_log(f"📈 TAKE PROFIT TRAILED:")
                self._summary_log(f"   Old TP: {old_tp:.5f}")
                self._summary_log(f"   New TP: {new_tp:.5f}")
                self._summary_log(f"   TP Change: {new_tp - old_tp:+.5f}")
                self._summary_log(f"   Current Price: {price:.5f}")
                self._summary_log(f"   Potential Profit: +{profit_potential:.5f} per unit")
                
                if side == "LONG":
                    profit_percent = (profit_potential / pos["entry"]) * 100
                    self._summary_log(f"   Profit %: +{profit_percent:.3f}%")
                else:
                    profit_percent = (profit_potential / pos["entry"]) * 100
                    self._summary_log(f"   Profit %: +{profit_percent:.3f}%")
                
                self._detailed_log(f"TP trailed from {old_tp} to {new_tp} at price {price}")
                
                # NEW: Update emergency SL when TP moves (if trailing is activated)
                if self.trailing_activated:
                    old_emergency_sl = self.emergency_sl_price
                    new_emergency_sl = self._calc_emergency_sl(side, new_tp)
                    
                    if (side == "LONG" and new_emergency_sl > old_emergency_sl) or (side == "SHORT" and new_emergency_sl < old_emergency_sl):
                        self.emergency_sl_price = new_emergency_sl
                        self._summary_log(f"🛡️ EMERGENCY SL TRAILED WITH TP:")
                        self._summary_log(f"   Old Emergency SL: {old_emergency_sl:.5f}")
                        self._summary_log(f"   New Emergency SL: {new_emergency_sl:.5f}")
                        self._summary_log(f"   Emergency SL Change: {new_emergency_sl - old_emergency_sl:+.5f}")
                        self._detailed_log(f"Emergency SL trailed from {old_emergency_sl} to {new_emergency_sl} with TP update")
                        
                        # Update emergency stop loss order if it exists
                        if self.emergency_cid and self.order_manager:
                            try:
                                eside = "SELL" if side == "LONG" else "BUY"
                                await self.order_manager.modify_emergency_stop(self.pair, self.emergency_cid, eside, float(self.emergency_sl_price))
                                self._summary_log(f"🛡️ ESL_UPDATED: {old_emergency_sl:.5f}→{self.emergency_sl_price:.5f} | Reason=trailing_tp | New_Reference=TP@{self.state['tp']:.5f}")
                            except Exception as e:
                                self._summary_log(f"❌ EMERGENCY STOP AMEND FAILED: {e}")
                
                # Update trade record with new TP
                if self.trade_log:
                    self.trade_log[-1]["tp_price"] = new_tp
            elif new_tp != self.state["tp"]:
                # TP changed but difference is too small - log but don't update
                price_difference = abs(self.state["tp"] - new_tp) if side == "LONG" else abs(new_tp - self.state["tp"])
                self._detailed_log(f"📊 TP CHANGE IGNORED: New TP {new_tp:.5f} vs Current TP {self.state['tp']:.5f} | Drift={price_difference:.5f} < Max={self.pc.maxer:.5f}")
        else:
            # TP not set yet - check if we should activate trailing
            activation_price = round(pos["entry"] * (1 + self.pc.trailing), self.pc.precision) if side == "LONG" else round(pos["entry"] * (1 - self.pc.trailing), self.pc.precision)
            if ((side == "LONG" and price >= activation_price) or (side == "SHORT" and price <= activation_price)) and not self.trailing_activated:
                # NEW: Track TP activation time
                self.tp_activation_time = now()
                
                # CRITICAL FIX: Set trailing_activated flag
                self.trailing_activated = True
                
                # Activate trailing and check if TP should be set
                initial_tp_result = self._trail_tp(side, price, 0, pos["entry"])
                if initial_tp_result == 0: # If no TP was set yet
                    # Don't set TP immediately - wait for price reversal
                    extremum = self.max_price_reached if side == "LONG" else self.min_price_reached
                    self._summary_log(f"🎯 TRAILING_ACTIVATED: Price={price:.5f} crossed threshold={activation_price:.5f} | Extremum={extremum:.5f} | Waiting for reversal")
                else:
                    # TP was set (this shouldn't happen with new logic, but handle it)
                    self.state["tp"] = initial_tp_result
                    self._summary_log(f"🎯 TRAILING ACTIVATED: TP set to {self.state['tp']:.5f}")
                
                # NEW: Activate emergency SL relative to TP only if TP is set
                if self.state["tp"] > 0:
                    old_emergency_sl = self.emergency_sl_price
                    new_emergency_sl = self._calc_emergency_sl(side, self.state["tp"])
                    self.emergency_sl_price = new_emergency_sl
                    self.emergency_sl_activated = True
                    self.emergency_sl_tp_reference = self.state["tp"]
                    
                    self._summary_log(f"🛡️ ESL_UPDATED: {old_emergency_sl:.5f}→{new_emergency_sl:.5f} | Reason=trailing_activation | New_Reference=TP@{self.state['tp']:.5f}")
                else:
                    # TP not set yet - keep existing emergency SL
                    self._summary_log(f"🛡️ EMERGENCY SL: Keeping existing SL until TP is set")
                
                # NEW: Log TP activation event
                self._log_trade_event("TP_ACTIVATED", {
                    "activation_price": activation_price,
                    "current_price": price,
                    "tp_price": self.state["tp"],
                    "emergency_sl_price": self.emergency_sl_price,
                    "activation_time": self.tp_activation_time,
                    "time_from_entry": self.tp_activation_time - self.entry_fill_time if self.entry_fill_time else None,
                    "side": side
                })
                
                # Update trade record with TP
                if self.trade_log:
                    self.trade_log[-1]["tp_price"] = self.state["tp"]
            elif self.trailing_activated and self.state["tp"] == 0:
                # NEW: After activation, continue checking for reversal to set TP
                tp_after_reversal = self._trail_tp(side, price, 0, pos["entry"])
                if tp_after_reversal > 0:
                    self.state["tp"] = tp_after_reversal
                    self._summary_log(f"🎯 TRAILING REVERSAL: TP set to {self.state['tp']:.5f}")
                    
                    # Activate emergency SL relative to the newly set TP
                    old_emergency_sl = self.emergency_sl_price
                    new_emergency_sl = self._calc_emergency_sl(side, self.state["tp"])
                    self.emergency_sl_price = new_emergency_sl
                    self.emergency_sl_activated = True
                    self.emergency_sl_tp_reference = self.state["tp"]
                    
                    self._summary_log(f"🛡️ ESL_UPDATED: {old_emergency_sl:.5f}→{new_emergency_sl:.5f} | Reason=trailing_reversal | New_Reference=TP@{self.state['tp']:.5f}")
                    
                    # Update trade record with TP
                    if self.trade_log:
                        self.trade_log[-1]["tp_price"] = self.state["tp"]

        # Check exit conditions - FIXED: Place orders when price crosses TP/SL, not just hits them
        tp_hit = (side == "LONG" and self.state["tp"] > 0 and price <= self.state["tp"]) or (
            side == "SHORT" and self.state["tp"] > 0 and price >= self.state["tp"]) 
        sl_hit = (side == "LONG" and price <= self.state["sl"]) or (
            side == "SHORT" and price >= self.state["sl"])

        # NEW: Manage existing exit orders with controller-based re-peg
        if self.exit_id and not sl_hit and not tp_hit and signal["type"].upper() != opp:
            try:
                order_side = "SELL" if side == "LONG" else "BUY"
                # Determine remaining qty if possible
                rem_qty = qty
                try:
                    es = await (self.order_manager.get_order(self.pair, self.exit_id) if self.order_manager else self.rest.get_order(self.pair, self.exit_id))
                    if es and es.get("status") == "NEW":
                        rem_qty = max(0.0, float(es.get("origQty", 0)) - float(es.get("executedQty", 0)))
                except Exception:
                    pass
                # CRITICAL FIX: Use the same controller that placed the current exit order
                # This prevents state mismatches between multiple MakerOrderController instances
                exit_controller = self._get_current_exit_controller()
                await exit_controller.amend_edge_if_needed(side=order_side, qty=rem_qty, reduce_only=True)
            except Exception as e:
                self._detailed_log(f"Exit maintenance via controller failed: {e}")

        # CRITICAL FIX: Handle SL hit with proper position closure
        if sl_hit:
            # Calculate the desired exit order price (maker) using edge-pegged resolver via controller
            desired_price = price
            self._summary_log(f"🔻 STOP LOSS TRIGGERED: Price {price:.5f} <= SL {self.state['sl']:.5f}")

            # If an exit already exists, only update it when drift >= maxer; otherwise place a new one
            if self.exit_id:
                try:
                    status = await (self.order_manager.get_order(self.pair, self.exit_id) if self.order_manager else self.rest.get_order(self.pair, self.exit_id))
                    if status and status.get("status") == "NEW":
                        cur_price = float(status.get("price", 0))
                        order_side = "SELL" if side == "LONG" else "BUY"
                        try:
                            orig_qty = float(status.get("origQty", 0))
                            exec_qty = float(status.get("executedQty", 0))
                            remaining_qty = max(0.0, orig_qty - exec_qty)
                        except Exception:
                            remaining_qty = qty
                        # CRITICAL FIX: Use consistent controller for both amendment and placement
                        # This is SL logic, so use sl_ctrl to match the subsequent _place_exit(..., reason="SL")
                        exit_controller = self._get_exit_controller("SL")
                        await exit_controller.amend_edge_if_needed(side=order_side, qty=remaining_qty, reduce_only=True)
                        return
                except Exception as e:
                    if "-2013" in str(e):
                        self._detailed_log(f"Exit order not found on exchange (-2013). Clearing exit_id {self.exit_id} and continuing.")
                        self.exit_id = None
                    else:
                        self._detailed_log(f"Error checking exit order status for SL: {e}")

            if not self.exit_id or (status and status.get("status") != "NEW"):
                self._summary_log(f"PLACING EXIT ORDER (SL): edge-pegged from market {price:.5f}")
                # Use remaining position size for reduce-only SL if we have it; otherwise fallback to qty
                try:
                    orig_qty = float(status.get("origQty", 0)) if status else 0.0
                    exec_qty = float(status.get("executedQty", 0)) if status else 0.0
                    remaining_qty = max(0.0, orig_qty - exec_qty) if status else qty
                except Exception:
                    remaining_qty = qty
                await self._place_exit(side, remaining_qty, price, reason="SL")

            loss = pos["entry"] - self.state["sl"] if side == "LONG" else self.state["sl"] - pos["entry"]
            # Added clarity logs for SL trigger context
            self._detailed_log(f"🧷 SL CHECK: side={side} price={price:.5f} sl={self.state['sl']:.5f} hit={(side == 'LONG' and price <= self.state['sl']) or (side == 'SHORT' and price >= self.state['sl'])}")
            if self.trade_log:
                self.trade_log[-1].update({
                    "exit_price": price,
                    "exit_qty": qty,
                    "exit_reason": "STOP_LOSS",
                    "exit_time": now(),
                    "pnl": -loss * qty
                })
            return
        
        # CRITICAL FIX: Handle TP hit or opposite signal with proper position closure
        elif tp_hit or signal["type"].upper() == opp:
            if tp_hit:
                # Edge-pegged exit order price comes from controller, use current market as base
                exit_order_price = price
                
                profit = self.state["tp"] - pos["entry"] if side == "LONG" else pos["entry"] - self.state["tp"]
                exit_reason = "TAKE_PROFIT"
                self._summary_log(f"🎯 TAKE PROFIT HIT: {self.state['tp']:.5f} | Profit: +{profit:.5f} per unit | Total: +{profit * qty:.2f}")
                # If exit already exists, attempt WS amend first when drift >= maxer
                amended = False
                if self.exit_id:
                    try:
                        status = await (self.order_manager.get_order(self.pair, self.exit_id) if self.order_manager else self.rest.get_order(self.pair, self.exit_id))
                        if status and status.get("status") == "NEW":
                            cur_price = float(status.get("price", 0))
                            order_side = "SELL" if side == "LONG" else "BUY"
                            try:
                                orig_qty = float(status.get("origQty", 0))
                                exec_qty = float(status.get("executedQty", 0))
                                remaining_qty = max(0.0, orig_qty - exec_qty)
                            except Exception:
                                remaining_qty = qty
                            # CRITICAL FIX: Use consistent controller for both amendment and placement
                            # This prevents state mismatch between exit_ctrl and tp_ctrl
                            exit_controller = self._get_exit_controller("TP")
                            await exit_controller.amend_edge_if_needed(side=order_side, qty=remaining_qty, reduce_only=True)
                            amended = True
                    except Exception:
                        # WS amend failed: cancel+place once
                        await self._cancel(self.exit_id)
                        # fall through to placement
                if not amended:
                    self._summary_log(f"PLACING EXIT ORDER (TP): side={'SELL' if side == 'LONG' else 'BUY'} level={self.state['tp']:.5f} from market {price:.5f} (edge-pegged)")
                    await self._place_exit(side, qty, price, reason="TP")
            else:
                profit = price - pos["entry"] if side == "LONG" else pos["entry"] - price
                exit_reason = "OPPOSITE_SIGNAL"
                # Throttle opposite signal logging to once per second to reduce log noise
                current_time = now()
                if current_time - self.last_opposite_signal_log >= 1.0:  # 1 second minimum
                    self._summary_log(f"🔄 OPPOSITE SIGNAL: Closing {side} position due to {signal['type']} signal | Profit: {profit:.5f} per unit")
                    self.last_opposite_signal_log = current_time
                # Use limit order for TP execution as maker
                await self._place_exit(side, qty, price, reason="Signal")
            
            # Update trade record
            if self.trade_log:
                self.trade_log[-1].update({
                    "exit_price": price,  # Use current price
                    "exit_qty": qty,
                    "exit_reason": exit_reason,
                    "exit_time": now(),
                    "pnl": (price - pos["entry"]) * qty if side == "LONG" else (pos["entry"] - price) * qty
                })
            
            # FIX: Don't reset exit_id immediately - let drift management work
            # self.exit_id = None  # REMOVED: Let drift management handle the exit order
            return

        # CRITICAL FIX: Only maintain exit orders if we don't have critical exit conditions
        # This prevents the constant order replacement seen in logs
        if self.exit_id and not sl_hit and not tp_hit and signal["type"].upper() != opp:
            # Controller-driven maintenance: re-peg exit when needed
            order_side = "SELL" if side == "LONG" else "BUY"
            try:
                # Use current remaining qty if exit exists; else fall back to position qty
                rem_qty = qty
                if self.exit_id:
                    try:
                        es = await (self.order_manager.get_order(self.pair, self.exit_id) if self.order_manager else self.rest.get_order(self.pair, self.exit_id))
                        if es and es.get("status") == "NEW":
                            rem_qty = max(0.0, float(es.get("origQty", 0)) - float(es.get("executedQty", 0)))
                    except Exception:
                        pass
                # CRITICAL FIX: Use the same controller that placed the current exit order
                exit_controller = self._get_current_exit_controller()
                await exit_controller.amend_edge_if_needed(side=order_side, qty=rem_qty, reduce_only=True)
            except Exception as e:
                self._detailed_log(f"Exit maintenance via controller failed: {e}")
            # Enhanced: poll and emergency SL
            await self.enhanced_order_polling()
            await self._check_emergency_sl(side, price)

    async def _check_emergency_sl(self, side: str, price: float):
        """Check and execute emergency stop loss with market orders"""
        if self.state["sl"] == 0 or self.emergency_triggered:
            return
            
        # Calculate emergency SL price based on current state
        if self.trailing_activated and self.state["tp"] > 0:
            # Emergency SL is relative to current trailing TP
            reference_price = self.state["tp"]
            if not self.emergency_sl_activated:
                self.emergency_sl_activated = True
                self.emergency_sl_tp_reference = reference_price
                self._detailed_log(f"🎯 EMERGENCY SL ACTIVATED: TP reference price set to {reference_price:.5f}")
        else:
            # Emergency SL is relative to regular stop loss
            reference_price = self.state["sl"]
            
        # Calculate new emergency SL price
        new_emergency_sl = self._calc_emergency_sl(side, reference_price)
        
        # Update emergency SL price if it's better (higher for LONG, lower for SHORT)
        if self.emergency_sl_price == 0 or (side == "LONG" and new_emergency_sl > self.emergency_sl_price) or (side == "SHORT" and new_emergency_sl < self.emergency_sl_price):
            old_emergency_sl = self.emergency_sl_price
            self.emergency_sl_price = new_emergency_sl
            
            self._detailed_log(f"🛡️ EMERGENCY SL UPDATED:")
            self._detailed_log(f"   Side: {side}")
            self._detailed_log(f"   Reference Price: {reference_price:.5f}")
            self._detailed_log(f"   Emergency SL %: {self.pc.emergency_stop_loss:.3f}")
            self._detailed_log(f"   Old Emergency SL: {old_emergency_sl:.5f}")
            self._detailed_log(f"   New Emergency SL: {self.emergency_sl_price:.5f}")
            self._detailed_log(f"   Current Market Price: {price:.5f}")
        
        # Check if emergency SL is triggered
        hit = (side == "LONG" and price <= self.emergency_sl_price) or (side == "SHORT" and price >= self.emergency_sl_price)
        self._detailed_log(f"🛡️ EMERGENCY SL CHECK: side={side} price={price:.5f} emergency_sl={self.emergency_sl_price:.5f} hit={hit}")
        
        if hit:
            pos = await self._position()
            if pos:
                # Execute market order immediately
                order_side = "SELL" if side == "LONG" else "BUY"
                execution_time = now()
                
                self._summary_log(f"🚨 EMERGENCY STOP LOSS TRIGGERED!")
                self._summary_log(f"   Side: {side}")
                self._summary_log(f"   Market Price: {price:.5f}")
                self._summary_log(f"   Emergency SL Price: {self.emergency_sl_price:.5f}")
                self._summary_log(f"   Executing {order_side} market order")
                self._summary_log(f"   Execution Time: {execution_time}")
                
                self._detailed_log(f"🚨 EMERGENCY SL EXECUTION:")
                self._detailed_log(f"   Market order: {order_side} {pos['qty']} @ MARKET")
                self._detailed_log(f"   Trigger price: {price:.5f} <= Emergency SL: {self.emergency_sl_price:.5f}")
                self._detailed_log(f"   Execution timestamp: {execution_time}")
                
                try:
                    # Execute market order (WS-first)
                    if self.order_manager:
                        await self.order_manager.place_market(self.pair, order_side, pos["qty"], True)
                    else:
                        await self.rest.place_market(self.pair, order_side, pos["qty"], True)
                    
                    # Log successful execution
                    self._summary_log(f"✅ EMERGENCY SL MARKET ORDER EXECUTED SUCCESSFULLY")
                    self._detailed_log(f"Emergency SL market order filled: {order_side} {pos['qty']} @ MARKET")
                    
                    # Update trade record
                    if self.trade_log:
                        self.trade_log[-1].update({
                            "exit_price": price,
                            "exit_qty": pos["qty"],
                            "exit_reason": "EMERGENCY_STOP_LOSS",
                            "exit_time": execution_time,
                            "pnl": (price - pos["entry"]) * pos["qty"] if side == "LONG" else (pos["entry"] - price) * pos["qty"]
                        })
                    
                    # Reset state
                    self.emergency_triggered = True
                    self.exit_id = None
                    self.state.update({"tp": 0, "sl": 0, "last": execution_time})
                    
                except Exception as e:
                    self._summary_log(f"❌ EMERGENCY SL MARKET ORDER FAILED: {e}")
                    self._detailed_log(f"Emergency SL market order failed: {e}")
                    # Don't set emergency_triggered to True so we can retry
    async def get_order_status_with_retry(self, order_id: str, max_retries: int = 3):
        """Get order status with retry logic for better reliability"""
        for attempt in range(max_retries):
            try:
                status = await (self.order_manager.get_order(self.pair, order_id) if self.order_manager else self.rest.get_order(self.pair, order_id))
                if status:
                    # Order found - return the status
                    self._detailed_log(f"Order status retrieved: {order_id} -> {status.get('status', 'UNKNOWN')}")
                    return status
                else:
                    # Order not found - likely filled or cleared
                    self._detailed_log(f"Order not found (likely filled): {order_id}")
                    return None
            except Exception as e:
                error_msg = str(e)
                if "-2013" in error_msg or "Unknown order" in error_msg:
                    # Treat as non-fatal: clear and continue
                    self._detailed_log(f"Order not found via API (-2013/Unknown): {order_id} - {error_msg}")
                    return None
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.3 * (2 ** attempt))
                    continue
                raise

    async def handle_exit_order_filled(self, status, side, qty):
        """Handle filled exit order with comprehensive logging"""
        avg_price = float(status["avgPrice"])
        executed_qty = float(status["executedQty"])
        
        # NEW: Enhanced exit order fill tracking
        self.exit_fill_time = now()
        self.exit_order_fill_detected = True
        
        # Log structured exit order fill event
        self._log_exit_order_event("EXIT_ORDER_FILLED", {
            "order_id": self.exit_id,
            "executed_qty": executed_qty,
            "avg_price": avg_price,
            "side": side,
            "fill_time": self.exit_fill_time,
            "time_from_exit_order": self.exit_fill_time - self.exit_order_time if self.exit_order_time else None,
            "drift_count": self.drift_count
        })
        
        # Enhanced logging with consistent pattern
        self.log_exit_order_filled(self.exit_id, executed_qty, avg_price)
        
        # NEW: Clear and visible exit order fill notification
        self._summary_log(f"✅ EXIT ORDER FILLED: {self.exit_id}")
        self._summary_log(f"   Executed Qty: {executed_qty}")
        self._summary_log(f"   Avg Price: {avg_price:.5f}")
        self._summary_log(f"   Side: {side}")
        
        # Calculate and log P&L
        pnl_info = self.calculate_exit_pnl(avg_price, executed_qty, side)
        self.log_exit_pnl(pnl_info)
        
        # NEW: Log trade completion event
        if self.current_trade_id:
            trade_duration = self.exit_fill_time - self.trade_start_time if self.trade_start_time else None
            self._log_trade_event("TRADE_COMPLETED", {
                "trade_id": self.current_trade_id,
                "entry_time": self.entry_fill_time,
                "exit_time": self.exit_fill_time,
                "duration_seconds": trade_duration,
                "entry_price": pnl_info.get("entry_price", 0),
                "exit_price": avg_price,
                "pnl": pnl_info.get("pnl_total", 0),
                "drift_count": self.drift_count,
                "exit_reason": "EXIT_ORDER_FILLED"
            })
        
        # Reset state
        self.exit_id = None
        self.emergency_triggered = False
        self.state.update({"tp": 0, "sl": 0, "last": now()})
        
        # NEW: Clear position closure notification
        self._summary_log(f"📊 POSITION CLOSED: All orders canceled, ready for new trades")
        
        # NEW: Reset trade tracking variables
        self.current_trade_id = None
        self.trade_start_time = None
        self.signal_time = None
        self.entry_order_time = None
        self.entry_fill_time = None
        self.tp_activation_time = None
        self.exit_order_time = None
        self.exit_fill_time = None
        self.drift_count = 0
        self.last_drift_time = None
        self.exit_order_history = []
        self.exit_order_status_checks = 0
        self.exit_order_fill_detected = False
        
        # NEW: Reset emergency SL state
        self.emergency_sl_price = 0.0
        self.emergency_sl_activated = False
        self.emergency_sl_tp_reference = 0.0
        
        # NEW: Reset trailing TP state for fresh start on new positions
        self.max_price_reached = 0.0
        self.min_price_reached = 0.0
        self.trailing_activated = False
        self.tp_armed_once = False

        # NEW: WS-mode cooldown start if signal still active at exit
        try:
            cfg = load_json(CONFIG_PATH, {}) or {}
            signals_cfg = cfg.get("signals", {}) or {}
            trading_cfg = cfg.get("trading", {}) or {}
            if str(signals_cfg.get("source", "file")).lower() == "ws":
                # Compute cooldown seconds (prefer minutes if provided)
                cd_minutes = int(trading_cfg.get("cooldown_minutes", 60))
                cd_seconds = int(trading_cfg.get("cooldown_seconds", 0))
                cooldown_seconds = cd_seconds if (cd_minutes <= 0 and cd_seconds >= 0) else cd_minutes * 60
                if cooldown_seconds > 0:
                    # Check current signal type for this pair
                    signals = load_json(SIGNALS_PATH, {}) or {}
                    sig = signals.get(self.pair, {"type": "flat"})
                    stype = str(sig.get("type", "flat")).lower()
                    if stype in ("long", "short"):
                        # Start cooldown for this pair
                        cd_path = USER_STATE_DIR / "cooldowns.json"
                        existing_cds = load_json(cd_path, {}) or {}
                        existing_cds[self.pair] = int(time.time()) + cooldown_seconds
                        save_json(cd_path, existing_cds)
                        # Mark signal as processed during cooldown to ignore re-entry
                        sig["processed"] = True
                        signals[self.pair] = sig
                        save_json(SIGNALS_PATH, signals)
                        self._detailed_log(f"Cooldown started for {self.pair}: {cooldown_seconds}s (signal still {stype})")
        except Exception as e:
            self._detailed_log(f"Cooldown start error: {e}")

    def calculate_exit_pnl(self, exit_price: float, executed_qty: float, side: str):
        """Calculate P&L for exit order"""
        if not self.trade_log:
            return {"error": "No trade log available"}
        
        last_trade = self.trade_log[-1]
        entry_price = last_trade.get("entry_price", 0)
        trade_side = last_trade.get("side", "UNKNOWN")
        
        # FIX: Ensure correct P&L calculation with explicit validation
        if trade_side == "LONG":
            # For LONG trades: P&L = exit_price - entry_price 
            # (positive if we sold higher than bought, negative if we sold lower)
            pnl_per_unit = exit_price - entry_price
            pnl_total = pnl_per_unit * executed_qty
            pnl_percent = (pnl_per_unit / entry_price) * 100 if entry_price > 0 else 0
        elif trade_side == "SHORT":
            # For SHORT trades: P&L = entry_price - exit_price
            # (positive if we bought back lower than sold, negative if we bought back higher)
            pnl_per_unit = entry_price - exit_price
            pnl_total = pnl_per_unit * executed_qty
            pnl_percent = (pnl_per_unit / entry_price) * 100 if entry_price > 0 else 0
        else:
            # Fallback: use exit order side (old logic for backward compatibility)
            if side == "SELL":  # Closing LONG position
                pnl_per_unit = exit_price - entry_price
            else:  # side == "BUY" - Closing SHORT position
                pnl_per_unit = entry_price - exit_price
            pnl_total = pnl_per_unit * executed_qty
            pnl_percent = (pnl_per_unit / entry_price) * 100 if entry_price > 0 else 0
        
        # CRITICAL FIX: Validate the calculation makes sense
        if trade_side == "LONG":
            # For LONG trades: if exit_price < entry_price, it should be a LOSS
            if exit_price < entry_price and pnl_per_unit > 0:
                self._detailed_log(f"🚨 P&L CALCULATION ERROR: LONG trade with exit < entry but positive P&L!")
                self._detailed_log(f"   Entry: {entry_price:.5f}, Exit: {exit_price:.5f}, P&L: {pnl_per_unit:.5f}")
                # Force correct calculation
                pnl_per_unit = exit_price - entry_price
                pnl_total = pnl_per_unit * executed_qty
                pnl_percent = (pnl_per_unit / entry_price) * 100 if entry_price > 0 else 0
                self._detailed_log(f"   CORRECTED P&L: {pnl_per_unit:.5f}")
        elif trade_side == "SHORT":
            # For SHORT trades: if exit_price > entry_price, it should be a LOSS
            if exit_price > entry_price and pnl_per_unit > 0:
                self._detailed_log(f"🚨 P&L CALCULATION ERROR: SHORT trade with exit > entry but positive P&L!")
                self._detailed_log(f"   Entry: {entry_price:.5f}, Exit: {exit_price:.5f}, P&L: {pnl_per_unit:.5f}")
                # Force correct calculation
                pnl_per_unit = entry_price - exit_price
                pnl_total = pnl_per_unit * executed_qty
                pnl_percent = (pnl_per_unit / entry_price) * 100 if entry_price > 0 else 0
                self._detailed_log(f"   CORRECTED P&L: {pnl_per_unit:.5f}")
        
        # DEBUG: Add debug logging to verify calculation
        self._detailed_log(f"🔍 P&L CALCULATION DEBUG:")
        self._detailed_log(f"   Trade Side: {trade_side}")
        self._detailed_log(f"   Exit Side: {side}")
        self._detailed_log(f"   Entry Price: {entry_price:.5f}")
        self._detailed_log(f"   Exit Price: {exit_price:.5f}")
        self._detailed_log(f"   P&L per unit: {pnl_per_unit:.5f}")
        self._detailed_log(f"   P&L total: {pnl_total:.5f}")
        self._detailed_log(f"   Expected: {'LOSS' if pnl_per_unit < 0 else 'PROFIT'}")
        
        return {
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_per_unit": pnl_per_unit,
            "pnl_total": pnl_total,
            "pnl_percent": pnl_percent,
            "trade_side": trade_side,
            "exit_side": side,
            "executed_qty": executed_qty
        }

    def log_exit_pnl(self, pnl_info):
        """Log P&L information for exit order"""
        if "error" in pnl_info:
            self._summary_log(f"   P&L: {pnl_info['error']}")
            return
        
        # NEW: Enhanced P&L logging with clear visibility
        pnl_total = pnl_info.get("pnl_total", 0)
        pnl_per_unit = pnl_info.get("pnl_per_unit", 0)
        pnl_percent = pnl_info.get("pnl_percent", 0)
        
        if pnl_total > 0:
            self._summary_log(f"💰 PROFIT: +{pnl_total:.2f} (+{pnl_percent:.2f}%)")
        else:
            self._summary_log(f"💸 LOSS: {pnl_total:.2f} ({pnl_percent:.2f}%)")
        
        self._summary_log(f"   Entry: {pnl_info.get('entry_price', 0):.5f}")
        self._summary_log(f"   Exit: {pnl_info.get('exit_price', 0):.5f}")
        self._summary_log(f"   P&L per unit: {pnl_per_unit:+.5f}")
        self._summary_log(f"   Total P&L: {pnl_info['pnl_total']:+.2f} USDT")

    def log_exit_order_filled(self, order_id: str, executed_qty: float, avg_price: float):
        """Consistent exit order fill logging"""
        # Summary log (for main output)
        self._summary_log(f"🎉 EXIT ORDER FILLED:")
        self._summary_log(f"   Order ID: {order_id}")
        self._summary_log(f"   Executed Quantity: {executed_qty}")
        self._summary_log(f"   Average Price: {avg_price:.5f}")
        
        # Detailed log
        self._detailed_log(f"🎉 EXIT ORDER FILLED: {executed_qty} units at {avg_price:.5f}")
        
        # Dedicated exit order log
        exit_log = logging.getLogger("exit_orders")
        exit_log.info(f"EXIT_FILLED: {order_id} | Qty: {executed_qty} | Price: {avg_price:.5f}")

    def log_exit_order_canceled(self, order_id: str):
        """Log canceled exit order"""
        self._detailed_log(f"Exit order canceled: {order_id}")
        exit_log = logging.getLogger("exit_orders")
        exit_log.info(f"EXIT_CANCELED: {order_id}")

    def log_exit_order_expired(self, order_id: str):
        """Log expired exit order"""
        self._detailed_log(f"Exit order expired: {order_id}")
        exit_log = logging.getLogger("exit_orders")
        exit_log.info(f"EXIT_EXPIRED: {order_id}")

    def log_unknown_order_status(self, order_id: str, status: str):
        """Log unknown order status"""
        self._detailed_log(f"Unknown exit order status: {order_id} -> {status}")
        exit_log = logging.getLogger("exit_orders")
        exit_log.warning(f"EXIT_UNKNOWN_STATUS: {order_id} -> {status}")

    def log_exit_order_error(self, order_id: str, error: str):
        """Log exit order error"""
        self._detailed_log(f"Error checking exit order {order_id}: {error}")
        exit_log = logging.getLogger("exit_orders")
        exit_log.error(f"EXIT_ERROR: {order_id} -> {error}")

    async def enhanced_order_polling(self):
        """Enhanced order polling that runs independently of the main tick loop"""
        if not self.exit_id:
            return
        
        try:
            # Get current position to retrieve side and qty
            pos = await self._position()
            if not pos:
                # No position found - exit order was likely filled
                self._log_exit_order_event("EXIT_ORDER_POSITION_NOT_FOUND", {
                    "order_id": self.exit_id,
                    "likely_filled": True
                })
                return
            
            # Get current price
            current_price = await self._get_price()
            if not current_price:
                return
            
            await self._maintain_exit_order(pos["side"], pos["qty"], current_price)
        except Exception as e:
            self.log_exit_order_error(self.exit_id, str(e))
    async def _maintain_exit_order(self, side, qty, price):
        """Enhanced exit order maintenance with better logging and error handling"""
        if not self.exit_id:
            return
        
        # NEW: Track exit order status checks
        self.exit_order_status_checks += 1
        current_time = now()
        
        try:
            # Get order status with retry logic
            status = await self.get_order_status_with_retry(self.exit_id)
            
            if not status:
                # Order not found - likely filled
                self._log_exit_order_event("EXIT_ORDER_NOT_FOUND", {
                    "order_id": self.exit_id,
                    "status_checks": self.exit_order_status_checks,
                    "time_since_order": current_time - self.exit_order_time if self.exit_order_time else None,
                    "likely_filled": True
                })
                
                # FIX: Use the last known exit order price for P&L calculation instead of current market price
                # This provides more accurate P&L since we know the order was placed at a specific price
                last_exit_price = None
                if self.exit_order_history:
                    last_exit_price = self.exit_order_history[-1].get("price", price)
                else:
                    last_exit_price = price
                
                # Create status with last known exit order price for accurate P&L
                estimated_status = {
                    "status": "FILLED",
                    "avgPrice": str(last_exit_price),
                    "executedQty": str(qty)
                }
                await self.handle_exit_order_filled(estimated_status, side, qty)
                return
                
            order_status = status["status"]
            
            # NEW: Log exit order status check
            self._log_exit_order_event("EXIT_ORDER_STATUS_CHECK", {
                "order_id": self.exit_id,
                "status": order_status,
                "status_checks": self.exit_order_status_checks,
                "time_since_order": current_time - self.exit_order_time if self.exit_order_time else None
            })
            
            if order_status == "FILLED":
                await self.handle_exit_order_filled(status, side, qty)
            elif order_status == "PARTIALLY_FILLED":
                filled_qty = float(status["executedQty"])
                # FIXED: Calculate actual remaining position quantity after partial fill
                # qty = current position size from exchange
                # filled_qty = amount just filled by this partial execution
                remaining = abs(qty) - filled_qty  # Use abs() to handle both LONG/SHORT
                if remaining <= 0:
                    self._log_exit_order_event("EXIT_ORDER_PARTIAL_FILL_ZERO_REMAINING", {
                        "order_id": self.exit_id,
                        "filled_qty": filled_qty,
                        "original_position_qty": abs(qty),
                        "remaining_position": remaining
                    })
                    self._detailed_log(f"Partial fill completed position: filled={filled_qty}, original_position={abs(qty)}, remaining={remaining}")
                    self.exit_id = None
                    return
                
                self._log_exit_order_event("EXIT_ORDER_PARTIAL_FILL", {
                    "order_id": self.exit_id,
                    "filled_qty": filled_qty,
                    "original_position_qty": abs(qty),
                    "remaining_position": remaining,
                    "avg_price": float(status.get("avgPrice", 0))
                })
                
                self._detailed_log(f"Partial fill detected: filled={filled_qty}, remaining_position={remaining}")
                self._summary_log(f"🔄 PARTIAL_FILL_RECOVERY: Canceling and re-placing exit order for remaining {remaining}")
                await self._cancel(self.exit_id)
                await self._place_exit(side, remaining, price, "Partial")
            elif order_status == "CANCELED":
                self._log_exit_order_event("EXIT_ORDER_CANCELED", {
                    "order_id": self.exit_id,
                    "cancel_reason": "manual_cancel"
                })
                self.log_exit_order_canceled(self.exit_id)
                self.exit_id = None
            elif order_status == "EXPIRED":
                self._log_exit_order_event("EXIT_ORDER_EXPIRED", {
                    "order_id": self.exit_id
                })
                self.log_exit_order_expired(self.exit_id)
                self.exit_id = None
            elif order_status == "NEW":
                # FIX: Check for price drift using the same logic as TP updates
                cur = float(status["price"])
                max_drift = self.pc.maxer
                
                # Use the same logic as TP updates: |current_order_price - new_price| > maxer
                # For exit orders, we compare the order price with the current market price
                if side == "LONG":
                    price_drift = abs(cur - price)  # |order_price - market_price|
                else:  # SHORT
                    price_drift = abs(price - cur)  # |market_price - order_price|
                
                if price_drift >= max_drift:
                    self.drift_count += 1
                    self.last_drift_time = current_time
                    
                    # CONCISE: Clear drift detection for exit order management
                    self._summary_log(f"🔄 DRIFT EXCEEDED: Price {price:.5f} | Order {cur:.5f} | Drift {price_drift:.5f} >= {max_drift:.5f}")
                    
                    self._log_exit_order_event("EXIT_ORDER_DRIFT_DETECTED", {
                        "order_id": self.exit_id,
                        "market_price": price,
                        "order_price": cur,
                        "drift": price_drift,
                        "max_drift": max_drift,
                        "drift_count": self.drift_count,
                        "time_since_last_drift": current_time - self.last_drift_time if self.last_drift_time else None
                    })
                    
                    # Prefer updating an existing limit order price over cancel+place
                    try:
                        desired_price = round(price + (self.pc.delta if side == "LONG" else -self.pc.delta), self.pc.precision)
                        order_side = "SELL" if side == "LONG" else "BUY"
                        mode_used = ""
                        if hasattr(self, "order_manager") and self.order_manager is not None:
                            new_cid, mode_used = await self.order_manager.update_limit_price(
                                self.pair, self.exit_id, order_side, qty, desired_price, True, self.pc.precision
                            )
                            self._summary_log(f"🛠️ UPDATED LIMIT ORDER PRICE: {cur:.5f} -> {desired_price:.5f} (mode: {mode_used}) | id: {self.exit_id} -> {new_cid}")
                            self._summary_log(f"   Path: {'WS (order.modify)' if str(mode_used).startswith('ws') else 'REST (' + str(mode_used) + ')'}")
                            self._log_exit_order_event("EXIT_ORDER_PRICE_UPDATED", {
                                "old_order_id": self.exit_id,
                                "new_order_id": new_cid,
                                "old_price": cur,
                                "new_price": desired_price,
                                "mode": mode_used
                            })
                            # Update local tracking
                            self.exit_id = new_cid
                            self.exit_order_history.append({
                                "order_id": new_cid,
                                "reason": "Drift_update",
                                "placed_time": current_time,
                                "price": desired_price,
                                "quantity": qty,
                                "side": order_side,
                                "update_mode": mode_used
                            })
                        else:
                            # Fallback to existing behavior
                            await self._cancel(self.exit_id)
                            await self._place_exit(side, qty, price, "Drift")
                    except Exception as e:
                        self._detailed_log(f"Exit order update failed, falling back to cancel+place: {e}")
                        await self._cancel(self.exit_id)
                        await self._place_exit(side, qty, price, "Drift")
                else:
                    # CONCISE: Simple status showing price is within acceptable range
                    self._summary_log(f"📊 Price {price:.5f} | Order {cur:.5f} | Drift {price_drift:.5f} < {max_drift:.5f} ✓")
                    
                    self._log_exit_order_event("EXIT_ORDER_DRIFT_CHECK_PASSED", {
                        "order_id": self.exit_id,
                        "market_price": price,
                        "order_price": cur,
                        "drift": price_drift,
                        "max_drift": max_drift
                    })
            else:
                self._log_exit_order_event("EXIT_ORDER_UNKNOWN_STATUS", {
                    "order_id": self.exit_id,
                    "status": order_status
                })
                self.log_unknown_order_status(self.exit_id, order_status)
                
        except Exception as e:
            self._log_exit_order_event("EXIT_ORDER_ERROR", {
                "order_id": self.exit_id,
                "error": str(e),
                "status_checks": self.exit_order_status_checks
            })
            self.log_exit_order_error(self.exit_id, str(e))


    def _get_exit_controller(self, reason: str):
        """Get the appropriate exit controller based on reason, ensuring consistency.
        
        CRITICAL: This method ensures the same controller is used for both 
        amend_edge_if_needed() and place_or_amend() operations to prevent 
        state inconsistencies between multiple MakerOrderController instances.
        """
        if reason.upper() in ("TP", "TAKE_PROFIT"):
            return self.tp_ctrl if hasattr(self, 'tp_ctrl') else self.exit_ctrl
        elif reason.upper() in ("SL", "STOP_LOSS"):
            return self.sl_ctrl if hasattr(self, 'sl_ctrl') else self.exit_ctrl
        else:
            return self.exit_ctrl

    def _get_current_exit_controller(self):
        """Get the controller that was used for the current exit order.
        
        CRITICAL: This ensures amendments use the same controller that placed 
        the original order, preventing state inconsistencies.
        """
        if not self.exit_controller_type:
            return self.exit_ctrl  # Fallback to generic controller
        
        if self.exit_controller_type == "TP":
            return self.tp_ctrl if hasattr(self, 'tp_ctrl') else self.exit_ctrl
        elif self.exit_controller_type == "SL":
            return self.sl_ctrl if hasattr(self, 'sl_ctrl') else self.exit_ctrl
        else:
            return self.exit_ctrl

    async def _place_exit(self, side, qty, target_price, reason: str):
        """Place/peg exit (TP/SL) at the edge via maker controller (reduce-only)."""
        # SAFETY: Validate exit quantity is reasonable
        if qty <= 0:
            self._detailed_log(f"SAFETY: Invalid exit quantity {qty}, skipping exit order placement")
            return
        if qty > 1000:  # Sanity check - adjust based on your typical position sizes
            self._detailed_log(f"SAFETY: Exit quantity {qty} seems unusually large, proceeding with caution")
        
        self._reload_pair_params_if_changed()
        order_side = "SELL" if side == "LONG" else "BUY"
        # Choose controller type by reason: TP vs SL vs Signal
        ctrl = self._get_exit_controller(reason)
        cid, final_price = await ctrl.place_or_amend(side=order_side, qty=qty, reduce_only=True)
        self.exit_id = cid
        # CRITICAL: Track which controller was used for this exit order
        # This ensures amendments use the same controller to prevent state mismatches
        self.exit_controller_type = reason.upper() if reason.upper() in ("TP", "SL") else "GENERIC"
        self._log_exit_order_event("EXIT_ORDER_PLACED", {
            "order_id": cid,
            "side": side,
            "reason": reason,
            "target_price": target_price,
            "order_price": final_price,
            "reduce_only": True
        })
        self._summary_log(f"🏁 EXIT EDGE PLACED ({reason.upper()}): {order_side} {qty}@{final_price:.5f}")

    async def _process_new_signal(self, price, signal):
        """Process a new signal immediately without checking if it's already processed"""
        st = signal.get("type", "flat").upper()
        self._detailed_log(f"Processing NEW signal type: '{signal.get('type')}' -> '{st}'")
        
        # FLAT signals clean up all orders
        if st == "FLAT":
            self._detailed_log(f"📊 NEW FLAT SIGNAL: Cleaning up all orders")
            await self._cleanup_all_orders()
            return
            
        # Check cooldown (legacy). Only apply in file mode to avoid disrupting WS cooldown behavior
        try:
            cfg = load_json(CONFIG_PATH, {}) or {}
            src = str(cfg.get("signals", {}).get("source", "file")).lower()
        except Exception:
            src = "file"
        if src != "ws":
            if now() - self.state["last"] < 3600:
                time_left = 3600 - (now() - self.state["last"])
                self._detailed_log(f"⏰ COOLDOWN ACTIVE: {time_left}s remaining (file mode), skipping new signal")
                return
            
        # Cancel any existing orders before placing new one
        if self.entry_id:
            self._detailed_log(f"🔄 CANCELING EXISTING ORDER before new signal")
            await self._cancel(self.entry_id)
            self.entry_id = None
            
        # Place new entry order
        await self._place_new_entry_order(price, st)

    async def _no_position(self, price, signal):
        st = signal.get("type", "flat").upper()
        self._detailed_log(f"Processing signal type: '{signal.get('type')}' -> '{st}'")
        
        # FLAT signals must clean up ALL orders
        if st == "FLAT":
            self._detailed_log(f"📊 FLAT SIGNAL: Cleaning up all orders")
            await self._cleanup_all_orders()
            return
            
        # Always maintain an existing entry order regardless of processed flag
        if self.entry_id:
            await self._adapt_entry(price, st)
            return
        else:
            # Adopt an existing venue entry order if present
            try:
                open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
            except Exception as e:
                self._detailed_log(f"Open orders check failed: {e}")
                open_orders = []
            for o in open_orders:
                coid = o.get("clientOrderId") or o.get("origClientOrderId") or ""
                if coid.startswith(f"{self.pair}_open"):
                    self.entry_id = coid
                    self._detailed_log(f"Adopted venue entry order: {self.entry_id}")
                    await self._adapt_entry(price, st)
            return
            
        if signal.get("processed", True):
            # CRITICAL FIX: Verify state before attempting any reset to avoid duplicate entries
            # If we already have an entry order tracked in memory, adapt it
            if self.entry_id:
                self._detailed_log(f"📋 SIGNAL ALREADY PROCESSED: Checking existing entry order")
                await self._adapt_entry(price, st)
                return

            # No tracked entry_id; verify if there are any open orders on venue
            try:
                open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
            except Exception as e:
                self._detailed_log(f"Open orders check failed: {e}")
                open_orders = []

            if open_orders:
                self._detailed_log(f"📋 SIGNAL PROCESSED: Found {len(open_orders)} open order(s) on venue; not resetting signal")
                return

            # Double-check position via REST to avoid WS transient misses
            try:
                rest_positions = await self.rest.position_risk(self.pair)
                has_rest_pos = any(abs(float(p.get("positionAmt", 0))) > 0 for p in rest_positions)
            except Exception as e:
                self._detailed_log(f"REST position double-check failed: {e}")
                has_rest_pos = False

            if has_rest_pos:
                self._detailed_log("📋 SIGNAL PROCESSED: Position exists per REST, not resetting signal")
                return

            # Optional: only reset if the signal is recent and we truly have nothing pending
            # To be conservative, skip automatic reset; require a fresh signal from upstream
            self._detailed_log("✅ SIGNAL PROCESSED: No entry_id, no open orders, no position confirmed; waiting for new signal (no auto-reset)")
            return
        else:
            self._detailed_log(f"📋 SIGNAL ALREADY PROCESSED: Checking existing entry order")
            await self._adapt_entry(price, st)
            return

    async def _place_new_entry_order(self, price, st):
        """Place a new entry order for a fresh signal"""
        self.entry_order_time = now()
        self._reload_pair_params_if_changed()
        qty = await self._bet(price)
        initial_qty_zero = (qty == 0)
        side = "BUY" if st == "LONG" else "SELL"
        # Use maker controller to place at edge
        cid, final_price = await self.entry_ctrl.place_or_amend(side=side, qty=qty, reduce_only=False)
        self.entry_id = cid
        self._summary_log(f"✅ ENTRY EDGE PLACED: {side} {qty}@{final_price:.5f}")
        return

    async def _cleanup_all_orders(self):
        """Clean up ALL orders (used by FLAT signal) with better error handling"""
        try:
            open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
            if open_orders:
                # FLAT SIGNAL DEDUPLICATION: Only log once per flat signal period
                if not self.flat_signal_processed:
                    self._summary_log(f"🧹 FLAT SIGNAL: Cancelling {len(open_orders)} open orders")
                    self.flat_signal_processed = True
                for order in open_orders:
                    try:
                        await (self.order_manager.cancel(self.pair, order['clientOrderId']) if self.order_manager else self.rest.cancel(self.pair, order['clientOrderId']))
                        self._detailed_log(f"Cancelled order: {order['clientOrderId']} @ {order.get('price', order.get('stopPrice', order.get('avgPrice', 'N/A')))}")
                    except Exception as e:
                        if "Unknown order" not in str(e):  # Don't log for already-filled orders
                            self._detailed_log(f"Failed to cancel {order['clientOrderId']}: {e}")
                        
            # Reset all order tracking and state
            self.entry_id = None
            self.exit_id = None
            self.state.update({"tp": 0, "sl": 0})
            self._detailed_log("All orders cleaned up, state reset")
            
        except Exception as e:
            self._detailed_log(f"Error during order cleanup: {e}")
            # Still reset tracking to prevent stuck state
            self.entry_id = None
            self.exit_id = None

    async def _adapt_entry(self, price, st):
        if not self.entry_id:
            return
        side = "BUY" if st == "LONG" else "SELL"
        try:
            status = await (self.order_manager.get_order(self.pair, self.entry_id) if self.order_manager else self.rest.get_order(self.pair, self.entry_id))
        except Exception as e:
            if "-2013" in str(e):
                self.entry_id = None
                return
            else:
                raise
        st_status = str(status.get("status", ""))
        if st_status == "FILLED":
            self.entry_id = None
            return
        if st_status == "PARTIALLY_FILLED":
            remaining = max(0.0, float(status.get("origQty", 0)) - float(status.get("executedQty", 0)))
            await self.entry_ctrl.amend_edge_if_needed(side=side, qty=remaining, reduce_only=False)
            return
        # Re-peg to edge on book change
        orig_qty = float(status.get("origQty", 0) or 0)
        await self.entry_ctrl.amend_edge_if_needed(side=side, qty=orig_qty, reduce_only=False)
        return

    async def _place_entry_order(self, st: str, qty: float, price: float, reason: str):
        """Centralized entry order placement"""
        side = "BUY" if st == "LONG" else "SELL"
        delta = -self.pc.delta if side == "BUY" else self.pc.delta
        p = round(price + delta, self.pc.precision)
        cid = f"{self.pair}_open_{now()}_{reason}"
        
        place_start = time.time()
        try:
            await self.rest.place_limit(self.pair, side, qty, p, cid)
            path_note = "REST (place_limit)"
        except Exception as e:
            # Handle -5022 Post Only rejected: adjust price one small tick to maker side and retry once
            es = str(e)
            if "-5022" in es or "Post Only order" in es:
                adjust = max(self.pc.delta * 0.25, 10 ** (-self.pc.precision))
                p2 = round(p - adjust if side == "BUY" else p + adjust, self.pc.precision)
                cid = f"{self.pair}_open_{now()}_{reason}_retry"
                self._summary_log(f"🔁 ENTRY -5022 RETRY: Adjusting price {p:.5f} -> {p2:.5f}")
                await self.rest.place_limit(self.pair, side, qty, p2, cid)
                p = p2
                path_note = "REST (place_limit|retry)"
            else:
                raise
        place_time = (time.time() - place_start) * 1000
        self.entry_id = cid
        self.last_order_time = time.time()
        
        self._summary_log(f"🎯 ENTRY ORDER ({reason.upper()}): {side} {qty}@{p:.5f} | Market: {price:.5f} | Delta: {delta:.5f} | Time: {place_time:.0f}ms")
        self._summary_log(f"   Path: {path_note}")
        self._detailed_log(f"Placed {reason} entry order at {p}")

    async def _cancel(self, cid):
        if not cid:
            return
        try:
            if self.order_manager:
                await self.order_manager.cancel(self.pair, cid)
            else:
                await self.rest.cancel(self.pair, cid)
            self._detailed_log(f"Canceled order {cid}")
            # Log entry order cancel event so external systems (signaler) can free capacity
            self._log_entry_order_event("ENTRY_ORDER_CANCELED", {
                "order_id": cid
            })
        except Exception as e:
            self.log.error(f"Cancel {cid} -> {e}")
            self._detailed_log(f"Cancel failed: {e}")

    async def on_order_update(self, update: Dict[str, Any]):
        """Handle order updates from user data WS (non-blocking)."""
        try:
            # Try Futures ORDER_TRADE_UPDATE schema
            if update.get("e") == "ORDER_TRADE_UPDATE" and "o" in update:
                od = update["o"]
                symbol = od.get("s")
                if symbol != self.pair:
                    return
                client_id = od.get("c")
                status = od.get("X")  # NEW, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED
                avg_price = float(od.get("ap", 0) or 0)
                executed_qty = float(od.get("z", 0) or 0)
                side = od.get("S")  # BUY/SELL

                # Entry order updates
                if self.entry_id and client_id == self.entry_id:
                    if status == "FILLED":
                        # Mimic _adapt_entry filled path
                        self._summary_log("✅ ENTRY ORDER FILLED (WS):")
                        self._summary_log(f"   Average Price: {avg_price:.5f}")
                        self._summary_log(f"   Executed Quantity: {executed_qty}")
                        self._detailed_log(f"Entry filled via WS: qty {executed_qty}, avg {avg_price}")
                        self.entry_id = None
                    elif status == "PARTIALLY_FILLED":
                        self._summary_log(f"🔄 ENTRY PARTIAL (WS): filled={executed_qty} avg={avg_price:.5f}")
                    elif status in ("CANCELED", "EXPIRED"):
                        self._summary_log(f"❌ ENTRY CANCELED (WS): id={client_id}")
                        # Emit structured cancel/expire for capacity release
                        ev_type = "ENTRY_ORDER_CANCELED" if status == "CANCELED" else "ENTRY_ORDER_EXPIRED"
                        self._log_entry_order_event(ev_type, {
                            "order_id": client_id,
                            "status": status
                        })
                        self.entry_id = None
                    return

                # Exit order updates
                if self.exit_id and client_id == self.exit_id:
                    if status == "FILLED":
                        # Build status-like dict for existing handler
                        status_payload = {"avgPrice": str(avg_price), "executedQty": str(executed_qty)}
                        await self.handle_exit_order_filled(status_payload, side, executed_qty)
                    elif status == "PARTIALLY_FILLED":
                        # NOTE: WebSocket partial fills are logged but not acted upon
                        # REST polling will handle the re-placement logic to avoid race conditions
                        self._log_exit_order_event("EXIT_ORDER_PARTIAL_FILL_WS", {
                            "order_id": self.exit_id,
                            "filled_qty": executed_qty,
                            "avg_price": avg_price
                        })
                    elif status in ("CANCELED", "EXPIRED"):
                        self._log_exit_order_event("EXIT_ORDER_CANCELED_WS", {"order_id": self.exit_id})
                        self.exit_id = None
                    return

            # Spot executionReport fallback (not expected for UM, but tolerate)
            if update.get("e") == "executionReport":
                symbol = update.get("s")
                if symbol != self.pair:
                    return
                client_id = update.get("c")
                status = update.get("X")
                avg_price = float(update.get("L", 0) or 0)
                last_qty = float(update.get("l", 0) or 0)
                side = update.get("S")
                if self.entry_id and client_id == self.entry_id and status == "FILLED":
                    self.entry_id = None
                if self.exit_id and client_id == self.exit_id and status == "FILLED":
                    status_payload = {"avgPrice": str(avg_price), "executedQty": str(last_qty)}
                    await self.handle_exit_order_filled(status_payload, side, last_qty)
        except Exception as e:
            self._detailed_log(f"on_order_update error: {e}")

    async def _ensure_symbol_filters(self):
        if self._symbol_filters is not None:
            return
        try:
            info = await self.rest.exchange_info(self.pair)
            symbols = info.get("symbols", [])
            if not symbols:
                return
            # Find the exact symbol entry to avoid mismatched filters
            sym_obj = None
            for s in symbols:
                try:
                    if str(s.get("symbol")) == self.pair:
                        sym_obj = s
                        break
                except Exception:
                    continue
            if sym_obj is None:
                sym_obj = symbols[0]
            fs = sym_obj.get("filters", [])
            filters = {}
            for f in fs:
                t = f.get("filterType")
                if t == "PRICE_FILTER":
                    filters["tickSize"] = float(f.get("tickSize", 0))
                    filters["minPrice"] = float(f.get("minPrice", 0))
                elif t == "LOT_SIZE":
                    filters["stepSize"] = float(f.get("stepSize", 0))
                    filters["minQty"] = float(f.get("minQty", 0))
                elif t == "MIN_NOTIONAL":
                    filters["minNotional"] = float(f.get("notional", f.get("minNotional", 0)))
            self._symbol_filters = filters
            self._detailed_log(f"Loaded symbol filters: {filters}")
        except Exception as e:
            self._detailed_log(f"Failed to load exchange filters: {e}")
            self._symbol_filters = None

    def _round_to_step(self, value: float, step: float) -> float:
        if step <= 0:
            return value
        return math.floor(value / step) * step

    def _apply_exchange_filters(self, raw_price: float, raw_qty: float) -> tuple[float, float]:
        if not self._symbol_filters:
            return raw_price, raw_qty
        price = raw_price
        qty = raw_qty
        tick = (self._symbol_filters or {}).get("tickSize", 0)
        step = (self._symbol_filters or {}).get("stepSize", 0)
        min_notional = (self._symbol_filters or {}).get("minNotional", 0)
        min_qty = (self._symbol_filters or {}).get("minQty", 0)
        if tick:
            price = self._round_to_step(price, tick)
        if step:
            qty = self._round_to_step(qty, step)
        # Ensure min notional
        if min_notional and price * qty < min_notional and step:
            needed = min_notional / max(price, 1e-12)
            steps = math.ceil(needed / step)
            qty = steps * step
        # Ensure min qty
        if min_qty and qty < min_qty and step:
            steps = math.ceil(min_qty / step)
            qty = steps * step
        return price, qty

    def _format_for_exchange(self, price: float, qty: float) -> tuple[str, str]:
        """Format price and quantity as strings matching exchange tick/step to avoid precision errors."""
        tick = 0.0
        step = 0.0
        if getattr(self, "_symbol_filters", None):
            try:
                tick = float((self._symbol_filters or {}).get("tickSize", 0) or 0)
                step = float((self._symbol_filters or {}).get("stepSize", 0) or 0)
            except Exception:
                tick = 0.0
                step = 0.0
        # Derive decimal places from step sizes
        def decimals_from_step(val: float) -> int:
            if val <= 0:
                return 8  # safe default
            s = ("%.[PREC]f".replace("[PREC]", "12")) % val
            s = s.rstrip('0').rstrip('.')
            if '.' in s:
                return len(s.split('.')[1])
            return 0
        p_dec = decimals_from_step(tick) if tick > 0 else 8
        q_dec = decimals_from_step(step) if step > 0 else 8
        # Use Decimal for exact quantization
        from decimal import Decimal, ROUND_DOWN, getcontext
        getcontext().prec = 28
        def quantize_str(value: float, decimals: int) -> str:
            q = Decimal(str(value)).quantize(Decimal('1') if decimals == 0 else Decimal('1.' + ('0' * decimals)), rounding=ROUND_DOWN)
            s = format(q, 'f')
            if decimals == 0:
                # remove any fractional part
                if '.' in s:
                    s = s.split('.')[0]
            else:
                # ensure no more than specified decimals (format already respects it)
                pass
            return s
        price_str = quantize_str(price, p_dec)
        qty_str = quantize_str(qty, q_dec)
        return price_str, qty_str

    async def _ensure_single_esl(self, side: str, desired_stop: float) -> Optional[str]:
        """Ensure exactly one STOP_MARKET closePosition ESL exists; adopt/amend or place if none.
        Returns the active ESL cid."""

        try:
            open_orders = await (self.order_manager.open_orders(self.pair) if self.order_manager else self.rest.open_orders(self.pair))
            # CRITICAL FIX: Handle None response from open_orders
            if open_orders is None:
                open_orders = []
            esls = []
            for o in (open_orders or []):
                try:
                    # CRITICAL FIX: Handle None order objects
                    if o is None:
                        continue
                    if str(o.get("type", "")).upper() == "STOP_MARKET" and bool(o.get("closePosition", False)):
                        esls.append(o)
                except Exception:
                    continue
            if esls:
                # Keep best; cancel extras
                target = float(desired_stop or 0.0)
                def score(o):
                    try:
                        if o is None:
                            return 0
                        sp = float(o.get("stopPrice") or o.get("sp") or 0)
                        return abs(sp - target) if target else -(float(o.get("updateTime") or 0))
                    except Exception:
                        return 0
                esls.sort(key=score)
                keep = esls[0] if esls else None
                if keep is None:
                    return None
                kept_cid = keep.get("clientOrderId") or keep.get("origClientOrderId")
                # NEW: Add delays between ESL cancellations to prevent REST burst
                for i, ex in enumerate(esls[1:]):
                    xc = ex.get("clientOrderId") or ex.get("origClientOrderId")
                    try:
                        await self.order_manager.cancel(self.pair, xc)
                        # Add small delay between cancellations (except for last order)
                        if i < len(esls[1:]) - 1:
                            # Immediate ESL cancellation - no delay needed
                            pass
                    except Exception:
                        pass
                # Adopt and amend kept if price drifted
                if kept_cid:
                    self.emergency_cid = kept_cid
                    if desired_stop > 0:
                        try:
                            await self.order_manager.modify_emergency_stop(self.pair, kept_cid, "SELL" if side == "LONG" else "BUY", float(desired_stop))
                        except Exception:
                            pass
                    try:
                        self._summary_log(f"🛡️ ESL ADOPTED: cid={kept_cid} stopPrice={float(keep.get('stopPrice') or 0):.5f}")
                    except Exception:
                        self._summary_log(f"🛡️ ESL ADOPTED: cid={kept_cid}")
                    return kept_cid
            # None found → place new
            cid = f"esl-{self.pair}-{int(time.time()*1000)}"
            try:
                result = await self.order_manager.place_emergency_stop(self.pair, "SELL" if side == "LONG" else "BUY", float(desired_stop), cid)
                if result:
                    self.emergency_cid = cid
                    self._summary_log(f"🛡️ EMERGENCY STOP ORDER BOOTSTRAPPED: cid={cid} stopPrice={desired_stop:.5f}")
                    return cid
                else:
                    self._summary_log(f"❌ ESL PLACEMENT FAILED: order_manager.place_emergency_stop returned None/False")
                    return None
            except Exception as place_error:
                self._summary_log(f"❌ ESL PLACEMENT EXCEPTION: {place_error}")
                return None
        except Exception as e:
            self._summary_log(f"⚠️ ensure_single_esl failed: {e}")
            return None

    async def handle_user_data_event(self, data: dict) -> None:
        """Handle user data stream events for real-time order and position updates"""
        event_type = data.get("e")
        
        if event_type == "ORDER_TRADE_UPDATE" and "o" in data:
            order_data = data["o"]
            client_id = order_data.get("c", "")
            status = order_data.get("X", "").upper()
            
            # Check if this is our order
            if (client_id == self.entry_id or client_id == self.exit_id or 
                client_id == self.emergency_cid or client_id.startswith(f"{self.pair}_")):
                
                # NEW: Mark order as canceled in state tracker
                if status == "CANCELED":
                    self.order_state_tracker.mark_canceled(client_id)
                
                if status in ["FILLED", "PARTIALLY_FILLED"]:
                    # NEW: Mark order as filled in state tracker immediately
                    if status == "FILLED":
                        self.order_state_tracker.mark_filled(client_id)
                    
                    # Update internal state immediately - minimal logging
                    if client_id == self.entry_id:
                        self.entry_id = None
                        self._summary_log(f"ENTRY_FILLED_STREAM: cid={client_id}")
                    elif client_id == self.exit_id:
                        self.exit_id = None
                        # CRITICAL FIX: Clear controller tracking when exit order is filled
                        self.exit_controller_type = None
                        # CRITICAL FIX: Immediately clear position state when exit order is filled
                        # This prevents the "STATE MISMATCH: No exchange position but internal state suggests position exists" issue
                        self.state.update({"tp": 0, "sl": 0, "last": now()})
                        # SIGNAL DEDUPLICATION: Reset ignored signal tracking when position closes
                        self.last_ignored_signal_type = None
                        
                        # NEW: Clear ESL when position closes
                        if self.emergency_cid:
                            try:
                                await self._cancel_emergency_stop()
                                self.emergency_cid = None
                            except Exception as e:
                                self._detailed_log(f"Error clearing ESL on exit fill: {e}")
                        
                        # Reset ESL state
                        self.emergency_sl_price = 0.0
                        self.emergency_sl_activated = False
                        self.emergency_sl_tp_reference = 0.0
                        self.emergency_sl_initialized = False
                        
                        self._summary_log(f"EXIT_FILLED_STREAM: cid={client_id}")
                        self._summary_log("🔄 POSITION_STATE_CLEARED: SL/TP reset after exit fill")
                        self._summary_log("🧹 ESL_CLEARED: Emergency stop cleared after position closure")
                    elif client_id == self.emergency_cid:
                        self.emergency_cid = None
                        # CRITICAL FIX: Circuit breaker - immediately stop all order management
                        self.exit_id = None  # Stop exit order management
                        self.entry_id = None  # Stop entry order management
                        
                        # CRITICAL FIX: Also clear position state when emergency SL is triggered
                        self.state.update({"tp": 0, "sl": 0, "last": now()})
                        # CRITICAL FIX: Clear ESL state to prevent stale values affecting future positions
                        self.emergency_sl_price = 0.0
                        self.emergency_sl_activated = False
                        self.emergency_sl_tp_reference = 0.0
                        self.emergency_sl_initialized = False
                        # SIGNAL DEDUPLICATION: Reset ignored signal tracking when position closes via ESL
                        self.last_ignored_signal_type = None
                        self._summary_log(f"ESL_FILLED_STREAM: cid={client_id}")
                        self._summary_log("🔄 POSITION_STATE_CLEARED: SL/TP/ESL reset after ESL fill")
                        self._summary_log("🛑 CIRCUIT_BREAKER: All order management stopped after ESL fill")
        
        elif event_type == "ACCOUNT_UPDATE" and "a" in data:
            # Handle position updates from user data stream
            account_data = data["a"]
            positions = account_data.get("P", [])
            
            for pos_data in positions:
                if pos_data.get("s") == self.pair:
                    pos_amt = float(pos_data.get("pa", 0))
                    if pos_amt == 0 and hasattr(self, '_last_position_amt') and self._last_position_amt != 0:
                        # Position closed - minimal logging
                        self._summary_log(f"POSITION_CLOSED_STREAM: {self.pair}")
                        # CRITICAL FIX: Clear position state when position is closed via account update
                        # This provides additional protection against state mismatches
                        if self.state["sl"] != 0 or self.state["tp"] != 0:
                            self.state.update({"tp": 0, "sl": 0, "last": now()})
                            # SIGNAL DEDUPLICATION: Reset ignored signal tracking when position closes
                            self.last_ignored_signal_type = None
                            self._summary_log("🔄 POSITION_STATE_CLEARED: SL/TP reset after position closure")
                    self._last_position_amt = pos_amt


async def ws_prices(symbols: list[str], store: Dict[str, float]):
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(f"{s.lower()}@bookTicker" for s in symbols)
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(url) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data).get("data", {})
                            s = d.get("s"); a = d.get("a"); b = d.get("b"); A = d.get("A"); B = d.get("B")
                            if s and a and b:
                                store[s.upper()] = float(a)
                                store[f"{s.upper()}_BOOK"] = {"best_bid": float(b), "best_ask": float(a), "bid_qty": float(B or 0), "ask_qty": float(A or 0), "ts": time.time()}
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
        except Exception as e:
            logging.error(f"WS error {e}; reconnect in 5s")
            await asyncio.sleep(5)

async def main():
    cfg = load_json(CONFIG_PATH, {})
    pair_cfg = {p: PairCfg(**v) for p, v in cfg.get("pairs", {}).items()}
    
    # Filter users with valid API keys
    users = []
    for u in cfg.get("users", []):
        user_cfg = UserCfg(**u)
        if user_cfg.api_key and user_cfg.secret_key:
            users.append(user_cfg)
        else:
            logging.info(f"Skipping user {user_cfg.uid} - no API credentials")
    
    if not users:
        logging.error("No users with valid API credentials found")
        return
    
    # Get initial active symbols from signals.json
    signals = load_json(SIGNALS_PATH, {})
    initial_symbols = [s for s in signals.keys() if s in pair_cfg]
    
    if not initial_symbols:
        logging.info("No initial signals found, starting with empty bot set...")
        initial_symbols = []
    else:
        logging.info(f"Initial active symbols: {initial_symbols}")
    
    prices: Dict[str, float] = {}
    tasks = []
    running_bots = []
    
    # Start WebSocket for ALL configured symbols (not just active ones)
    # This ensures we get price feeds for any symbol that might become active
    all_symbols = list(pair_cfg.keys())
    tasks.append(asyncio.create_task(ws_prices(all_symbols, prices)))
    
    # Create bots for initial active symbols
    for sym in initial_symbols:
        for u in users:
            sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7), trust_env=False)
            rest = BinanceREST(u.api_key, u.secret_key, sess, u.ip)
            bot_task = asyncio.create_task(SymbolBot(sym, pair_cfg[sym], u, rest, prices).run())
            running_bots.append(bot_task)
            logging.info(f"Created bot for {sym}-{u.uid}")
    
    # Start signal monitor with initial symbols to avoid duplicates
    tasks.append(asyncio.create_task(signal_monitor(pair_cfg, users, prices, running_bots, initial_symbols)))
    
    # Add all running bots to main tasks
    tasks.extend(running_bots)
    
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass