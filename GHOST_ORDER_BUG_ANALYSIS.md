# Ghost Order Bug Analysis - BCHUSDT Nov 10, 2025

## Issue Summary
**CRITICAL BUG**: Bot attempted to amend a non-existent order 737 times over 41 minutes, failing to detect order cancellation and recover.

## Timeline
- **06:00:10.789**: NEW SIGNAL RECEIVED: SHORT
- **06:00:10.885**: ORDER_PLACE_REQUEST (cid=BCHUSDT_open_1762754410_lad0, SELL 0.164 @ 503.54)
- **06:00:10.962**: ORDER_PLACE_RESPONSE (success)
- **06:00:10.963**: ✅ ENTRY EDGE PLACED: SELL 0.164@503.54000
- **06:00:12.811**: First EDGE_PEGGED attempt (price changed to 503.38)
- **06:00:13.200**: First "Order does not exist" error (WS_AMEND_ERROR -2013)
- **06:00:13.446**: ORDER_VERIFIED_FINAL {status=CANCELED, via=rest}
- **06:00:13 - 06:41:43**: Bot continued trying to amend for **41 MINUTES**
- **06:41:43**: Last attempt to amend the ghost order
- **07:00:09.078**: NEW SIGNAL RECEIVED: SHORT (next hourly signal)
- **07:00:09.248**: New order placed (BCHUSDT_open_1762758009_lad0)
- **07:00:10.047**: ENTRY_FILLED_STREAM (new order filled)
- **07:00:10.642**: ⚠️ PROACTIVE_SYNC triggered (bot had no internal state for filled order)

## Statistics
- **Total references to ghost order**: 1,477 log lines
- **"Order does not exist" errors**: 737 errors
- **Duration**: 41 minutes (2,493 seconds)
- **Average error frequency**: ~1 error every 3.4 seconds
- **No active order**: Bot had NO working order for the entire 41-minute period despite signal being active

## Root Cause Analysis

### Why Was the Order Cancelled?
The order was placed with `tif=GTX` (Good-Til-Crossed / Post-Only):
- Order: SELL @ 503.54
- Market: best_bid=503.53, best_ask=503.54
- **Issue**: Order was placed exactly at best_ask. If order book changed microseconds later, the order would cross and get auto-cancelled by Binance's GTX rules.

### Why Didn't the Bot Detect Cancellation?
Looking at the sequence:
```
06:00:12.811 EDGE_PEGGED {type=entry, reason=book_change, tick_diff=16, old_price=503.54, new_price=503.38}
06:00:12.811 ORDER_AMEND_REQUEST {cid=BCHUSDT_open_1762754410_lad0, attempt=1}
06:00:13.105 ORDER_AMEND_REQUEST {cid=BCHUSDT_open_1762754410_lad0, attempt=2}
06:00:13.200 ORDER_REJECTED {mode=ws, code=None, msg=WS_AMEND_ERROR status=400 code=-2013 msg=Order does not exist.}
06:00:13.446 ORDER_VERIFIED_FINAL {cid=BCHUSDT_open_1762754410_lad0, status=CANCELED, via=rest}
06:00:13.591 ORDER_AMENDED {type=entry, old=503.54, new=503.38, mode=order_canceled}
```

**The bot DID detect the cancellation** via:
1. WebSocket amendment rejection (-2013)
2. REST API verification (ORDER_VERIFIED_FINAL status=CANCELED)
3. Logging "ORDER_AMENDED {mode=order_canceled}"

**But it FAILED to:**
1. Clear `self.entry_id` (left it as `BCHUSDT_open_1762754410_lad0`)
2. Stop the edge-pegging loop for this order
3. Attempt to place a new order
4. Flag the signal as needing re-entry

### Why Did It Keep Trying for 41 Minutes?
The bot's edge-pegging logic likely checks:
- "Is there an active signal?" → YES (SHORT signal active)
- "Do I have an entry_id?" → YES (`BCHUSDT_open_1762754410_lad0`)
- "Is the price different from current book?" → YES (market moving)
- **Action**: Try to amend order

Because `self.entry_id` was never cleared, the bot thought it still had a working order and continued trying to amend it on every price tick.

### Why Did Next Signal Work?
At 07:00:09, a new SHORT signal arrived:
- Bot likely has logic: "On new signal, clear old entry_id"
- Placed fresh order with new cid (`BCHUSDT_open_1762758009_lad0`)
- Order filled immediately
- **But**: PROACTIVE_SYNC triggered, indicating bot lost track of its own order fill

## Critical Problems Identified

### 1. **Ghost Order State** (PRIMARY BUG)
**When**: Order gets cancelled (by exchange or bot) but `entry_id` is not cleared
**Impact**: Bot thinks it has a working order when it doesn't
**Duration**: Can persist until next signal (potentially hours)
**Symptoms**:
- Continuous "Order does not exist" errors
- No actual working order on exchange
- No new order placement attempts
- Market exposure gap (no entry despite active signal)

### 2. **Failed Order Recovery**
**When**: Order placement/amendment fails
**Impact**: Bot doesn't retry or place new order
**Consequences**:
- Missed trading opportunities
- Prolonged periods without market presence
- Signal intent not executed

### 3. **WebSocket Order Tracking Lost**
**When**: Order fills but bot requires PROACTIVE_SYNC
**Impact**: Suggests the bot didn't receive or process the WebSocket fill notification
**Evidence**: Line 07:00:10.642 "⚠️ PROACTIVE_SYNC: Position detected but no internal state"

### 4. **No Circuit Breaker for Repeated Errors**
**When**: Same error repeats hundreds of times
**Impact**: Wasted API calls, log spam, no self-correction
**Missing**: Error threshold detection (e.g., "if same error 5+ times, take corrective action")

## Proposed Solution (Surgical Precision)

### Solution 1: Order Status Verification After Amendment Rejection
**Trigger**: When ORDER_REJECTED with code -2013 ("Order does not exist")
**Action**:
```python
# In handle_user_data_event() or wherever WS_AMEND_ERROR is processed:
if error_code == -2013 and self.entry_id == rejected_order_cid:
    self._detailed_log(f"GHOST_ORDER_DETECTED: {rejected_order_cid} does not exist, clearing entry_id")
    self.entry_id = None
    # Option A: Try to replace the order if signal is still active
    if self.signal_type != "NEUTRAL":
        self._detailed_log(f"GHOST_ORDER_RECOVERY: Attempting to replace cancelled entry order")
        self._place_entry_order()  # Re-place entry order
    # Option B: Just clear and wait for next signal
    else:
        self._detailed_log(f"GHOST_ORDER_CLEARED: No active signal, awaiting next signal")
```

**Risk**: Low - only affects clearly cancelled orders
**Impact**: Prevents ghost order loops entirely

### Solution 2: Order Verification After REST Confirmation
**Trigger**: When ORDER_VERIFIED_FINAL shows status=CANCELED for entry_id
**Action**:
```python
# In _verify_order() or wherever ORDER_VERIFIED_FINAL is logged:
if order_status == "CANCELED" and cid == self.entry_id:
    self._detailed_log(f"ENTRY_ORDER_CANCELED: Clearing entry_id {cid}")
    self.entry_id = None
    # Try to recover if signal active
    if self.signal_type != "NEUTRAL":
        self._detailed_log(f"ENTRY_ORDER_RECOVERY: Signal {self.signal_type} active, replacing order")
        self._place_entry_order()
```

**Risk**: Low - REST API confirmation is definitive
**Impact**: Catches cancellations that slip through WebSocket

### Solution 3: Circuit Breaker for Repeated Amendment Failures
**Trigger**: N consecutive amendment failures for same order
**Action**:
```python
# Add to __init__:
self.entry_amend_failure_count = 0
self.entry_amend_failure_threshold = 3  # Stop after 3 failures

# In amendment error handling:
if error_code == -2013:
    self.entry_amend_failure_count += 1
    if self.entry_amend_failure_count >= self.entry_amend_failure_threshold:
        self._detailed_log(f"CIRCUIT_BREAKER: {self.entry_amend_failure_count} consecutive amendment failures, clearing entry_id")
        self.entry_id = None
        self.entry_amend_failure_count = 0
        if self.signal_type != "NEUTRAL":
            self._place_entry_order()

# Reset counter on successful amendment:
if amendment_success:
    self.entry_amend_failure_count = 0
```

