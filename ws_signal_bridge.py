"""
ws_signal_bridge.py - WebSocket Signal Bridge with Log Spam Fixes

RECENT CHANGES (Log Spam Fix):
- Fixed flat->flat bootstrap spam by only logging meaningful transitions (line ~244)
- Added WS connection logging for monitoring reconnections (line ~192)  
- Added bootstrap completion logging (line ~254)

BACKUP: ws_signal_bridge.py.backup_before_flat_fix
"""

import asyncio
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Dict, Any, Optional

import aiohttp

BASE_DIR = Path(__file__).resolve().parent
SIGNALS_PATH = BASE_DIR / "signals.json"
CONFIG_PATH = BASE_DIR / "config.json"
USER_STATE_DIR = BASE_DIR / "user_state"
USER_STATE_DIR.mkdir(exist_ok=True)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

TRANSITIONS_LOG_PATH = LOG_DIR / "signal_transitions.log"

# Transition logger
TRANSITIONS_LOG = logging.getLogger("signal_transitions")
if not TRANSITIONS_LOG.handlers:
	TRANSITIONS_LOG.setLevel(logging.INFO)
	h = logging.FileHandler(TRANSITIONS_LOG_PATH)
	h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
	TRANSITIONS_LOG.addHandler(h)


def load_json(path: Path, default: Any) -> Any:
	try:
		with path.open() as f:
			return json.load(f)
	except (FileNotFoundError, json.JSONDecodeError):
		return default


def save_json(path: Path, data: Any) -> None:
	with path.open("w") as f:
		json.dump(data, f, separators=(",", ":"))


