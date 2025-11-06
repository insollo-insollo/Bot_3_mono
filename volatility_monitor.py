"""
Volatility Monitoring and High Volatility Mode Management
Added: 2025-11-06 - Volatility-aware trading mode
"""

import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class VolatilityConfig:
    """Configuration for volatility detection and HV mode"""
    # Score thresholds
    HV_ENTER_THRESHOLD: float = 60.0  # Score > 60 enters HV mode
    HV_EXIT_THRESHOLD: float = 40.0   # Score < 40 exits HV mode
    
    # Price movement weights (0-40 points max)
    MAX_CHANGE_WEIGHT: float = 2500.0  # multiplier for max change
    AVG_CHANGE_WEIGHT: float = 7500.0  # multiplier for avg change
    
    # Spread weights (0-30 points max)
    SPREAD_MULTIPLIER: float = 60.0
    
    # Amendment failure weights (0-30 points max)
    FAILURE_MULTIPLIER: float = 30.0
    
    # History lengths
    PRICE_HISTORY_LENGTH: int = 60
    SPREAD_HISTORY_LENGTH: int = 20
    FAILURE_HISTORY_LENGTH: int = 20


class VolatilityMonitor:
    """
    Multi-signal volatility detection system.
    Monitors price changes, spread widening, and amendment failures to detect volatility.
    """
    
    def __init__(self, config: Optional[VolatilityConfig] = None):
        self.config = config or VolatilityConfig()
        
        # Data storage per pair
        self.price_changes: Dict[str, List[float]] = {}
        self.spread_history: Dict[str, List[float]] = {}
        self.amendment_failures: Dict[str, List[int]] = {}
        
        # Current state
        self.volatility_scores: Dict[str, float] = {}
        self.volatility_modes: Dict[str, str] = {}  # pair -> "NORMAL" | "HIGH_VOLATILITY"
        
        # Circuit breaker tracking
        self.consecutive_failures: Dict[str, int] = {}
        self.cancel_and_replace_count: Dict[str, int] = {}
        
    def calculate_volatility_score(self, pair: str) -> float:
        """Calculate 0-100 volatility score from multiple signals"""
        score = 0.0
        
        # Signal 1: Price movement (0-40 points)
        if pair in self.price_changes:
            changes = self.price_changes[pair][-self.config.PRICE_HISTORY_LENGTH:]
            if len(changes) > 10:
                max_change = max(abs(c) for c in changes)
                avg_change = sum(abs(c) for c in changes) / len(changes)
                
                # Max change contribution (0-25 points)
                if max_change > 0.01:  # >1% move
                    score += min(25.0, max_change * self.config.MAX_CHANGE_WEIGHT)
                
                # Average change contribution (0-15 points)
                if avg_change > 0.002:  # >0.2% average
                    score += min(15.0, avg_change * self.config.AVG_CHANGE_WEIGHT)
        
        # Signal 2: Spread widening (0-30 points)
        if pair in self.spread_history:
            spreads = self.spread_history[pair][-self.config.SPREAD_HISTORY_LENGTH:]
            if len(spreads) > 5:
                current_spread = spreads[-1]
                avg_spread = sum(spreads[:-1]) / len(spreads[:-1])
                
                if current_spread > avg_spread * 1.5:  # Spread 50% wider
                    spread_score = min(30.0, (current_spread / avg_spread - 1) * self.config.SPREAD_MULTIPLIER)
                    score += spread_score
        
        # Signal 3: Amendment failure rate (0-30 points)
        if pair in self.amendment_failures:
            failures = self.amendment_failures[pair][-self.config.FAILURE_HISTORY_LENGTH:]
            if len(failures) > 5:
                failure_rate = sum(failures) / len(failures)
                score += min(30.0, failure_rate * self.config.FAILURE_MULTIPLIER)
        
        return min(100.0, score)
    
    def update_and_check_mode(self, pair: str) -> str:
        """Update volatility score and switch modes if needed"""
        score = self.calculate_volatility_score(pair)
        self.volatility_scores[pair] = score
        
        current_mode = self.volatility_modes.get(pair, "NORMAL")
        
        if current_mode == "NORMAL" and score > self.config.HV_ENTER_THRESHOLD:
            # Switch to HV mode
            self.volatility_modes[pair] = "HIGH_VOLATILITY"
            self.cancel_and_replace_count[pair] = 0  # Reset counter
            return "HIGH_VOLATILITY"
        
        elif current_mode == "HIGH_VOLATILITY" and score < self.config.HV_EXIT_THRESHOLD:
            # Switch back to normal
            self.volatility_modes[pair] = "NORMAL"
            return "NORMAL"
        
        return current_mode
    
    def on_price_update(self, pair: str, old_price: float, new_price: float):
        """Track price changes"""
        if pair not in self.price_changes:
            self.price_changes[pair] = []
        
        if old_price > 0:
            pct_change = (new_price - old_price) / old_price
            self.price_changes[pair].append(pct_change)
            
            # Keep only recent data
            if len(self.price_changes[pair]) > self.config.PRICE_HISTORY_LENGTH + 40:
                self.price_changes[pair] = self.price_changes[pair][-self.config.PRICE_HISTORY_LENGTH:]
    
    def on_spread_update(self, pair: str, spread_pct: float):
        """Track spread changes"""
        if pair not in self.spread_history:
            self.spread_history[pair] = []
        
        self.spread_history[pair].append(spread_pct)
        
        # Keep only recent data
        if len(self.spread_history[pair]) > self.config.SPREAD_HISTORY_LENGTH + 10:
            self.spread_history[pair] = self.spread_history[pair][-self.config.SPREAD_HISTORY_LENGTH:]
    
    def on_amendment_result(self, pair: str, success: bool):
        """Track amendment success/failure"""
        if pair not in self.amendment_failures:
            self.amendment_failures[pair] = []
        
        self.amendment_failures[pair].append(0 if success else 1)
        
        # Keep only recent data
        if len(self.amendment_failures[pair]) > self.config.FAILURE_HISTORY_LENGTH + 10:
            self.amendment_failures[pair] = self.amendment_failures[pair][-self.config.FAILURE_HISTORY_LENGTH:]
        
        # Update consecutive failures for circuit breaker
        if not success:
            self.consecutive_failures[pair] = self.consecutive_failures.get(pair, 0) + 1
        else:
            self.consecutive_failures[pair] = 0
        
        # Check if mode should change after each amendment
        self.update_and_check_mode(pair)
    
    def get_mode(self, pair: str) -> str:
        """Get current volatility mode for a pair"""
        return self.volatility_modes.get(pair, "NORMAL")
    
    def get_score(self, pair: str) -> float:
        """Get current volatility score for a pair"""
        return self.volatility_scores.get(pair, 0.0)
    
    def get_consecutive_failures(self, pair: str) -> int:
        """Get consecutive amendment failures count"""
        return self.consecutive_failures.get(pair, 0)
    
    def increment_cancel_replace(self, pair: str) -> int:
        """Increment cancel-and-replace counter and return new count"""
        self.cancel_and_replace_count[pair] = self.cancel_and_replace_count.get(pair, 0) + 1
        count = self.cancel_and_replace_count[pair]
        
        # If we've cancelled and replaced 5+ times, ensure HV mode is active
        if count >= 5:
            current_mode = self.volatility_modes.get(pair, "NORMAL")
            if current_mode != "HIGH_VOLATILITY":
                self.volatility_modes[pair] = "HIGH_VOLATILITY"
        
        return count
    
    def reset_circuit_breaker(self, pair: str):
        """Reset circuit breaker counters"""
        self.consecutive_failures[pair] = 0
        self.cancel_and_replace_count[pair] = 0


@dataclass
class OrderState:
    """Track state of a single order"""
    order_id: str
    status: str  # "ACTIVE", "FILLED", "CANCELED"
    amendable: bool
    last_amend_time: float = 0.0
    amendments_count: int = 0
    max_amendments: int = 999
    price: float = 0.0
    qty: float = 0.0


class OrderStateTracker:
    """
    Track lifecycle of all orders to prevent amendment loops.
    """
    
    def __init__(self):
        self.orders: Dict[str, OrderState] = {}
        
    def add_order(self, order_id: str, price: float, qty: float, max_amendments: int = 999):
        """Add a new order to tracking"""
        self.orders[order_id] = OrderState(
            order_id=order_id,
            status="ACTIVE",
            amendable=True,
            price=price,
            qty=qty,
            max_amendments=max_amendments
        )
    
    def can_amend(self, order_id: str, min_interval: float = 0.5) -> bool:
        """Check if order can be amended"""
        if order_id not in self.orders:
            return False
        
        state = self.orders[order_id]
        
        # Check status
        if state.status != "ACTIVE":
            return False
        
        if not state.amendable:
            return False
        
        # Check amendment count limit
        if state.amendments_count >= state.max_amendments:
            return False
        
        # Check time interval
        if time.time() - state.last_amend_time < min_interval:
            return False
        
        return True
    
    def record_amendment(self, order_id: str, new_price: float, new_qty: float):
        """Record an amendment attempt"""
        if order_id in self.orders:
            self.orders[order_id].amendments_count += 1
            self.orders[order_id].last_amend_time = time.time()
            self.orders[order_id].price = new_price
            self.orders[order_id].qty = new_qty
    
    def mark_filled(self, order_id: str):
        """Mark order as filled"""
        if order_id in self.orders:
            self.orders[order_id].status = "FILLED"
            self.orders[order_id].amendable = False
    
    def mark_canceled(self, order_id: str):
        """Mark order as canceled"""
        if order_id in self.orders:
            self.orders[order_id].status = "CANCELED"
            self.orders[order_id].amendable = False
    
    def remove_order(self, order_id: str):
        """Remove order from tracking"""
        if order_id in self.orders:
            del self.orders[order_id]
    
    def get_state(self, order_id: str) -> Optional[OrderState]:
        """Get order state"""
        return self.orders.get(order_id)

