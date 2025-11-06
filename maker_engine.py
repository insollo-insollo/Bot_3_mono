import time
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List


@dataclass
class MakerTuning:
    ladder_ticks: List[int] = None
    ladder_debounce_ms: int = 30
    amend_time_budget_ms: int = 1200
    ws_retries_before_rest: int = 5
    wide_spread_extra_ticks: int = 1  # add +1 tick if spread ≥ 2 ticks
    volatility_extra_ticks_low: int = 0
    volatility_extra_ticks_med: int = 1
    volatility_extra_ticks_high: int = 2
    # Size/Depth heuristic: ratio_to_topDepth -> extra ticks
    large_size_threshold_ticks: List[Tuple[float, int]] = None
    
    # NEW: High Volatility Mode parameters
    # Normal mode (default)
    normal_min_tick_diff_entry: int = 5
    normal_min_tick_diff_exit: int = 10
    normal_min_amend_interval: float = 2.0
    normal_tolerance_ticks: int = 1
    
    # High Volatility mode (activated when volatility detected)
    hv_min_tick_diff_entry: int = 20
    hv_min_tick_diff_exit: int = 30
    hv_min_amend_interval: float = 5.0
    hv_tolerance_ticks: int = 5
    hv_max_amendments_per_order: int = 3

    def __post_init__(self):
        if self.ladder_ticks is None:
            self.ladder_ticks = [0, 1, 2, 3, 5, 8]
        if self.large_size_threshold_ticks is None:
            self.large_size_threshold_ticks = [
                (1.0, 0),
                (2.0, 2),
                (3.0, 3),
                (5.0, 5),
                (8.0, 8),
            ]
    
    def get_params(self, pair: str, volatility_mode: str = "NORMAL") -> dict:
        """Get parameters based on current volatility mode"""
        if volatility_mode == "HIGH_VOLATILITY":
            return {
                'min_tick_diff_entry': self.hv_min_tick_diff_entry,
                'min_tick_diff_exit': self.hv_min_tick_diff_exit,
                'min_amend_interval': self.hv_min_amend_interval,
                'tolerance_ticks': self.hv_tolerance_ticks,
                'max_amendments': self.hv_max_amendments_per_order
            }
        else:
            return {
                'min_tick_diff_entry': self.normal_min_tick_diff_entry,
                'min_tick_diff_exit': self.normal_min_tick_diff_exit,
                'min_amend_interval': self.normal_min_amend_interval,
                'tolerance_ticks': self.normal_tolerance_ticks,
                'max_amendments': 999  # Unlimited in normal mode
            }


@dataclass
class Book:
    best_bid: float
    best_ask: float
    bid_qty: float
    ask_qty: float
    ts: float

    @property
    def spread(self) -> float:
        return max(0.0, self.best_ask - self.best_bid)


class PegPriceResolver:
    def __init__(self, tuning: Optional[MakerTuning] = None):
        self.tuning = tuning or MakerTuning()

    @staticmethod
    def _estimate_volatility_tag(book: Book, tick: float) -> str:
        # Simple, local heuristic: wide spread -> higher vol tag
        # Can be replaced by a real volatility detector later
        try:
            spread_ticks = int(round(book.spread / max(tick, 1e-12)))
        except Exception:
            spread_ticks = 0
        if spread_ticks >= 5:
            return "high"
        if spread_ticks >= 2:
            return "med"
        return "low"

    def _size_offset_ticks(self, order_qty: float, top_depth: float) -> int:
        if top_depth <= 0:
            return 0
        ratio = order_qty / top_depth
        extra = 0
        for threshold, ticks in self.tuning.large_size_threshold_ticks:
            if ratio >= threshold:
                extra = ticks
        return extra

    def resolve_price(self, *,
                      side: str,
                      order_type: str,  # "entry"|"tp"|"sl"
                      qty: float,
                      book: Book,
                      tick: float) -> float:
        # Base edge: entries and SL peg to primary side; TP pegs to opposite side
        if order_type == "tp":
            base = book.best_ask if side.upper() == "SELL" else book.best_bid
        else:
            base = book.best_bid if side.upper() == "BUY" else book.best_ask

        # Offsets
        vol_tag = self._estimate_volatility_tag(book, tick)
        vol_extra = {
            "low": self.tuning.volatility_extra_ticks_low,
            "med": self.tuning.volatility_extra_ticks_med,
            "high": self.tuning.volatility_extra_ticks_high,
        }.get(vol_tag, 0)

        # Spread-based extra
        try:
            spread_ticks = int(round(book.spread / max(tick, 1e-12)))
        except Exception:
            spread_ticks = 0
        wide_spread_extra = self.tuning.wide_spread_extra_ticks if spread_ticks >= 2 else 0

        # Top-of-book size heuristic
        top_depth = book.bid_qty if side.upper() == "BUY" else book.ask_qty
        size_extra = self._size_offset_ticks(qty, top_depth)

        total_extra_ticks = vol_extra + wide_spread_extra + size_extra

        # Entries/SL: step away from touch on the maker-safe side; TP: same logic
        if side.upper() == "BUY":
            price = base - total_extra_ticks * tick
        else:
            price = base + total_extra_ticks * tick

        # Quantize to tick grid by rounding to nearest tick step away from touch
        # Avoid crossing the touch due to fp rounding
        if side.upper() == "BUY":
            # floor to tick grid
            k = int((price / max(tick, 1e-12)))
            price = k * tick
            if price >= base:
                price = base - tick
        else:
            k = int((price / max(tick, 1e-12)))
            price = k * tick
            if price <= base:
                price = base + tick

        return max(0.0, price)