class WsSignalsBridge:
	"""Bridges external WS signal feed to local signals.json with cooldown-aware processing."""
	def __init__(self, cfg: Dict[str, Any], allowed_symbols: Dict[str, Any]):
		self.cfg = cfg
		self.allowed_symbols = set(allowed_symbols.keys())
		self.signals_cfg = cfg.get("signals", {})
		self.trading_cfg = cfg.get("trading", {})
		self.url: str = self.signals_cfg.get("url", "")
		self.insecure_tls: bool = bool(self.signals_cfg.get("insecure_tls", False))
		self.enter_on_start_if_active: bool = bool(self.signals_cfg.get("enter_on_start_if_active", True))
		self.require_flat_reset_after_cooldown: bool = bool(self.signals_cfg.get("require_flat_reset_after_cooldown", False))
		self.reconnect_min: int = int(self.signals_cfg.get("ws_reconnect_min_seconds", 1))
		self.reconnect_max: int = int(self.signals_cfg.get("ws_reconnect_max_seconds", 30))
		self.ws_stale_seconds: int = int(self.signals_cfg.get("ws_stale_seconds", 60))

		# Cooldown calculation: prefer minutes if provided, fallback to seconds
		cd_minutes = int(self.trading_cfg.get("cooldown_minutes", 0))
		cd_seconds = int(self.trading_cfg.get("cooldown_seconds", 0))
		self.cooldown_seconds: int = cd_seconds if (cd_minutes <= 0 and cd_seconds >= 0) else cd_minutes * 60

		self._last_types: Dict[str, str] = {}
		self._current_types: Dict[str, str] = {}
		self._cooldowns: Dict[str, int] = {}  # symbol -> unix ts
		self._last_message_ts: float = 0.0
		self._session: Optional[aiohttp.ClientSession] = None
		self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
		self._stop = False
		self._price_cache: Dict[str, float] = {}  # For enhanced logging
		self._stale = False

		# Bootstrap from existing files
		existing = load_json(SIGNALS_PATH, {}) or {}
		for sym, obj in existing.items():
			if sym in self.allowed_symbols and isinstance(obj, dict):
				self._last_types[sym] = str(obj.get("type", "flat")).lower()
				self._current_types[sym] = self._last_types[sym]
		self._cooldowns = load_json(USER_STATE_DIR / "cooldowns.json", {}) or {}

		# REST-only price enrichment
		self._binance_rest_base: str = "https://fapi.binance.com"

	def _log_transition(self, sym: str, from_t: str, to_t: str):
		if from_t == to_t:
			return
		source = "ws"
		TRANSITIONS_LOG.info(f"{source} {sym} {from_t}->{to_t}")

	def _get_current_price(self, sym: str) -> float:
		"""Get current price for minimal logging - no API calls"""
		# Try to get price from local cache if available
		try:
			# This would be populated by ws_prices in engine.py
			return self._price_cache.get(sym, 0.0)
		except:
			return 0.0
	
	async def _log_transition_with_price(self, sym: str, from_t: str, to_t: str):
		# Minimal load: single REST price fetch with tight timeout; skip WS price calls entirely
		price = await self._rest_fetch_price_with_backoff(sym, max_tries=1, delay_seconds=0.0)
		if price is None:
			self._log_transition(sym, from_t, to_t)
			return
		source = "ws"
		TRANSITIONS_LOG.info(f"{source} {sym} {from_t}->{to_t} price={price}")

	def _save_cooldowns(self):
		save_json(USER_STATE_DIR / "cooldowns.json", self._cooldowns)

	def _cooldown_active(self, sym: str) -> bool:
		end_ts = int(self._cooldowns.get(sym, 0) or 0)
		return end_ts > int(time.time())

	def _set_cooldown(self, sym: str):
		if self.cooldown_seconds <= 0:
			return
		self._cooldowns[sym] = int(time.time()) + self.cooldown_seconds
		self._save_cooldowns()

	def _clear_cooldown(self, sym: str):
		if sym in self._cooldowns:
			self._cooldowns.pop(sym, None)
			self._save_cooldowns()

	def _update_signals_file(self, updates: Dict[str, Dict[str, Any]]):
		# Load, apply, save atomically
		existing: Dict[str, Any] = load_json(SIGNS_PATH := SIGNALS_PATH, {}) or {}
		changed = False
		for sym, upd in updates.items():
			if sym not in self.allowed_symbols:
				continue
			prev = existing.get(sym, {"type": "flat", "processed": True})
			new_type = str(upd.get("type", prev.get("type", "flat"))).lower()
			new_processed = bool(upd.get("processed", prev.get("processed", True)))
			if prev.get("type") != new_type or bool(prev.get("processed", True)) != new_processed:
				existing[sym] = {"type": new_type, "processed": new_processed}
				changed = True
		if changed:
			save_json(SIGNS_PATH, existing)

	def _emit_trigger_if_needed(self, sym: str, new_type: str, initial_bootstrap: bool = False):
		# Decide processed flag based on cooldowns and transitions
		if new_type == "flat":
			# Always mark flat processed
			self._update_signals_file({sym: {"type": "flat", "processed": True}})
			# Early clear cooldown when signal returns to flat
			self._clear_cooldown(sym)
			return

		# Active signal long/short
		in_cd = self._cooldown_active(sym)
		last_t = self._last_types.get(sym, "flat")
		if in_cd:
			# Keep processed True to ignore during cooldown
			self._update_signals_file({sym: {"type": new_type, "processed": True}})
			return

		# Not in cooldown
		if initial_bootstrap:
			if self.enter_on_start_if_active:
				self._update_signals_file({sym: {"type": new_type, "processed": False}})
			else:
				self._update_signals_file({sym: {"type": new_type, "processed": True}})
			return

		if last_t == "flat":
			# Edge-trigger with enhanced logging
			current_price = self._get_current_price(sym)
			self._update_signals_file({sym: {"type": new_type, "processed": False}})
			# Minimal but informative signal logging
			logging.info(f"🔥 SIGNAL: {sym} {last_t}→{new_type} @{current_price:.5f}")
			return

		# Same active state as before: do nothing here; cooldown expiry task will re-emit if required
		self._update_signals_file({sym: {"type": new_type, "processed": True}})

	async def _ensure_session(self):
		if self._session is None:
			self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

	async def _connect(self):
		await self._ensure_session()
		ssl_ctx = None
		if self.insecure_tls:
			ssl_ctx = ssl.create_default_context()
			ssl_ctx.check_hostname = False
			ssl_ctx.verify_mode = ssl.CERT_NONE
		self._ws = await self._session.ws_connect(self.url, ssl=ssl_ctx, heartbeat=15)
		self._last_message_ts = time.time()
		# Log WS connection/reconnection event
		TRANSITIONS_LOG.info(f"ws WS_CONNECTED url={self.url}")

	async def _rest_fetch_price_with_backoff(self, symbol: str, max_tries: int = 3, delay_seconds: float = 0.5) -> Optional[float]:
		await self._ensure_session()
		url = f"{self._binance_rest_base}/fapi/v1/ticker/price"
		params = {"symbol": symbol}
		tries = 0
		while tries < max_tries:
			tries += 1
			try:
				async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=1.2)) as r:
					if r.status == 200:
						data = await r.json()
						p = data.get("price")
						if p is not None:
							return float(p)
					# For non-200, fallthrough to retry
			except Exception:
				pass
			if tries < max_tries:
				try:
					await asyncio.sleep(delay_seconds)
				except Exception:
					pass
		return None

	async def _reader_loop(self):
		assert self._ws is not None
		ws = self._ws
		bootstrap_done = False
		while not self._stop:
			msg = await ws.receive()
			if msg.type == aiohttp.WSMsgType.TEXT:
				self._last_message_ts = time.time()
				try:
					data = json.loads(msg.data)
					# Expecting {"BTCUSDT": {"type": "long"}, ...}
					if isinstance(data, dict):
						updates: Dict[str, str] = {}
						for k, v in data.items():
							if not isinstance(v, dict):
								continue
							stype = str(v.get("type", "flat")).lower()
							updates[k.upper()] = stype
						# Apply updates per allowed symbol
						for sym, stype in updates.items():
							if sym not in self.allowed_symbols:
								continue
							prev = self._current_types.get(sym, "flat")
							self._current_types[sym] = stype
							if not bootstrap_done:
								# On first snapshot or first message containing this symbol
								# FIXED: Only log meaningful transitions during bootstrap (skip flat->flat spam)
								if prev != stype:
									asyncio.create_task(self._log_transition_with_price(sym, prev, stype))
								self._emit_trigger_if_needed(sym, stype, initial_bootstrap=True)
							else:
								if prev != stype:
									asyncio.create_task(self._log_transition_with_price(sym, prev, stype))
									self._emit_trigger_if_needed(sym, stype, initial_bootstrap=False)
							# Track last type seen for edge detection
							self._last_types[sym] = stype
						# After processing first valid message, consider bootstrap done
						if not bootstrap_done:
							TRANSITIONS_LOG.info(f"ws WS_BOOTSTRAP_COMPLETE symbols={len(updates)}")
						bootstrap_done = True
				except Exception as e:
					logging.getLogger("signal_bridge").warning(f"WS parse error: {e}")
			elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
				break

	async def _cooldown_expiry_loop(self):
		# Periodically check for cooldown expiry and re-emit triggers if still active
		while not self._stop:
			try:
				# Staleness detection: if no messages for ws_stale_seconds, mark stale
				if self.ws_stale_seconds > 0 and (time.time() - self._last_message_ts) > self.ws_stale_seconds:
					self._stale = True
				else:
					self._stale = False
				if self.cooldown_seconds > 0 and not self.require_flat_reset_after_cooldown:
					# Reload cooldowns written by bots
					self._cooldowns = load_json(USER_STATE_DIR / "cooldowns.json", {}) or {}
					# Reload current signals to know processed state
					signals = load_json(SIGNALS_PATH, {}) or {}
					for sym in list(self.allowed_symbols):
						end_ts = int(self._cooldowns.get(sym, 0) or 0)
						if end_ts <= 0:
							continue
						if end_ts <= int(time.time()):
							# Cooldown expired
							current_type = self._current_types.get(sym, "flat")
							if current_type in ("long", "short") and not self._stale:
								# If processed is True, flip to False to trigger re-entry
								prev = signals.get(sym, {"type": current_type, "processed": True})
								if bool(prev.get("processed", True)):
									self._update_signals_file({sym: {"type": current_type, "processed": False}})
							# Clear cooldown
							self._clear_cooldown(sym)
			except Exception:
				pass
			await asyncio.sleep(1.0)

	async def run(self):
		retry = self.reconnect_min
		while not self._stop:
			try:
				await self._connect()
				# Launch helper loops
				tasks = [
					asyncio.create_task(self._reader_loop()),
					asyncio.create_task(self._cooldown_expiry_loop()),
				]
				await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
			except Exception as e:
				logging.getLogger("signal_bridge").error(f"WS bridge error: {e}")
			finally:
				try:
					if self._ws and not self._ws.closed:
						await self._ws.close()
				except Exception:
					pass
				self._ws = None
				# Backoff
				await asyncio.sleep(retry)
				retry = min(max(self.reconnect_min, retry * 2), self.reconnect_max)

	async def stop(self):
		self._stop = True
		if self._session:
			await self._session.close()


async def run_ws_signals_bridge() -> Optional[asyncio.Task]:
	"""Factory to be called by engine when signals.source == 'ws'."""
	cfg = load_json(CONFIG_PATH, {}) or {}
	pairs = cfg.get("pairs", {}) or {}
	if not pairs:
		return None
	bridge = WsSignalsBridge(cfg, pairs)
	return asyncio.create_task(bridge.run()) 