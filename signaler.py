import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

BASE_DIR = Path(__file__).resolve().parent
TRADE_EVENTS_LOG = BASE_DIR / "logs" / "trade_events.log"
ENTRY_ORDERS_LOG = BASE_DIR / "logs" / "entry_orders.log"
SIGNALS_PATH = BASE_DIR / "signals.json"
CONFIG_PATH = BASE_DIR / "signaler_config.json"
ENGINE_CONFIG_PATH = BASE_DIR / "config.json"

MAX_CONCURRENT_DEFAULT = 8
PENDING_TIMEOUT_DEFAULT = 300  # seconds


def atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path: Path, default):
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return default


class ActiveTrades:
    def __init__(self):
        # key: (pair, user_id, trade_id)
        self.active: Set[Tuple[str, int, str]] = set()
        # pending entries per pair (no trade_id yet) -> timestamp reserved
        self.pending_pairs: Dict[str, float] = {}
        # track side per pair from TRADE_STARTED events
        self._pair_side: Dict[str, str] = {}

    def on_trade_event(self, event: dict):
        et = event.get("event_type")
        pair = event.get("pair")
        uid = event.get("user_id")
        trade_id = event.get("trade_id")
        if not pair or uid is None:
            return
        if et == "TRADE_STARTED" and trade_id:
            self.active.add((pair, int(uid), trade_id))
            # capture side for this pair (used for flattening)
            side = str(event.get("side", "")).upper()
            if side in ("LONG", "SHORT"):
                self._pair_side[pair] = side
            # when trade starts, clear pending for this pair
            self.pending_pairs.pop(pair, None)
        elif et == "TRADE_COMPLETED" and trade_id:
            self.active.discard((pair, int(uid), trade_id))
            # completion also clears pending just in case
            self.pending_pairs.pop(pair, None)
            # if no more active for this pair, drop side
            if pair not in {p for (p, _u, _t) in self.active}:
                self._pair_side.pop(pair, None)

    def on_entry_event(self, event: dict):
        # Monitor entry lifecycle to reserve/release capacity
        et = event.get("event_type")
        pair = event.get("pair")
        if not pair:
            return
        if et == "ENTRY_ORDER_PLACED":
            # only mark pending if this pair isn't already active
            if pair not in {p for (p, _u, _t) in self.active}:
                self.pending_pairs[pair] = time.time()
        elif et in ("ENTRY_ORDER_CANCELED", "ENTRY_ORDER_EXPIRED"):
            # release reservation on explicit cancel/expire
            self.pending_pairs.pop(pair, None)

    def prune_timeouts(self, now_ts: float, timeout_s: int) -> int:
        if timeout_s <= 0 or not self.pending_pairs:
            return 0
        to_clear = [p for p, ts in self.pending_pairs.items() if (now_ts - ts) >= timeout_s]
        for p in to_clear:
            self.pending_pairs.pop(p, None)
        return len(to_clear)

    def count(self) -> int:
        # capacity usage = active trades + pending entries
        return len(self.active) + len(self.pending_pairs)

    def active_pairs(self) -> Set[str]:
        return {p for (p, _u, _t) in self.active} | set(self.pending_pairs.keys())

    def active_sides_by_pair(self) -> Dict[str, str]:
        # return a shallow copy to avoid external mutations
        return dict(self._pair_side)


def get_engine_pairs() -> List[str]:
    """Read pairs from bot_multi/config.json to know valid symbols for random mode."""
    cfg = load_json(ENGINE_CONFIG_PATH, {})
    pairs_cfg = cfg.get("pairs", {})
    return list(pairs_cfg.keys())


def read_existing_events(path: Path, prefix: str) -> List[dict]:
    """One-shot read of existing events in a log file (no tailing)."""
    events: List[dict] = []
    if not path.exists():
        return events
    with path.open("r") as f:
        for line in f:
            if prefix in line:
                json_str = line.split(prefix, 1)[-1].strip()
                try:
                    events.append(json.loads(json_str))
                except json.JSONDecodeError:
                    continue
    return events


