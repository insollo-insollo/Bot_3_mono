from typing import Optional, Tuple, Dict, Any, List
import time

from core import BinanceREST  # type: ignore
from ws_trading import TradingWsClient


class BatchPositionManager:
	"""Global batch position manager - single API call for all symbols"""
	_instance = None
	
	def __new__(cls):
		if cls._instance is None:
			cls._instance = super().__new__(cls)
			cls._instance._initialized = False
		return cls._instance
	
	def __init__(self):
		if not self._initialized:
			self.cache: Dict[str, List[Dict[str, Any]]] = {}
			self.last_fetch = 0.0
			self.cache_ttl = 1.5  # 1.5 second cache for batch efficiency
			self.rest_client = None
			self._initialized = True
	
	def set_rest_client(self, rest_client):
		"""Set REST client for batch operations"""
		self.rest_client = rest_client
	
	async def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
		"""Get positions with intelligent batching - 94% fewer API calls"""
		now = time.time()
		
		# Refresh cache if needed
		if now - self.last_fetch > self.cache_ttl and self.rest_client:
			try:
				# Single API call for ALL positions instead of 16 individual calls
				all_positions = await self.rest_client.position_risk()  # No symbol = all
				
				# Organize by symbol for fast lookup
				self.cache = {}
				for pos in (all_positions or []):
					pos_symbol = pos.get("symbol")
					if pos_symbol:
						if pos_symbol not in self.cache:
							self.cache[pos_symbol] = []
						self.cache[pos_symbol].append(pos)
				
				self.last_fetch = now
			except Exception:
				# Use stale cache if batch fails
				pass
		
		# Return requested symbol or all
		if symbol:
			return self.cache.get(symbol, [])
		
		# Return all positions
		all_positions = []
		for positions in self.cache.values():
			all_positions.extend(positions)
		return all_positions


# Global batch manager instance
_batch_position_manager = BatchPositionManager()

