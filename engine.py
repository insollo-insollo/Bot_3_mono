import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

import aiohttp

from core import PairCfg, UserCfg, SymbolBot, BinanceREST, ws_prices, load_json  # type: ignore
from order_manager import OrderManager
from ws_trading import TradingWsClient
from ws_signal_bridge import run_ws_signals_bridge

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path('/root')
CONFIG_PATH = CURRENT_DIR / 'config.json'
SIGNALS_PATH = CURRENT_DIR / 'signals.json'  # Use local signals file to match bot_22_A.py

LOG_DIR = CURRENT_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def select_ws_endpoint(cfg_trading: dict) -> str:
	custom = cfg_trading.get('ws_url')
	if custom:
		return custom
	return 'wss://ws-fapi.binance.com/ws-fapi/v1'


async def user_data_stream(user: UserCfg, bot_registry: Dict[str, SymbolBot]) -> None:
	"""Handle user data stream for real-time order and position updates"""
	listen_key = None
	last_keepalive = 0
	
	while True:
		try:
			# Get or refresh listen key
			if not listen_key or time.time() - last_keepalive > 1800:  # 30 minutes
				rest = BinanceREST(user.api_key, user.secret_key, aiohttp.ClientSession())
				listen_key = await rest.get_listen_key()
				await rest.keepalive_listen_key(listen_key)
				last_keepalive = time.time()
				logging.info(f"User data stream connected: user={user.uid}")
			
			# Connect to user data stream
			url = f"wss://fstream.binance.com/ws/{listen_key}"
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(url) as ws:
					async for msg in ws:
						if msg.type == aiohttp.WSMsgType.TEXT:
							try:
								data = json.loads(msg.data)
								await process_user_data_event(data, bot_registry)
							except Exception as e:
								logging.error(f"User data processing error: {e}")
						elif msg.type == aiohttp.WSMsgType.ERROR:
							break
						
						# Keepalive check
						if time.time() - last_keepalive > 1800:
							break
		
		except Exception as e:
			logging.error(f"User data stream error: {e}")
			await asyncio.sleep(5)
			listen_key = None  # Force refresh


async def process_user_data_event(data: dict, bot_registry: Dict[str, SymbolBot]) -> None:
	"""Process user data stream events and route to appropriate bots"""
	event_type = data.get("e")
	
	if event_type == "ORDER_TRADE_UPDATE" and "o" in data:
		order_data = data["o"]
		symbol = order_data.get("s")
		status = order_data.get("X", "").upper()
		client_id = order_data.get("c", "")
		
		if symbol and symbol in bot_registry:
			bot = bot_registry[symbol]
			# Minimal but informative logging
			if status in ["FILLED", "PARTIALLY_FILLED"]:
				price = float(order_data.get("ap", 0) or order_data.get("p", 0))
				qty = float(order_data.get("z", 0))
				logging.info(f"ORDER_FILLED: {symbol} {status} qty={qty} price={price} cid={client_id}")
			
			# Notify bot of order update
			try:
				await bot.handle_user_data_event(data)
			except Exception as e:
				logging.error(f"Bot event handling error {symbol}: {e}")
	
	elif event_type == "ACCOUNT_UPDATE" and "a" in data:
		account_data = data["a"]
		positions = account_data.get("P", [])
		
		for pos_data in positions:
			symbol = pos_data.get("s")
			if symbol and symbol in bot_registry:
				bot = bot_registry[symbol]
				# Minimal position update logging
				pos_amt = float(pos_data.get("pa", 0))
				if pos_amt != 0:
					entry_price = float(pos_data.get("ep", 0))
					logging.info(f"POSITION_UPDATE: {symbol} qty={pos_amt} entry={entry_price}")
				
				try:
					await bot.handle_user_data_event(data)
				except Exception as e:
					logging.error(f"Bot position update error {symbol}: {e}")