async def tail_file(path: Path, prefix: str, initial_scan: bool = True):
    pos = 0
    while not path.exists():
        await asyncio.sleep(1)
    if initial_scan:
        with path.open("r") as f:
            for line in f:
                if prefix in line:
                    json_str = line.split(prefix, 1)[-1].strip()
                    try:
                        yield json.loads(json_str)
                    except json.JSONDecodeError:
                        continue
            pos = f.tell()
    with path.open("r") as f:
        f.seek(pos)
        while True:
            where = f.tell()
            line = f.readline()
            if not line:
                await asyncio.sleep(0.5)
                f.seek(where)
            else:
                if prefix in line:
                    json_str = line.split(prefix, 1)[-1].strip()
                    try:
                        yield json.loads(json_str)
                    except json.JSONDecodeError:
                        continue


async def tail_trade_events(initial_scan: bool = True):
    async for ev in tail_file(TRADE_EVENTS_LOG, "TRADE_EVENT: ", initial_scan):
        yield ev


async def tail_entry_events(initial_scan: bool = True):
    async for ev in tail_file(ENTRY_ORDERS_LOG, "ENTRY_ORDER_EVENT: ", initial_scan):
        yield ev


def choose_next_pairs(plan: Dict[str, str], occupied_pairs: Set[str], n: int) -> List[Tuple[str, str]]:
    """Choose up to n pairs not currently occupied, preserving dict order."""
    chosen: List[Tuple[str, str]] = []
    for pair, direction in plan.items():
        if pair in occupied_pairs:
            continue
        if direction.upper() not in ("LONG", "SHORT", "FLAT"):
            continue
        chosen.append((pair, direction.upper()))
        if len(chosen) >= n:
            break
    return chosen


def choose_random_pairs(candidates: List[str], occupied_pairs: Set[str], n: int) -> List[Tuple[str, str]]:
    """Choose up to n random pairs not currently occupied, with random LONG/SHORT directions."""
    available = [p for p in candidates if p not in occupied_pairs]
    if not available or n <= 0:
        return []
    random.shuffle(available)
    pick = available[:n]
    return [(p, random.choice(["LONG", "SHORT"])) for p in pick]