class MakerOrderController:
    def __init__(self, bot, order_type: str, resolver: PegPriceResolver, tuning: Optional[MakerTuning] = None, exit_kind: Optional[str] = None):
        self.bot = bot
        self.order_type = order_type  # "entry" or "exit"
        self.resolver = resolver
        self.tuning = tuning or resolver.tuning
        self._amend_inflight: bool = False
        self._amend_started_at: float = 0.0
        self._last_price: Optional[float] = None
        self.exit_kind: Optional[str] = exit_kind
        self._last_qty: Optional[float] = None

    def _now(self) -> float:
        return time.time()

    def _get_tick(self) -> float:
        try:
            # Prefer exchange tickSize if available
            if getattr(self.bot, "_symbol_filters", None):
                ts = float(self.bot._symbol_filters.get("tickSize", 0) or 0)
                if ts > 0:
                    return ts
            return 10 ** (-(self.bot.pc.precision))
        except Exception:
            return 10 ** (-(getattr(self.bot.pc, 'precision', 1)))

    def _get_book(self) -> Optional[Book]:
        try:
            raw = self.bot._get_order_book()
            if not raw:
                return None
            return Book(
                best_bid=float(raw.get('best_bid', 0) or 0),
                best_ask=float(raw.get('best_ask', 0) or 0),
                bid_qty=float(raw.get('bid_qty', 0) or 0),
                ask_qty=float(raw.get('ask_qty', 0) or 0),
                ts=float(raw.get('ts', 0) or 0),
            )
        except Exception:
            return None

    def _current_order_id(self) -> Optional[str]:
        return self.bot.entry_id if self.order_type == "entry" else self.bot.exit_id

    def _set_current_order_id(self, cid: Optional[str]):
        if self.order_type == "entry":
            old = getattr(self.bot, 'entry_id', None)
            self.bot.entry_id = cid
            try:
                if cid and cid != old:
                    self.bot._summary_log(f"WORKING_CID_UPDATED {{type={self.order_type}, from={old}, to={cid}}}")
            except Exception:
                pass
        else:
            old = getattr(self.bot, 'exit_id', None)
            self.bot.exit_id = cid
            try:
                if cid and cid != old:
                    self.bot._summary_log(f"WORKING_CID_UPDATED {{type={self.order_type}, from={old}, to={cid}}}")
            except Exception:
                pass

    async def place_or_amend(self, *, side: str, qty: float, reduce_only: bool) -> Tuple[Optional[str], float]:
        book = self._get_book()
        tick = self._get_tick()
        if not book or tick <= 0:
            # Best effort fallback to last seen price
            p = float(self.bot.prices.get(self.bot.pair, 0) or 0)
            if p <= 0:
                raise RuntimeError("No book/price available to place order")
            # Nudge one tick to maker side
            price = (p - tick) if side.upper() == "BUY" else (p + tick)
        else:
            # Determine the effective order type for resolver
            if self.order_type == "entry":
                ot = "entry"
            else:
                ot = (self.exit_kind or ("tp" if reduce_only else "sl"))
            price = self.resolver.resolve_price(side=side, order_type=ot, qty=qty, book=book, tick=tick)

        # If we already have an order, try amend; otherwise place
        cid = self._current_order_id()
        if cid:
            return await self._amend_to_price(side=side, qty=qty, new_price=price, reduce_only=reduce_only)
        return await self._place_new(side=side, qty=qty, price=price, reduce_only=reduce_only)

    async def amend_edge_if_needed(self, *, side: str, qty: float, reduce_only: bool) -> Tuple[Optional[str], float]:
        # Inflight guard
        if self._amend_inflight and (self._now() - self._amend_started_at) * 1000 < self.tuning.amend_time_budget_ms:
            self.bot._detailed_log("AMEND_SKIPPED {reason=inflight|budget}")
            return self._current_order_id(), self._last_price or 0.0

        book = self._get_book()
        tick = self._get_tick()
        if not book or tick <= 0:
            return self._current_order_id(), self._last_price or 0.0

        # Determine the effective order type for resolver
        if self.order_type == "entry":
            ot = "entry"
        else:
            ot = (self.exit_kind or ("tp" if reduce_only else "sl"))
        desired = self.resolver.resolve_price(side=side, order_type=ot, qty=qty, book=book, tick=tick)
        cur_id = self._current_order_id()
        if not cur_id:
            # No order yet, place it
            return await self._place_new(side=side, qty=qty, price=desired, reduce_only=reduce_only)

        # Fetch current price via status (best effort)
        cur_price = None
        try:
            status = await (self.bot.order_manager.get_order(self.bot.pair, cur_id) if self.bot.order_manager else self.bot.rest.get_order(self.bot.pair, cur_id))
            cur_price = float(status.get("price", 0) or 0)
            if cur_price <= 0:
                cur_price = None
        except Exception:
            cur_price = None

        # Fallback to last known price if status is unavailable
        if cur_price is None:
            cur_price = self._last_price or 0.0

        # Skip amend if within one tick
        if abs(cur_price - desired) < max(tick, 1e-12):
            return cur_id, desired

        return await self._amend_to_price(side=side, qty=qty, new_price=desired, reduce_only=reduce_only)

    async def handle_partial_fill(self, *, side: str, orig_qty: float, executed_qty: float, reduce_only: bool) -> Tuple[Optional[str], float]:
        remaining = max(0.0, float(orig_qty) - float(executed_qty))
        if remaining <= 0:
            return self._current_order_id(), self._last_price or 0.0
        return await self.amend_edge_if_needed(side=side, qty=remaining, reduce_only=reduce_only)

    async def _place_new(self, *, side: str, qty: float, price: float, reduce_only: bool) -> Tuple[Optional[str], float]:
        pair = self.bot.pair
        tick = self._get_tick()
        ladder = list(self.tuning.ladder_ticks)
        cid_prefix = f"{pair}_open" if self.order_type == "entry" else f"{pair}_close"
        # Try ladder with rebase per step
        for step_idx, ticks in enumerate(ladder):
            # Rebase to fresh book at each step
            book = self._get_book()
            if book:
                base = book.best_bid if side.upper() == "BUY" else book.best_ask
                # step away from touch by ticks
                p = base - ticks * tick if side.upper() == "BUY" else base + ticks * tick
                price = p
            cid = f"{cid_prefix}_{int(time.time())}_lad{ticks}"
            try:
                if self.bot.order_manager:
                    await self.bot.order_manager.place_limit(pair, side, qty, price, cid, reduce_only, self.bot.pc.precision)
                else:
                    await self.bot.rest.place_limit(pair, side, qty, price, cid, reduce_only)
                self._set_current_order_id(cid)
                self._last_price = price
                self._last_qty = float(qty)
                try:
                    book = self._get_book()
                    if book:
                        tick = self._get_tick()
                        base = book.best_bid if side.upper() == "BUY" else book.best_ask
                        ticks_off = 0
                        try:
                            ticks_off = int(round(abs(price - base) / max(tick, 1e-12)))
                        except Exception:
                            ticks_off = 0
                        self.bot._summary_log(f"EDGE_PEGGED {{type={self.order_type}, side={side}, price={price:.8f}, best_bid={book.best_bid:.8f}, best_ask={book.best_ask:.8f}, ticks_off={ticks_off}}}")
                    else:
                        self.bot._summary_log(f"EDGE_PEGGED {{type={self.order_type}, side={side}, price={price:.8f}}}")
                except Exception:
                    self.bot._summary_log(f"EDGE_PEGGED {{type={self.order_type}, side={side}, price={price:.8f}}}")
                return cid, price
            except Exception as e:
                es = str(e)
                if "-5022" in es or "Post Only order" in es:
                    self.bot._summary_log(f"ORDER_LADDER_STEP {{ticks={ticks}, rebase_price={price:.8f}}}")
                    # Debounce before next ladder step
                    try:
                        await asyncio.sleep(self.tuning.ladder_debounce_ms / 1000)
                    except Exception:
                        pass
                    continue
                # Non-5022 error: bubble up
                raise
        # Last resort: one extra tick away
        extra_ticks = ladder[-1] + 1 if ladder else 1
        book = self._get_book()
        if book:
            base = book.best_bid if side.upper() == "BUY" else book.best_ask
            price = base - extra_ticks * tick if side.upper() == "BUY" else base + extra_ticks * tick
        cid = f"{cid_prefix}_{int(time.time())}_lad{extra_ticks}"
        if self.bot.order_manager:
            await self.bot.order_manager.place_limit(pair, side, qty, price, cid, reduce_only, self.bot.pc.precision)
        else:
            await self.bot.rest.place_limit(pair, side, qty, price, cid, reduce_only)
        self._set_current_order_id(cid)
        self._last_price = price
        self._last_qty = float(qty)
        return cid, price

    async def _amend_to_price(self, *, side: str, qty: float, new_price: float, reduce_only: bool) -> Tuple[Optional[str], float]:
        # Inflight guard
        if self._amend_inflight and (self._now() - self._amend_started_at) * 1000 < self.tuning.amend_time_budget_ms:
            self.bot._summary_log("AMEND_SKIPPED {reason=inflight|budget}")
            return self._current_order_id(), self._last_price or new_price
        self._amend_inflight = True
        self._amend_started_at = self._now()
        try:
            orig = self._current_order_id()
            if not orig:
                return await self._place_new(side=side, qty=qty, price=new_price, reduce_only=reduce_only)
            # Avoid redundant amend when same price within one tick
            try:
                tick = self._get_tick()
            except Exception:
                tick = 0.0
            if self._last_price is not None and abs((self._last_price or 0.0) - new_price) < max(tick, 1e-12):
                return orig, self._last_price

            # Determine if quantity changed (use stepSize if available)
            quantity_changed = False
            try:
                step = 0.0
                if getattr(self.bot, "_symbol_filters", None):
                    step = float(self.bot._symbol_filters.get("stepSize", 0) or 0)
                if step <= 0:
                    step = float(getattr(self.bot.pc, 'quantity_step', 0.0) or 0.0)
                prev_q = float(self._last_qty or 0.0)
                cur_q = float(qty)
                if step > 0:
                    # Compare in steps to ignore rounding dust
                    quantity_changed = int(round(prev_q / step)) != int(round(cur_q / step))
                else:
                    quantity_changed = abs(prev_q - cur_q) > 0
            except Exception:
                quantity_changed = abs(float(self._last_qty or 0.0) - float(qty)) > 0

            # Log explicit amend reason
            try:
                reason = "qty_change" if quantity_changed else "book_change"
                old_p = float(self._last_price or 0.0)
                new_p = float(new_price)
                tick_sz = float(tick or 0.0)
                td = 0
                try:
                    td = int(round(abs(new_p - old_p) / max(tick_sz, 1e-12)))
                except Exception:
                    td = 0
                book = self._get_book()
                if book:
                    base = book.best_bid if side.upper() == "BUY" else book.best_ask
                    off_ticks = 0
                    try:
                        off_ticks = int(round(abs(new_p - base) / max(tick_sz, 1e-12)))
                    except Exception:
                        off_ticks = 0
                    self.bot._summary_log(f"EDGE_PEGGED {{type={self.order_type}, reason={reason}, tick_diff={td}, old_price={old_p:.8f}, new_price={new_p:.8f}, best_bid={book.best_bid:.8f}, best_ask={book.best_ask:.8f}, off_ticks={off_ticks}, tickSize={tick_sz}}}")
                else:
                    self.bot._summary_log(f"EDGE_PEGGED {{type={self.order_type}, reason={reason}, tick_diff={td}, old_price={old_p:.8f}, new_price={new_p:.8f}, tickSize={tick_sz}}}")
            except Exception:
                pass

            # Try direct amend
            try:
                new_cid, mode = await self.bot.order_manager.update_limit_price(
                    self.bot.pair, orig, side, qty, new_price, reduce_only, self.bot.pc.precision, ws_only=True
                )
                # Confirm actual working order reflects the amend when possible
                try:
                    verified = await self.bot.order_manager.get_order(self.bot.pair, new_cid)
                    if isinstance(verified, dict) and float(verified.get("price", new_price)) != float(new_price):
                        self.bot._detailed_log(f"Amend verification mismatch: target={new_price} actual={verified.get('price')}")
                except Exception:
                    pass
                self.bot._summary_log(f"ORDER_AMENDED {{type={self.order_type}, old={self._last_price}, new={new_price}, mode={mode}}}")
                self._set_current_order_id(new_cid)
                self._last_price = new_price
                self._last_qty = float(qty)
                return new_cid, new_price
            except Exception as e:
                es = str(e)
                # Ladder on -5022
                if "-5022" in es or "Post Only order" in es:
                    try:
                        self.bot._summary_log(f"ORDER_REJECTED {{mode=ws, code=-5022, msg={es}}}")
                    except Exception:
                        pass
                    tick = self._get_tick()
                    for ticks in self.tuning.ladder_ticks:
                        book = self._get_book()
                        base = None
                        if book:
                            base = book.best_bid if side.upper() == "BUY" else book.best_ask
                        target = new_price
                        if base is not None:
                            target = base - ticks * tick if side.upper() == "BUY" else base + ticks * tick
                        # Skip if no effective change
                        if self._last_price is not None and abs(self._last_price - target) < max(tick, 1e-12):
                            continue
                        try:
                            new_cid, mode = await self.bot.order_manager.update_limit_price(
                                self.bot.pair, orig, side, qty, target, reduce_only, self.bot.pc.precision, ws_only=True
                            )
                            try:
                                verified = await self.bot.order_manager.get_order(self.bot.pair, new_cid)
                                if isinstance(verified, dict) and float(verified.get("price", target)) != float(target):
                                    self.bot._detailed_log(f"Amend verification mismatch: target={target} actual={verified.get('price')}")
                            except Exception:
                                pass
                            self.bot._summary_log(f"ORDER_AMENDED {{type={self.order_type}, old={self._last_price}, new={target}, mode={mode}}}")
                            self.bot._summary_log(f"ORDER_LADDER_STEP {{ticks={ticks}, rebase_price={target}}}")
                            self._set_current_order_id(new_cid)
                            self._last_price = target
                            self._last_qty = float(qty)
                            return new_cid, target
                        except Exception as e2:
                            if "-5022" in str(e2):
                                try:
                                    await asyncio.sleep(self.tuning.ladder_debounce_ms / 1000)
                                except Exception:
                                    pass
                                continue
                            try:
                                self.bot._summary_log(f"ORDER_REJECTED {{mode=ws, msg={str(e2)}}}")
                            except Exception:
                                pass
                            raise
                # Other errors: fallback to cancel+place path using order_manager
                # NEW: Stop-on-filled guard — if current order is already FILLED/CANCELED, halt further actions
                try:
                    snapshot = await self.bot.order_manager.get_order(self.bot.pair, orig)
                    if isinstance(snapshot, dict):
                        st = str(snapshot.get("status", "")).upper()
                        if st in ("FILLED", "CANCELED"):
                            return orig, self._last_price or new_price
                        # ReduceOnly safety clamp: for exits, cap qty to current positionAmt
                        if reduce_only:
                            try:
                                pos_list = await self.bot.order_manager.position_risk(self.bot.pair)
                                pos_amt = 0.0
                                for p in pos_list or []:
                                    if p.get("symbol") == self.bot.pair:
                                        pos_amt = abs(float(p.get("positionAmt", 0) or 0))
                                        break
                                if pos_amt > 0:
                                    qty = min(float(qty), float(pos_amt))
                            except Exception:
                                pass
                except Exception:
                    pass
                new_cid, mode = await self.bot.order_manager.update_limit_price(
                    self.bot.pair, orig, side, qty, new_price, reduce_only, self.bot.pc.precision, ws_only=False
                )
                self._set_current_order_id(new_cid)
                self._last_price = new_price
                self._last_qty = float(qty)
                return new_cid, new_price
        finally:
            self._amend_inflight = False 