**Risk**: Very low - prevents runaway error loops
**Impact**: Limits damage from any order tracking bug

### Solution 4: Periodic Entry Order Health Check
**Trigger**: Every N seconds (e.g., 60s), verify entry order exists if entry_id is set
**Action**:
```python
# In main tick loop:
def _check_entry_order_health(self):
    """Verify entry order still exists on exchange if we think we have one"""
    if not self.entry_id or self.position_active:
        return
    
    # Check last verification time
    if now() - self.last_entry_health_check < 60:  # Check every 60s
        return
    
    self.last_entry_health_check = now()
    
    # Query exchange for order status
    order = self._get_order_status(self.entry_id)
    if not order or order['status'] in ['CANCELED', 'EXPIRED', 'REJECTED']:
        self._detailed_log(f"HEALTH_CHECK_FAILED: Entry order {self.entry_id} not found or cancelled")
        self.entry_id = None
        if self.signal_type != "NEUTRAL":
            self._detailed_log(f"HEALTH_CHECK_RECOVERY: Replacing missing entry order")
            self._place_entry_order()
```

**Risk**: Medium - adds periodic REST API calls
**Impact**: Catches any order tracking desync within 60s

## Recommended Implementation Priority

1. **MUST HAVE** (Fix immediately):
   - Solution 1: Clear entry_id on -2013 error
   - Solution 2: Clear entry_id on REST CANCELED confirmation
   - Solution 3: Circuit breaker (3 failures = clear entry_id)

2. **SHOULD HAVE** (Add for robustness):
   - Solution 4: Periodic health check (60s interval)

3. **NICE TO HAVE** (Future enhancement):
   - Order fill WebSocket tracking improvement (to prevent PROACTIVE_SYNC on own fills)
   - Detailed alerting when ghost orders detected
   - Metrics/dashboard for order tracking issues

## Testing Strategy

### Test Case 1: GTX Order Auto-Cancellation
1. Place entry order at exact best_ask price
2. Verify bot detects cancellation within 5 seconds
3. Verify bot attempts to replace order
4. Verify no ghost order loop occurs

### Test Case 2: Manual Order Cancellation
1. Let bot place entry order
2. Manually cancel via Binance UI
3. Verify bot detects within 60 seconds (health check)
4. Verify bot replaces order if signal active

### Test Case 3: Network Disruption During Amendment
1. Place entry order
2. Simulate network error during amendment
3. Verify circuit breaker triggers after 3 failures
4. Verify bot recovers gracefully

### Test Case 4: Rapid Order Cancellation
1. Place order that gets cancelled immediately (multiple times)
2. Verify bot doesn't spam order placement
3. Verify proper backoff or rate limiting

## Impact Assessment

### Current Impact (Without Fix)
- **Trading Efficiency**: Can miss entire hour of trading (41 minutes in this case)
- **API Usage**: 737 wasted API calls in 41 minutes (~18/minute)
- **Risk Management**: No market presence despite active signal
- **Binance API Limits**: Risk of rate limiting or account flags

### Post-Fix Impact
- Ghost order detection: <5 seconds
- Order replacement: Immediate (if signal active)
- API waste: Eliminated (max 3 failed attempts before recovery)
- Trading uptime: Near 100% when signal active

## Related Issues
- Similar to position closure race condition (fixed in v1.1.1)
- Indicates broader pattern: Bot doesn't handle exchange rejections robustly
- Suggests need for comprehensive "state reconciliation" system

## Code Locations to Modify
(Will need to examine bot_22_A.py to identify exact functions)
- Order amendment error handling
- ORDER_VERIFIED_FINAL processing
- Entry order placement logic
- Health check/tick loop

## Questions for Code Review
1. Where is `self.entry_id` set and cleared currently?
2. How does bot handle ORDER_PLACE_RESPONSE vs ORDER_TRADE vs ORDER_UPDATE events?
3. Is there existing error threshold tracking we can leverage?
4. Where should order replacement logic live?
5. Should we also check exit_id and emergency_cid for similar issues?