def emit_signals(pairs_to_signal: List[Tuple[str, str]]):
    if not pairs_to_signal:
        return
    signals = load_json(SIGNALS_PATH, {})
    for pair, direction in pairs_to_signal:
        # Avoid creating a second signal if there is an unprocessed one already
        existing = signals.get(pair)
        if isinstance(existing, dict) and existing.get("processed") is False:
            logging.info(f"[signaler] Skipping emit for {pair}, unprocessed signal already exists")
            continue
        signals[pair] = {"type": direction.lower(), "processed": False}
        logging.info(f"[signaler] Emitting signal: {pair} -> {direction}")
    atomic_write_json(SIGNALS_PATH, signals)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        logging.error("[signaler] Missing signaler_config.json. Create it from the README example.")
        return

    mode = str(cfg.get("mode", "plan")).lower()  # "plan" | "random"
    max_concurrent = int(cfg.get("max_concurrent_trades", MAX_CONCURRENT_DEFAULT))
    plan: Dict[str, str] = cfg.get("plan", {})  # ignored when empty or in random mode
    flatten_on_start: bool = bool(cfg.get("flatten_open_trades_on_start", False))
    pending_timeout_s: int = int(cfg.get("pending_timeout_seconds", PENDING_TIMEOUT_DEFAULT))

    # Candidates for random mode come from engine config pairs
    engine_pairs = get_engine_pairs()
    random_candidates = engine_pairs if mode == "random" else []

    if mode == "random" and not random_candidates:
        logging.error("[signaler] Random mode requires pairs in bot_multi/config.json")
        return

    # Filter plan to engine-configured pairs only (preserve order)
    if mode == "plan" and engine_pairs:
        allowed = set(engine_pairs)
        plan = {p: d for p, d in plan.items() if p in allowed}

    tracker = ActiveTrades()

    # Initial reconstruction from both logs (one-shot scan, do not tail here)
    for ev in read_existing_events(TRADE_EVENTS_LOG, "TRADE_EVENT: "):
        tracker.on_trade_event(ev)
    for ev in read_existing_events(ENTRY_ORDERS_LOG, "ENTRY_ORDER_EVENT: "):
        tracker.on_entry_event(ev)

    # Reserve capacity for any existing unprocessed signals to avoid overflow at startup
    existing_signals = load_json(SIGNALS_PATH, {})
    allowed_pairs_set = set(engine_pairs) if engine_pairs else None
    now_ts = time.time()
    for pair, sig in (existing_signals.items() if isinstance(existing_signals, dict) else []):
        try:
            if not isinstance(sig, dict):
                continue
            if sig.get("processed", True) is False:
                if allowed_pairs_set is None or pair in allowed_pairs_set:
                    if pair not in {p for (p, _u, _t) in tracker.active}:
                        # seed reservation timestamp for unprocessed signals
                        tracker.pending_pairs.setdefault(pair, now_ts)
        except Exception:
            continue

    # Optional: free any stale pending based on timeout
    cleared = tracker.prune_timeouts(now_ts, pending_timeout_s)
    if cleared:
        logging.info(f"[signaler] Cleared {cleared} stale pending reservations on startup")

    logging.info(f"[signaler] Starting with {tracker.count()} active+pending (mode={mode})")

    # Optional: Trigger exits for currently open trades using opposite signals
    if flatten_on_start:
        sides = tracker.active_sides_by_pair()
        if sides:
            to_flatten: List[Tuple[str, str]] = []
            for pair, side in sides.items():
                # select opposite signal to trigger maker exit in the bot
                opp = "SHORT" if side == "LONG" else "LONG"
                # Only flatten engine-configured pairs
                if not engine_pairs or pair in engine_pairs:
                    to_flatten.append((pair, opp))
            if to_flatten:
                emit_signals(to_flatten)

    # Fill capacity immediately after initialization
    capacity = max(0, max_concurrent - tracker.count())
    if capacity > 0:
        occupied = tracker.active_pairs()
        if mode == "random":
            to_signal = choose_random_pairs(random_candidates, occupied, capacity)
        else:
            to_signal = choose_next_pairs(plan, occupied, capacity)
        if to_signal:
            emit_signals(to_signal)

    # Tail updates and continue emitting when capacity frees up
    trade_gen = tail_trade_events(initial_scan=False)
    entry_gen = tail_entry_events(initial_scan=False)
    trade_task = asyncio.create_task(trade_gen.__anext__())
    entry_task = asyncio.create_task(entry_gen.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({trade_task, entry_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is trade_task:
                    ev = t.result()
                    tracker.on_trade_event(ev)
                    trade_task = asyncio.create_task(trade_gen.__anext__())
                else:
                    ev = t.result()
                    tracker.on_entry_event(ev)
                    entry_task = asyncio.create_task(entry_gen.__anext__())

            # Before emitting more, free any stale pending reservations
            cleared = tracker.prune_timeouts(time.time(), pending_timeout_s)
            if cleared:
                logging.info(f"[signaler] Cleared {cleared} stale pending reservations")

            capacity = max(0, max_concurrent - tracker.count())
            if capacity <= 0:
                continue

            occupied = tracker.active_pairs()
            if mode == "random":
                to_signal = choose_random_pairs(random_candidates, occupied, capacity)
            else:
                to_signal = choose_next_pairs(plan, occupied, capacity)

            if to_signal:
                emit_signals(to_signal)
    finally:
        for t in (trade_task, entry_task):
            if not t.done():
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass 