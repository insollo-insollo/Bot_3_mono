import asyncio
import json
import time
import hmac
import logging
import random  # for backoff jitter
from typing import Dict, Any, Tuple, Optional

import aiohttp

class TradingWsClient:
	"""Binance WS trading client (v1) with per-request signing."""
	def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str, secret_key: str, recv_window: int = 5000, request_timeout: float = 5.0):
		self.session = session
		self.url = url
		self.api_key = api_key
		self.secret_key = secret_key
		self.recv_window = recv_window
		self.request_timeout = request_timeout
		self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
		self._id_counter = 0
		self._pending: Dict[int, asyncio.Future] = {}
		self._lock = asyncio.Lock()
		self._reader_task: Optional[asyncio.Task] = None
		self._logger = logging.getLogger("bot_2_0_ws")
		# NEW: Serialize connect/reader creation to avoid concurrent receive()
		self._connect_lock = asyncio.Lock()
		# Connection failure tracking for exponential backoff
		self._consecutive_failures: int = 0
		self._max_backoff: float = 30.0  # Maximum 30 seconds backoff

	async def connect(self):
		async with self._connect_lock:
			if self._ws and not self._ws.closed:
				# Ensure reader task exists and is alive
				if not self._reader_task or self._reader_task.done():
					self._reader_task = asyncio.create_task(self._reader())
				# Reset failure count on successful reuse
				self._consecutive_failures = 0
				return
			headers = {"X-MBX-APIKEY": self.api_key}
			# Close any prior ws safely
			if self._reader_task and not self._reader_task.done():
				try:
					self._reader_task.cancel()
					try:
						await self._reader_task
					except Exception:
						pass
				except Exception:
					pass
			self._reader_task = None
			if self._ws and not self._ws.closed:
				try:
					await self._ws.close()
				except Exception:
					pass
			try:
				self._ws = await self.session.ws_connect(self.url, headers=headers, heartbeat=15)
				self._reader_task = asyncio.create_task(self._reader())
				self._logger.info(f"WS-TRADING connected url={self.url}")
				# Reset failure count on successful connection
				self._consecutive_failures = 0
			except Exception as e:
				# Increment failure count and calculate backoff for actual failures
				self._consecutive_failures += 1
				backoff_time = min(
					0.1 * (2 ** (self._consecutive_failures - 1)),  # Start with 0.1s, exponential backoff
					self._max_backoff
				)
				self._logger.error(f"WS-TRADING connect error: {type(e).__name__}: {e} (failure #{self._consecutive_failures}, backoff {backoff_time:.1f}s)")
				if backoff_time > 0.1:  # Only wait for repeated failures
					await asyncio.sleep(backoff_time)
				raise

	async def close(self):
		if self._reader_task:
			try:
				self._reader_task.cancel()
				try:
					await self._reader_task
				except Exception:
					pass
			except Exception:
				pass
		if self._ws and not self._ws.closed:
			await self._ws.close()
		self._ws = None

	async def _reader(self):
		assert self._ws is not None
		ws = self._ws
		while True:
			msg = await ws.receive()
			if msg.type == aiohttp.WSMsgType.TEXT:
				try:
					data = json.loads(msg.data)
					rid = data.get("id")
					if rid is not None and rid in self._pending:
						fut = self._pending.pop(rid)
						fut.set_result(data)
				except Exception as e:
					self._logger.debug(f"WS-TRADING reader parse error: {e}")
			elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
				# Reject all pending
				pending_count = len(self._pending)
				for rid, fut in list(self._pending.items()):
					if not fut.done():
						fut.set_exception(RuntimeError("WS trading connection closed"))
				self._pending.clear()
				self._logger.error(f"WS-TRADING connection closed type={msg.type} dropped_pending={pending_count}")
				break

	async def _next_id(self) -> int:
		async with self._lock:
			self._id_counter += 1
			return self._id_counter

	def _prepare_future(self, rid: int) -> asyncio.Future:
		fut: asyncio.Future = asyncio.get_running_loop().create_future()
		self._pending[rid] = fut
		return fut

	def _sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
		signed = dict(params)
		ts = int(time.time() * 1000)
		signed.update({
			"apiKey": self.api_key,
			"timestamp": ts,
			"recvWindow": self.recv_window,
		})
		def _canon(v: Any) -> Any:
			if isinstance(v, bool):
				return "true" if v else "false"
			return v
		items = sorted((k, _canon(v)) for k, v in signed.items() if k != "signature" and v is not None)
		query = "&".join(f"{k}={v}" for k, v in items)
		sig = hmac.new(self.secret_key.encode(), query.encode(), "sha256").hexdigest()
		signed["signature"] = sig
		return signed

	async def send_request(self, method: str, params: Dict[str, Any], timeout: float = None) -> Dict[str, Any]:
		await self.connect()
		assert self._ws is not None
		rid = await self._next_id()
		full_params = self._sign_params(params)
		req = {"id": rid, "method": method, "params": full_params}
		fut = self._prepare_future(rid)
		meta = {
			"method": method,
			"symbol": params.get("symbol"),
			"side": params.get("side"),
			"type": params.get("type"),
			"cid": params.get("newClientOrderId") or params.get("origClientOrderId")
		}
		start = time.time()
		try:
			await self._ws.send_str(json.dumps(req))
			resp = await asyncio.wait_for(fut, timeout or self.request_timeout)
			elapsed = int((time.time() - start) * 1000)
			self._logger.debug(f"WS-TRADING ok {meta} in {elapsed}ms")
			return resp
		except asyncio.TimeoutError as e:
			elapsed = int((time.time() - start) * 1000)
			self._logger.error(f"WS-TRADING timeout {meta} after {elapsed}ms pending={len(self._pending)}")
			raise
		except Exception as e:
			elapsed = int((time.time() - start) * 1000)
			self._logger.error(f"WS-TRADING error {meta} {type(e).__name__}: {e} after {elapsed}ms pending={len(self._pending)}")
			raise 