async def main():
	cfg = load_json(CONFIG_PATH, {})
	if not cfg:
		logging.error(f"[bot_2_0] Missing config: {CONFIG_PATH}")
		return

	pair_cfg: Dict[str, PairCfg] = {p: PairCfg(**v) for p, v in cfg.get('pairs', {}).items()}

	users: List[UserCfg] = []
	for u in cfg.get('users', []):
		uc = UserCfg(**u)
		if uc.api_key and uc.secret_key:
			users.append(uc)
		else:
			logging.info(f"[bot_2_0] Skipping user {uc.uid} - no API credentials")
	if not users:
		logging.error('[bot_2_0] No users with valid API credentials')
		return

	prices: Dict[str, float] = {}
	user_sessions: Dict[int, aiohttp.ClientSession] = {
		u.uid: aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7), trust_env=False) for u in users
	}
	user_rests: Dict[int, BinanceREST] = {}
	user_ws_clients: Dict[int, TradingWsClient] = {}

	cfg_trading = cfg.get('trading', {})
	cfg_signals = cfg.get('signals', {})
	# Start prices WS for all configured symbols (optional)
	all_symbols = list(pair_cfg.keys())
	bg_tasks: List[asyncio.Task] = []
	if all_symbols:
		bg_tasks.append(asyncio.create_task(ws_prices(all_symbols, prices)))

	# If signals source is WS, start the external WS bridge that writes into signals.json
	if cfg_signals.get('source', 'file') == 'ws':
		bridge_task = await run_ws_signals_bridge()
		if bridge_task is not None:
			bg_tasks.append(bridge_task)

	# Dynamic symbol bot management
	# symbol -> user_id -> (task, backoff_seconds)
	symbol_tasks: Dict[str, Dict[int, Tuple[asyncio.Task, float]]] = {}
	started_symbols: Set[str] = set()
	
	# Bot registry for user data stream routing
	bot_registry: Dict[str, SymbolBot] = {}

	def ensure_user_clients(user: UserCfg):
		# Helper ensures REST and WS client exist for user
		rest = user_rests.get(user.uid)
		if rest is None:
			sess = user_sessions[user.uid]
			rest = BinanceREST(user.api_key, user.secret_key, sess, user.ip)
			user_rests[user.uid] = rest
		ws_client = user_ws_clients.get(user.uid)
		if ws_client is None:
			ws_url = select_ws_endpoint(cfg_trading)
			sess = user_sessions[user.uid]
			ws_client = TradingWsClient(sess, ws_url, user.api_key, user.secret_key)
			user_ws_clients[user.uid] = ws_client
		return rest, ws_client

	async def start_symbol_for_user(symbol: str, user: UserCfg, initial_backoff: float = 0.5) -> None:
		# Construct bot and start supervised task for one user+symbol
		rest, ws_client = ensure_user_clients(user)
		om = OrderManager(rest, ws_client)
		bot = SymbolBot(symbol, pair_cfg[symbol], user, rest, prices, om)
		om.symbol_bot = bot
		
		# Add to bot registry for user data stream routing
		bot_registry[symbol] = bot
		
		# NEW: Pre-warm WebSocket connection with a small delay to avoid startup bursts
		try:
			# Immediate WS connection - no delays needed
			await ws_client.connect()
		except Exception as e:
			logging.warning(f"[engine] Pre-warm WS connection failed for {symbol}-{user.uid}: {e}")
			# Continue anyway - OrderManager will handle reconnection

		async def run_bot():
			await bot.run()

		backoff = initial_backoff
		task = asyncio.create_task(run_bot())
		if symbol not in symbol_tasks:
			symbol_tasks[symbol] = {}
		symbol_tasks[symbol][user.uid] = (task, backoff)

		def _on_done(t: asyncio.Task, sym: str = symbol, uid: int = user.uid) -> None:
			try:
				exc = t.exception()
			except asyncio.CancelledError:
				logging.info(f"[engine] Bot task cancelled: {sym} user={uid}")
				return
			except Exception as e:  # noqa: BLE001
				logging.exception(f"[engine] Unexpected error reading task result for {sym} user={uid}: {e}")
				exc = None
			# Supervise and restart on failure
			if exc is not None:
				_, prev_backoff = symbol_tasks.get(sym, {}).get(uid, (None, initial_backoff))
				next_backoff = min(prev_backoff * 2.0, 60.0)
				logging.error(f"[engine] Bot task crashed for {sym} user={uid}: {exc}. Restarting in {next_backoff:.1f}s")
				async def _restart_later():
					await asyncio.sleep(next_backoff)
					await start_symbol_for_user(sym, next(u for u in users if u.uid == uid), next_backoff)
				asyncio.create_task(_restart_later())
			else:
				logging.info(f"[engine] Bot task finished gracefully for {sym} user={uid}")

		task.add_done_callback(_on_done)
		logging.info(f"[engine] Started bot: {symbol} for user={user.uid}")

	async def signals_watcher(poll_seconds: float = 2.0) -> None:
		logging.info("[engine] Signals watcher started")
		while True:
			try:
				signals: Dict[str, dict] = load_json(SIGNALS_PATH, {}) or {}
				desired_symbols: Set[str] = {s for s in signals.keys() if s in pair_cfg}
				# Start any new desired symbols - immediate startup
				for sym in sorted(desired_symbols):
					if sym in started_symbols:
						continue
					for u in users:
						await start_symbol_for_user(sym, u)
					started_symbols.add(sym)
				# (Optional) We keep running symbols even if removed to allow exits; could implement stop logic later
			except Exception as e:  # noqa: BLE001
				logging.exception(f"[engine] Signals watcher error: {e}")
			finally:
				await asyncio.sleep(poll_seconds)

	# Seed any existing signals at startup - immediate parallel startup
	seed_signals = load_json(SIGNALS_PATH, {}) or {}
	for sym in sorted(s for s in seed_signals.keys() if s in pair_cfg):
		if sym not in started_symbols:
			for u in users:
				await start_symbol_for_user(sym, u)
			started_symbols.add(sym)

	# Start watcher
	bg_tasks.append(asyncio.create_task(signals_watcher()))
	
	# Start user data streams for real-time order/position updates
	for user in users:
		bg_tasks.append(asyncio.create_task(user_data_stream(user, bot_registry)))
		logging.info(f"User data stream started: user={user.uid}")

	try:
		# Keep engine alive with background tasks (prices WS + signals watcher + user data streams + all bots)
		await asyncio.gather(*bg_tasks)
	finally:
		await asyncio.gather(*[s.close() for s in user_sessions.values()], return_exceptions=True)


if __name__ == '__main__':
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		pass 