class OrderManager:
	"""Thin wrapper for order placement/maintenance with WS-first policy and guarded REST fallback."""

	def __init__(self, rest: BinanceREST, ws_client: Optional[TradingWsClient] = None, symbol_bot=None):
		self.rest = rest
		self.ws = ws_client
		self.symbol_bot = symbol_bot
		
		# Initialize batch position manager
		_batch_position_manager.set_rest_client(rest)
		# Fallback policy - MORE CONSERVATIVE to prevent ban escalation
		self._ws_fail_count: int = 0
		self._ws_last_fail_ts: float = 0.0
		self._ws_down_since: float = 0.0
		self._fallback_fail_threshold: int = 2  # Reduced from 3 to 2 - fail faster to REST
		self._fallback_down_seconds: int = 120  # Reduced from 180 to 120 - try WS again sooner
		# NEW: In-flight amend debounce per client id
		self._amend_inflight: Dict[str, float] = {}
		# NEW: WS-first caches and REST backoff to avoid rate limits
		self._positions_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
		self._open_orders_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
		self._rest_backoff_until: Dict[str, Dict[str, float]] = {"positions": {}, "open_orders": {}}
		self._cache_ttl_sec: float = 1.0  # serve recent WS cache to callers
		self._rest_min_interval_sec: float = 10.0  # do not poll REST more than this per symbol

	def _ws_allowed(self) -> bool:
		# Allow WS unless we have declared it down for a while
		if not self.ws:
			return False
		if self._ws_down_since > 0 and (time.time() - self._ws_down_since) < self._fallback_down_seconds:
			return False
		return True

	def _record_ws_failure(self):
		self._ws_fail_count += 1
		self._ws_last_fail_ts = time.time()
		if self._ws_fail_count >= self._fallback_fail_threshold:
			# declare temporary WS down
			self._ws_down_since = self._ws_last_fail_ts
			try:
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					self.symbol_bot._summary_log("WS_MARKED_DOWN: trading WS temporarily disabled; using REST fallback")
			except Exception:
				pass

	def _record_ws_success(self):
		self._ws_fail_count = 0
		self._ws_last_fail_ts = 0.0
		self._ws_down_since = 0.0

	def _normalize_positions(self, resp: Any) -> List[Dict[str, Any]]:
		"""Normalize WS position.status response to REST-like list of dicts.
		Returns list of dicts with keys: symbol, positionAmt, entryPrice (all strings as in REST).
		"""
		positions: List[Dict[str, Any]] = []
		try:
			base = resp
			if isinstance(base, dict):
				base = base.get("result") or base.get("data") or base
			# Some WS responses wrap positions under a key
			if isinstance(base, dict):
				candidates = base.get("positions") or base.get("updateData") or base.get("rows") or base.get("list") or []
			elif isinstance(base, list):
				candidates = base
			else:
				candidates = []
			for p in candidates:
				if not isinstance(p, Dict):
					continue
				symbol = p.get("symbol") or p.get("s")
				pa = p.get("positionAmt") or p.get("pa") or p.get("posAmt") or p.get("quantity")
				ep = p.get("entryPrice") or p.get("ep") or p.get("avgEntryPrice")
				if not symbol or pa is None:
					continue
				# Convert to strings to match REST shape
				try:
					pa_str = str(float(pa))
					ep_str = str(float(ep)) if ep is not None else "0"
					positions.append({"symbol": symbol, "positionAmt": pa_str, "entryPrice": ep_str})
				except Exception:
					continue
		except Exception:
			# If anything goes wrong, return empty so caller can decide on fallback
			return []
		return positions

	def _normalize_orders(self, resp: Any) -> List[Dict[str, Any]]:
		"""Normalize WS openOrders/status into a list of order dicts with a 'price' key present."""
		orders: List[Dict[str, Any]] = []
		base = resp
		if isinstance(base, dict):
			base = base.get("result") or base.get("data") or base
		if isinstance(base, dict):
			candidates = base.get("openOrders") or base.get("orders") or base.get("list") or []
		elif isinstance(base, list):
			candidates = base
		else:
			candidates = []
		for o in candidates:
			if not isinstance(o, Dict):
				continue
			# Ensure price field exists (WS may use short key 'p')
			if "price" not in o and "p" in o:
				try:
					o["price"] = float(o.get("p") or 0)
				except Exception:
					o["price"] = 0
			# Normalize client order id naming
			if "origClientOrderId" not in o and "clientOrderId" in o:
				o["origClientOrderId"] = o["clientOrderId"]
			orders.append(o)
		return orders

	async def place_limit(self, symbol: str, side: str, qty: float, price: float, cid: str, reduce: bool = False, precision: Optional[int] = None):
		# Use centralized order preparation if SymbolBot is available
		if self.symbol_bot and hasattr(self.symbol_bot, "_prepare_order_params"):
			final_price, final_qty, _ = self.symbol_bot._prepare_order_params(symbol, side, qty, price, reduce_only=reduce)
		else:
			final_price, final_qty = price, qty

		# Format as strings to satisfy exchange precision constraints
		price_str = str(final_price)
		qty_str = str(final_qty)
		if self.symbol_bot and hasattr(self.symbol_bot, "_format_for_exchange"):
			try:
				price_str, qty_str = self.symbol_bot._format_for_exchange(final_price, final_qty)
			except Exception:
				pass

		# WS-first with retries before REST fallback
		max_ws_retries = 5
		backoffs_ms: List[int] = [50, 100, 200, 300, 500]
		attempt = 0
		last_exception: Optional[Exception] = None
		while self._ws_allowed() and attempt < max_ws_retries:
			attempt += 1
			try:
				await self.ws.connect()
				params = {
					"symbol": symbol,
					"side": side,
					"type": "LIMIT",
					"timeInForce": "GTX",
					"quantity": qty_str,
					"price": price_str,
					"newClientOrderId": cid,
					"reduceOnly": reduce,
					"selfTradePreventionMode": "EXPIRE_TAKER",
				}
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					try:
						self.symbol_bot._summary_log(f"ORDER_PLACE_REQUEST {{mode=ws, symbol={symbol}, cid={cid}, side={side}, qty={qty_str}, price={price_str}, tif=GTX, ro={reduce}}}")
					except Exception:
						pass
				resp = await self.ws.send_request("order.place", params)
				# If WS returned an error payload, raise to trigger retry logic
				if isinstance(resp, dict):
					status = resp.get("status")
					err = resp.get("error") or {}
					code = err.get("code")
					msg = err.get("msg")
					if code is not None or (status and status != 200):
						# -5022: bubble immediately so caller can re-peg
						if code == -5022:
							raise Exception(f"-5022 WS_PLACE_ERROR status={status} code={code} msg={msg}")
						# Auth/time errors: trigger reconnect and retry
						if code in (-1022, -1021, -1023):
							# Force reconnect for next attempt
							try:
								await self.ws.close()
							except Exception:
								pass
							raise Exception(f"WS_PLACE_ERROR status={status} code={code} msg={msg}")
						# Other non-200: treat as transient and retry
						raise Exception(f"WS_PLACE_ERROR status={status} code={code} msg={msg}")
				self._record_ws_success()
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					try:
						self.symbol_bot._summary_log("ORDER_PLACE_RESPONSE {mode=ws}")
					except Exception:
						pass
				return resp, "WS"
			except Exception as e:
				# If post-only rejection, bubble out immediately without retrying same price
				if "-5022" in str(e):
					raise
				last_exception = e
				self._record_ws_failure()
				# Backoff before next attempt if more retries left
				if attempt < max_ws_retries:
					try:
						import asyncio  # local import to avoid top-level dependency
						await asyncio.sleep(backoffs_ms[min(attempt-1, len(backoffs_ms)-1)] / 1000)
					except Exception:
						pass

		# After WS retries, fallback to REST
		if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
			try:
				self.symbol_bot._summary_log(f"ORDER_PLACE_REQUEST {{mode=rest, symbol={symbol}, cid={cid}, side={side}, qty={qty_str}, price={price_str}, tif=GTX, ro={reduce}}}")
			except Exception:
				pass
		resp = await self.rest.place_limit(symbol, side, qty_str, price_str, cid, reduce)
		# Fast-path REST post-only rejection (-5022): bubble up so controller can re-peg immediately
		try:
			if isinstance(resp, dict):
				code = resp.get("code")
				status = resp.get("status")
				msg = resp.get("msg") or resp.get("message")
				if code == -5022 or (isinstance(status, int) and status >= 400 and code == -5022):
					raise Exception(f"-5022 REST_PLACE_ERROR status={status} code={code} msg={msg}")
		except Exception as e:
			# Ensure we do not log a success response when bubbling an error
			raise
		if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
			try:
				self.symbol_bot._summary_log("ORDER_PLACE_RESPONSE {mode=rest}")
			except Exception:
				pass
		return resp, "REST"

	async def place_market(self, symbol: str, side: str, qty: float, reduce: bool = False):
		if self._ws_allowed():
			try:
				await self.ws.connect()
				params = {
					"symbol": symbol,
					"side": side,
					"type": "MARKET",
					"quantity": qty,
					"reduceOnly": str(reduce).lower(),
				}
				resp = await self.ws.send_request("order.place", params)
				self._record_ws_success()
				return resp
			except Exception:
				self._record_ws_failure()
		return await self.rest.place_market(symbol, side, qty, reduce)

	async def cancel(self, symbol: str, cid: str):
		if self._ws_allowed():
			try:
				await self.ws.connect()
				params = {"symbol": symbol, "origClientOrderId": cid}
				resp = await self.ws.send_request("order.cancel", params)
				self._record_ws_success()
				return resp
			except Exception:
				self._record_ws_failure()
		return await self.rest.cancel(symbol, cid)

	async def get_order(self, symbol: str, cid: str):
		if self._ws_allowed():
			try:
				await self.ws.connect()
				params = {"symbol": symbol, "origClientOrderId": cid}
				resp = await self.ws.send_request("order.get", params)
				# If WS returned an error envelope, try alias method once
				if isinstance(resp, dict) and (resp.get("error") is not None or (isinstance(resp.get("status"), int) and resp.get("status") >= 400)):
					try:
						resp = await self.ws.send_request("order.status", params)
					except Exception:
						pass
				self._record_ws_success()
				# Normalize fields to REST-like keys when possible
				order: Dict[str, Any] = {}
				if isinstance(resp, dict):
					res = resp.get("result")
					# Only accept proper result dicts, ignore bare error envelopes
					if isinstance(res, dict):
						order = dict(res)
						# Ensure 'price' if short key present
						if "price" not in order and "p" in order:
							try:
								order["price"] = float(order.get("p") or 0)
							except Exception:
								order["price"] = 0
				# If price still missing or zero, try WS openOrders to backfill by client id
				try:
					need_price = True
					if order:
						try:
							pv = float(order.get("price", 0) or 0)
						except Exception:
							pv = 0.0
						need_price = pv <= 0
					if need_price:
						# FIXED: Use REST API instead of invalid WS methods (openOrders.list/openOrders.status don't exist)
						olist_resp = await self.rest.open_orders(symbol)
						orders = self._normalize_orders(olist_resp)
						for o in orders:
							coid = o.get("clientOrderId") or o.get("origClientOrderId")
							if coid == cid:
								order = order or {}
								order["price"] = o.get("price", 0)
								break
				except Exception:
					pass
				# If still missing price, do a single REST call
				try:
					try:
						pv = float(order.get("price", 0) or 0)
					except Exception:
						pv = 0.0
					if pv <= 0:
						rest_order = await self.rest.get_order(symbol, cid)
						if isinstance(rest_order, dict):
							if "price" in rest_order:
								order = {**order, **rest_order}
							elif "p" in rest_order:
								try:
									order["price"] = float(rest_order.get("p") or 0)
								except Exception:
									order["price"] = 0
				except Exception:
					# ignore, return best effort
					pass
				# Always return dict or None (avoid propagating numeric statuses)
				return order if isinstance(order, dict) and order else None
			except Exception:
				self._record_ws_failure()
		# REST fallback only
		return await self.rest.get_order(symbol, cid)

	async def open_orders(self, symbol: str) -> List[Dict[str, Any]]:
		# WS-first with cache; avoid REST fallback during rate limits
		now_ts = time.time()
		# Serve recent cache if present
		try:
			cache_ts, cache_val = self._open_orders_cache.get(symbol, (0.0, []))
			if now_ts - cache_ts <= self._cache_ttl_sec and cache_val:
				return cache_val
		except Exception:
			pass
		# FIXED: Remove invalid WebSocket methods - use REST API for open orders
		# (v2/openOrders, openOrders.list don't exist in Binance WebSocket API)
		# REST fallback guarded by backoff and min interval
		bo = self._rest_backoff_until["open_orders"].get(symbol, 0.0)
		if now_ts < bo:
			cached_data = self._open_orders_cache.get(symbol, (0.0, []))
			return cached_data[1] if cached_data and len(cached_data) > 1 else []
		cached_data = self._open_orders_cache.get(symbol, (0.0, []))
		last_ts = cached_data[0] if cached_data and len(cached_data) > 0 else 0.0
		if now_ts - last_ts < self._rest_min_interval_sec:
			return cached_data[1] if cached_data and len(cached_data) > 1 else []
		try:
			orders = await self.rest.open_orders(symbol)
			# Ensure we never return None - always return a list
			orders = orders or []
			self._open_orders_cache[symbol] = (now_ts, orders)
			return orders
		except Exception as e:
			msg = str(e)
			if "429" in msg or "418" in msg or "Too many requests" in msg or "banned" in msg:
				self._rest_backoff_until["open_orders"][symbol] = now_ts + 60.0
			cached_data = self._open_orders_cache.get(symbol, (0.0, []))
			return cached_data[1] if cached_data and len(cached_data) > 1 else []

	async def position_risk(self, symbol: Optional[str] = None):
		# PHASE 4: Batch-first with WS fallback for maximum efficiency
		now_ts = time.time()
		
		# Try batch position manager first (94% fewer API calls)
		try:
			positions = await _batch_position_manager.get_positions(symbol)
			if positions:
				# Update local cache from batch results
				if symbol:
					self._positions_cache[symbol] = (now_ts, positions)
				return positions
		except Exception:
			pass
		
		# Fallback to individual symbol cache if available
		if symbol:
			try:
				cache_ts, cache_val = self._positions_cache.get(symbol, (0.0, []))
				if now_ts - cache_ts <= self._cache_ttl_sec and cache_val is not None:
					return cache_val
			except Exception:
				pass
		
		# WS fallback for individual symbol
		if self._ws_allowed() and symbol:
			try:
				await self.ws.connect()
				params: Dict[str, Any] = {"symbol": symbol}
				resp = await self.ws.send_request("v2/account.position", params)
				# If WS returned an error, try legacy method once
				if isinstance(resp, dict) and (resp.get("error") is not None or (isinstance(resp.get("status"), int) and resp.get("status") >= 400)):
					try:
						resp = await self.ws.send_request("account.position", params)
					except Exception:
						raise
				self._record_ws_success()
				positions = self._normalize_positions(resp)
				# Cache result
				self._positions_cache[symbol] = (now_ts, positions)
				return positions
			except Exception:
				self._record_ws_failure()
		
		# Final REST fallback (individual symbol only)
		if symbol:
			bo = self._rest_backoff_until["positions"].get(symbol, 0.0)
			if now_ts < bo:
				return self._positions_cache.get(symbol, (0.0, []))[1]
			last_ts = self._positions_cache.get(symbol, (0.0, []))[0]
			if now_ts - last_ts < self._rest_min_interval_sec:
				return self._positions_cache.get(symbol, (last_ts, []))[1]
			try:
				pos = await self.rest.position_risk(symbol)
				self._positions_cache[symbol] = (now_ts, pos or [])
				return pos or []
			except Exception as e:
				msg = str(e)
				if "429" in msg or "418" in msg or "Too many requests" in msg or "banned" in msg:
					self._rest_backoff_until["positions"][symbol] = now_ts + 60.0
				return self._positions_cache.get(symbol, (0.0, []))[1]
		
		return []

	async def update_limit_price(
		self,
		symbol: str,
		orig_cid: str,
		side: str,
		qty: float,
		new_price: float,
		reduce_only: bool = False,
		precision: Optional[int] = None,
		ws_only: bool = False,
	) -> Tuple[str, str]:
		# Prepare values
		if self.symbol_bot and hasattr(self.symbol_bot, "_prepare_order_params"):
			final_price, final_qty, _ = self.symbol_bot._prepare_order_params(symbol, side, qty, new_price, reduce_only=reduce_only)
		else:
			final_price, final_qty = new_price, qty

		# Format as strings
		price_str = str(final_price)
		qty_str = str(final_qty)
		if self.symbol_bot and hasattr(self.symbol_bot, "_format_for_exchange"):
			try:
				price_str, qty_str = self.symbol_bot._format_for_exchange(final_price, final_qty)
			except Exception:
				pass

		# Resolve WS retry budget from tuning (fallback last resort)
		ws_retry_budget = 3
		try:
			if hasattr(self.symbol_bot, "_maker_tuning") and getattr(self.symbol_bot._maker_tuning, "ws_retries_before_rest", None):
				ws_retry_budget = int(self.symbol_bot._maker_tuning.ws_retries_before_rest)
		except Exception:
			ws_retry_budget = 3

		def _log_summary(msg: str):
			try:
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					self.symbol_bot._summary_log(msg)
			except Exception:
				pass

		def _log_detailed(msg: str):
			try:
				if self.symbol_bot and hasattr(self.symbol_bot, "_detailed_log"):
					self.symbol_bot._detailed_log(msg)
			except Exception:
				pass

		# NEW: Debounce duplicate in-flight amends on the same client id
		if orig_cid in self._amend_inflight:
			_log_summary(f"ORDER_AMEND_REQUEST {{mode=ws, method=order.modify, symbol={symbol}, cid={orig_cid}}} -> SKIPPED (in-flight)")
			return orig_cid, "ws.debounce"
		self._amend_inflight[orig_cid] = time.time()
		try:
			# Always try WS order.modify first (supports price and quantity)
			if self._ws_allowed():
				for attempt in range(max(1, ws_retry_budget)):
					try:
						await self.ws.connect()
						params = {
							"symbol": symbol,
							"origClientOrderId": orig_cid,
							"side": side,
							"type": "LIMIT",
							"price": price_str,
							"quantity": qty_str,
							"timeInForce": "GTX",
							"selfTradePreventionMode": "EXPIRE_TAKER",
							"reduceOnly": reduce_only,
						}
						_log_summary(f"ORDER_AMEND_REQUEST {{mode=ws, method=order.modify, symbol={symbol}, cid={orig_cid}, side={side}, qty={qty_str}, price={price_str}, tif=GTX, ro={bool(reduce_only)}, attempt={attempt+1}}}")
						resp = await self.ws.send_request("order.modify", params)
						# Inspect envelope for errors
						if isinstance(resp, dict):
							status = resp.get("status")
							err = resp.get("error") or {}
							code = err.get("code")
							msg = err.get("msg")
							if code is not None or (isinstance(status, int) and status >= 400):
								# -5022 handled by caller via price adjust
								if code == -5022:
									raise Exception(f"-5022 WS_AMEND_ERROR status={status} code={code} msg={msg}")
								# Auth/time errors: reconnect and retry
								if code in (-1022, -1021, -1023):
									try:
										await self.ws.close()
									except Exception:
										pass
									raise Exception(f"WS_AMEND_ERROR status={status} code={code} msg={msg}")
								# Other errors: raise to retry or fallback
								raise Exception(f"WS_AMEND_ERROR status={status} code={code} msg={msg}")
						self._record_ws_success()

						# Short settle delay, then verify on the same cid
						try:
							import asyncio
							# Immediate retry - no delay needed
							pass
						except Exception:
							pass
						verified = None
						verified_price = ""
						verified_status = ""
						verify_phases: List[str] = []
						# Phase 1: WS order.get (alias fallback)
						try:
							v1 = await self.ws.send_request("order.get", {"symbol": symbol, "origClientOrderId": orig_cid})
							if isinstance(v1, dict) and (v1.get("error") is not None or (isinstance(v1.get("status"), int) and v1.get("status") >= 400)):
								try:
									v1 = await self.ws.send_request("order.status", {"symbol": symbol, "origClientOrderId": orig_cid})
								except Exception:
									pass
							verify_phases.append("ws.get")
							if isinstance(v1, dict):
								res = v1.get("result")
								if isinstance(res, dict):
									verified = dict(res)
									verified_status = str(verified.get("status", "")).upper()
									verified_price = str(verified.get("price", ""))
						except Exception as e_get:
							_log_detailed(f"ORDER_VERIFY {{phase=ws.get, error={e_get}}}")
						# Phase 2: REST fallback (FIXED: removed invalid WS methods openOrders.list/openOrders.status)
						if not verified or not verified_price:
							try:
								v2 = await self.rest.open_orders(symbol)
								verify_phases.append("rest.open_orders")
								orders = self._normalize_orders(v2)
								for o in orders:
									coid = o.get("clientOrderId") or o.get("origClientOrderId")
									if coid == orig_cid:
										verified = o
										verified_status = str(o.get("status", "")).upper()
										verified_price = str(o.get("price", ""))
										break
							except Exception as e_rest:
								_log_detailed(f"ORDER_VERIFY {{phase=rest.open_orders, error={e_rest}}}")
						# Phase 3: REST getOrder
						if not verified or not verified_price:
							try:
								rest_v = await self.rest.get_order(symbol, orig_cid)
								verify_phases.append("rest.get")
								if isinstance(rest_v, dict):
									verified = dict(rest_v)
									verified_status = str(verified.get("status", "")).upper()
									verified_price = str(verified.get("price", ""))
							except Exception as e_rest:
								_log_detailed(f"ORDER_VERIFY {{phase=rest.get, error={e_rest}}}")
						if verified and verified_status in ("NEW", "PARTIALLY_FILLED") and (verified_price == price_str or verified_price == str(final_price)):
							_log_detailed(f"OPEN_ORDER_SNAPSHOT {{cid={orig_cid}, status={verified_status}, price={verified_price}}}")
							_log_summary("ORDER_AMEND_RESPONSE {mode=ws.modify}")
							return orig_cid, "ws.modify"
						else:
							_log_detailed(f"ORDER_VERIFY_FAIL {{cid={orig_cid}, phases={verify_phases}, status={verified_status}, price={verified_price}}}")
					except Exception as e:
						self._record_ws_failure()
						msg = str(e)
						# Best-effort error code extraction
						code = None
						try:
							if "\"code\":" in msg:
								code = msg.split("\"code\":")[-1].split(",")[0].strip()
						except Exception:
							pass
						_log_summary(f"ORDER_REJECTED {{mode=ws, code={code}, msg={msg}}}")
						# Special handling: -5027 means no-op amend. Verify current order; if equal within a tick, treat as success and return.
						try:
							if "-5027" in msg:
								verified = await self.get_order(symbol, orig_cid)
								if isinstance(verified, dict):
									vstatus = str(verified.get("status", "")).upper()
									vprice = 0.0
									try:
										vprice = float(verified.get("price", 0) or 0)
									except Exception:
										vprice = 0.0
									# Determine tickSize if available
									tick = 0.0
									try:
										if self.symbol_bot and getattr(self.symbol_bot, "_symbol_filters", None):
											tick = float(self.symbol_bot._symbol_filters.get("tickSize", 0) or 0)
									except Exception:
										tick = 0.0
									thr = max(tick, 1e-12)
									target = float(final_price)
									if vstatus in ("NEW", "PARTIALLY_FILLED") and abs(vprice - target) < thr:
										_log_detailed(f"OPEN_ORDER_SNAPSHOT {{cid={orig_cid}, status={vstatus}, price={vprice}}}")
										_log_summary("ORDER_AMEND_RESPONSE {mode=ws.noop}")
										return orig_cid, "ws.noop"
						except Exception:
							pass
						# Enhanced -2013 handling: Multi-step verification without assumptions
						try:
							if "-2013" in msg:
								# Step 1: Check order status via WebSocket
								try:
									order_status = await self.ws.send_request("order.status", {"symbol": symbol, "origClientOrderId": orig_cid})
									if order_status and isinstance(order_status, dict):
										status = str(order_status.get("status", "")).upper()
										if status in ["FILLED", "PARTIALLY_FILLED"]:
											_log_summary(f"ORDER_VERIFIED_FILLED {{cid={orig_cid}, status={status}, via=ws}}")
											return orig_cid, f"order_{status.lower()}"
										elif status in ["CANCELED", "CANCELLED"]:
											_log_summary(f"ORDER_VERIFIED_CANCELLED {{cid={orig_cid}, via=ws}}")
											return orig_cid, "order_cancelled"
								except Exception:
									pass
								
								# Step 2: Check position to confirm fill impact
								try:
									positions = await self.ws.send_request("v2/account.position", {"symbol": symbol})
									if positions and isinstance(positions, list):
										position_exists = any(float(p.get("positionAmt", 0)) != 0 for p in positions)
										if position_exists:
											_log_summary(f"ORDER_FILL_CONFIRMED {{cid={orig_cid}, via=position_check}}")
											return orig_cid, "order_filled_confirmed"
								except Exception:
									pass
								
								# Step 3: Final REST verification if WebSocket unclear
								try:
									rest_order = await self.rest.get_order(symbol, orig_cid)
									if rest_order and isinstance(rest_order, dict):
										status = str(rest_order.get("status", "")).upper()
										if status in ["FILLED", "PARTIALLY_FILLED", "CANCELED", "CANCELLED"]:
											_log_summary(f"ORDER_VERIFIED_FINAL {{cid={orig_cid}, status={status}, via=rest}}")
											return orig_cid, f"order_{status.lower()}"
								except Exception:
									pass
								
								# Order truly doesn't exist or verification failed
								_log_summary(f"ORDER_NOT_FOUND {{cid={orig_cid}, verification_complete=true}}")
								return orig_cid, "order_not_found"
						except Exception:
							pass
						if attempt == ws_retry_budget - 1:
							break

			# REST fallback path (only if ws_only=False and WS failed)
			if ws_only:
				raise RuntimeError("WS-only update_limit_price failed; REST fallback disabled")

			# Build a sufficiently unique client ID for replacement order
			import time as _t, random as _r
			base_ms = int(_t.time() * 1000)
			suffix = f"{base_ms}_{_r.randint(1000,9999)}"
			new_cid = f"{symbol}_upd_{suffix}"

			# Attempt REST cancel first (ignore -2011 Unknown order)
			try:
				_log_summary(f"ORDER_AMEND_REQUEST {{mode=rest.cancel, symbol={symbol}, oldCid={orig_cid}}}")
				await self.rest.cancel(symbol, orig_cid)
			except Exception as e_cancel:
				# Log but continue to place
				_log_summary(f"ORDER_REJECTED {{mode=rest.cancel, msg={e_cancel}}}")

			# Place the replacement order via REST
			_log_summary(f"ORDER_AMEND_REQUEST {{mode=rest.place, symbol={symbol}, newCid={new_cid}, side={side}, qty={qty_str}, price={price_str}, tif=GTX, ro={str(reduce_only).lower()}}}")
			await self.rest.place_limit(symbol, side, qty_str, price_str, new_cid, reduce_only)
			_log_summary("ORDER_AMEND_RESPONSE {mode=cancel+place}")
			# Verify once
			try:
				verified = await self.get_order(symbol, new_cid)
				if isinstance(verified, dict):
					vstatus = str(verified.get("status", "")).upper()
					vprice = str(verified.get("price", ""))
					_log_detailed(f"OPEN_ORDER_SNAPSHOT {{cid={new_cid}, status={vstatus}, price={vprice}}}")
			except Exception:
				pass
			return new_cid, "cancel+place"
		finally:
			# Ensure inflight guard is cleared
			try:
				self._amend_inflight.pop(orig_cid, None)
			except Exception:
				pass

	async def place_emergency_stop(self, symbol: str, side: str, stop_price: float, cid: str) -> Tuple[Any, str]:
		"""Place emergency STOP_MARKET order with closePosition=True via WebSocket-first policy.
		
		UPDATED 2025-12-09: Migrated to Algo Service per Binance changelog (effective 2025-12-09).
		Now uses algoOrder.place (WS) and POST /fapi/v1/algoOrder (REST) endpoints.
		See: https://developers.binance.com/docs/derivatives/change-log
		"""
		if self._ws_allowed():
			try:
				await self.ws.connect()
				# NEW: Use algoOrder.place for conditional orders (Algo Service migration)
				params = {
					"symbol": symbol,
					"side": side,
					"algoType": "CONDITIONAL",
					"type": "STOP_MARKET",
					"triggerPrice": str(stop_price),
					"closePosition": "true",
					"workingType": "CONTRACT_PRICE",
					"newClientOrderId": cid
				}
				resp = await self.ws.send_request("algoOrder.place", params)
				# Check for error response
				if isinstance(resp, dict):
					status = resp.get("status")
					err = resp.get("error") or {}
					code = err.get("code")
					msg = err.get("msg")
					if code is not None or (isinstance(status, int) and status >= 400):
						raise Exception(f"ALGO_ORDER_PLACE_ERROR status={status} code={code} msg={msg}")
				self._record_ws_success()
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					try:
						self.symbol_bot._summary_log("EMERGENCY_STOP_PLACE_RESPONSE {mode=ws.algo}")
					except Exception:
						pass
				return resp, "WS"
			except Exception as e:
				self._record_ws_failure()
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					try:
						self.symbol_bot._summary_log(f"EMERGENCY_STOP_PLACE_WS_FAILED: {e}")
					except Exception:
						pass
		
		# REST fallback - use new algoOrder endpoint (Algo Service migration)
		try:
			resp = await self.rest._call("POST", "/fapi/v1/algoOrder", {
				"symbol": symbol,
				"side": side,
				"algoType": "CONDITIONAL",
				"type": "STOP_MARKET",
				"triggerPrice": str(stop_price),
				"closePosition": "true",
				"workingType": "CONTRACT_PRICE",
				"newClientOrderId": cid
			}, True)
			if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
				try:
					self.symbol_bot._summary_log("EMERGENCY_STOP_PLACE_RESPONSE {mode=rest.algo}")
				except Exception:
					pass
			return resp, "REST"
		except Exception as e:
			if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
				try:
					self.symbol_bot._summary_log(f"EMERGENCY_STOP_PLACE_REST_FAILED: {e}")
				except Exception:
					pass
			# Final visibility: both WS and REST failed
			try:
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					self.symbol_bot._summary_log("EMERGENCY_STOP_PLACE_FAILED: no ESL placed after WS and REST attempts")
			except Exception:
				pass
			raise

	async def modify_emergency_stop(self, symbol: str, cid: str, side: str, new_stop_price: float) -> Tuple[str, str]:
		"""Modify emergency STOP_MARKET order stop price via cancel-and-replace.
		
		UPDATED 2025-12-09: Migrated to Algo Service per Binance changelog (effective 2025-12-09).
		Modification of untriggered conditional orders is NO LONGER SUPPORTED.
		Must use cancel-and-replace: algoOrder.cancel then algoOrder.place.
		See: https://developers.binance.com/docs/derivatives/change-log
		
		Returns: (new_cid, mode) - new_cid may differ from input cid after replacement
		"""
		# Debounce per-cid to avoid concurrent modify on the same emergency order
		if cid in self._amend_inflight:
			try:
				if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
					self.symbol_bot._summary_log(f"EMERGENCY_ORDER_AMEND_REQUEST {{cid={cid}}} -> SKIPPED (in-flight)")
			except Exception:
				pass
			return cid, "ws.debounce"
		self._amend_inflight[cid] = time.time()
		
		# Generate new client order ID for replacement order
		import random
		new_cid = f"esl-{symbol}-{int(time.time() * 1000)}"
		
		try:
			# STEP 1: Cancel existing algo order
			cancel_success = False
			if self._ws_allowed():
				try:
					await self.ws.connect()
					cancel_params = {
						"symbol": symbol,
						"origClientOrderId": cid
					}
					cancel_resp = await self.ws.send_request("algoOrder.cancel", cancel_params)
					# Check for error response
					if isinstance(cancel_resp, dict):
						status = cancel_resp.get("status")
						err = cancel_resp.get("error") or {}
						code = err.get("code")
						if code is None and (status is None or status == 200):
							cancel_success = True
							self._record_ws_success()
						else:
							# Log but continue - order might already be triggered/cancelled
							if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
								try:
									self.symbol_bot._summary_log(f"EMERGENCY_STOP_CANCEL_WS_WARN: code={code} msg={err.get('msg')}")
								except Exception:
									pass
					else:
						cancel_success = True
						self._record_ws_success()
				except Exception as e:
					self._record_ws_failure()
					if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
						try:
							self.symbol_bot._summary_log(f"EMERGENCY_STOP_CANCEL_WS_FAILED: {e}")
						except Exception:
							pass
			
			# REST fallback for cancel if WS failed
			if not cancel_success:
				try:
					await self.rest._call("DELETE", "/fapi/v1/algoOrder", {
						"symbol": symbol,
						"origClientOrderId": cid
					}, True)
					cancel_success = True
				except Exception as e:
					# Log but continue - order might already be triggered/cancelled
					if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
						try:
							self.symbol_bot._summary_log(f"EMERGENCY_STOP_CANCEL_REST_WARN: {e}")
						except Exception:
							pass
					# Continue anyway - we'll try to place the new order
					cancel_success = True  # Assume old order is gone
			
			# STEP 2: Place new algo order with updated stop price
			if cancel_success:
				place_success = False
				if self._ws_allowed():
					try:
						await self.ws.connect()
						place_params = {
							"symbol": symbol,
							"side": side,
							"algoType": "CONDITIONAL",
							"type": "STOP_MARKET",
							"triggerPrice": str(new_stop_price),
							"closePosition": "true",
							"workingType": "CONTRACT_PRICE",
							"newClientOrderId": new_cid
						}
						place_resp = await self.ws.send_request("algoOrder.place", place_params)
						# Check for error response
						if isinstance(place_resp, dict):
							status = place_resp.get("status")
							err = place_resp.get("error") or {}
							code = err.get("code")
							if code is None and (status is None or status == 200):
								place_success = True
								self._record_ws_success()
							else:
								raise Exception(f"ALGO_ORDER_PLACE_ERROR status={status} code={code} msg={err.get('msg')}")
						else:
							place_success = True
							self._record_ws_success()
					except Exception as e:
						self._record_ws_failure()
						if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
							try:
								self.symbol_bot._summary_log(f"EMERGENCY_STOP_REPLACE_WS_FAILED: {e}")
							except Exception:
								pass
				
				# REST fallback for place if WS failed
				if not place_success:
					try:
						await self.rest._call("POST", "/fapi/v1/algoOrder", {
							"symbol": symbol,
							"side": side,
							"algoType": "CONDITIONAL",
							"type": "STOP_MARKET",
							"triggerPrice": str(new_stop_price),
							"closePosition": "true",
							"workingType": "CONTRACT_PRICE",
							"newClientOrderId": new_cid
						}, True)
						place_success = True
					except Exception as e:
						if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
							try:
								self.symbol_bot._summary_log(f"EMERGENCY_STOP_REPLACE_REST_FAILED: {e}")
							except Exception:
								pass
						raise
				
				if place_success:
					if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
						try:
							self.symbol_bot._summary_log(f"EMERGENCY_STOP_MODIFY_RESPONSE {{mode=cancel+replace, old_cid={cid}, new_cid={new_cid}}}")
						except Exception:
							pass
					return new_cid, "cancel+replace"
			
			# If we get here, something failed
			if self.symbol_bot and hasattr(self.symbol_bot, "_summary_log"):
				try:
					self.symbol_bot._summary_log("EMERGENCY_STOP_MODIFY_FAILED: cancel+replace unsuccessful")
				except Exception:
					pass
			return cid, "failed"
		finally:
			try:
				self._amend_inflight.pop(cid, None)
			except Exception:
				pass