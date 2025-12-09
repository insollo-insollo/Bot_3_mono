# Binance Algo Service Migration - December 9, 2025

## Overview

This document describes the changes made to Bot_3_mono to comply with Binance's Algo Service migration for USDⓈ-M Futures conditional orders.

**Effective Date:** December 9, 2025  
**Announced:** November 6, 2025  
**Source:** [Binance Derivatives API Change Log](https://developers.binance.com/docs/derivatives/change-log)

## What Changed (Binance Side)

Binance migrated the following order types to their Algo Service:
- `STOP_MARKET`
- `TAKE_PROFIT_MARKET`
- `STOP`
- `TAKE_PROFIT`
- `TRAILING_STOP_MARKET`

### New API Endpoints

| Action | Old Endpoint | New Endpoint |
|--------|--------------|--------------|
| Place | `POST /fapi/v1/order` | `POST /fapi/v1/algoOrder` |
| Cancel | `DELETE /fapi/v1/order` | `DELETE /fapi/v1/algoOrder` |
| Cancel All | - | `DELETE /fapi/v1/algoOpenOrders` |
| Query | `GET /fapi/v1/order` | `GET /fapi/v1/algoOrder` |
| Query Open | - | `GET /fapi/v1/openAlgoOrders` |
| Query All | - | `GET /fapi/v1/allAlgoOrders` |

### New WebSocket Methods

| Action | Old Method | New Method |
|--------|------------|------------|
| Place | `order.place` | `algoOrder.place` |
| Cancel | `order.cancel` | `algoOrder.cancel` |

### New WebSocket Event

- `ALGO_UPDATE` - Fires when conditional orders change status

### Critical Breaking Change

> **Modification of untriggered conditional orders is NOT SUPPORTED**

The old `order.modify` method no longer works for STOP_MARKET and other conditional orders.

### Old Endpoints Now Return Error

Using the old endpoints for conditional orders will return:
```
Error code: -4120 (STOP_ORDER_SWITCH_ALGO)
```

## Changes Made to Bot_3_mono

### Files Modified

1. **`order_manager.py`**
2. **`bot_22_A.py`**

### Backups Created

- `config.json.backup_before_algo_migration`
- `order_manager.py.backup_before_algo_migration`

---

## Detailed Changes

### 1. `order_manager.py` - `place_emergency_stop()` (Lines 766-833)

**Before:**
```python
params = {
    "symbol": symbol,
    "side": side,
    "type": "STOP_MARKET",
    "stopPrice": str(stop_price),
    "closePosition": "true",
    "workingType": "CONTRACT_PRICE",
    "newClientOrderId": cid
}
resp = await self.ws.send_request("order.place", params)
```

**After:**
```python
params = {
    "symbol": symbol,
    "side": side,
    "algoType": "CONDITIONAL",  # NEW
    "type": "STOP_MARKET",
    "triggerPrice": str(stop_price),  # Changed from stopPrice
    "closePosition": "true",
    "workingType": "CONTRACT_PRICE",
    "newClientOrderId": cid
}
resp = await self.ws.send_request("algoOrder.place", params)  # NEW method
```

**REST Fallback:** Changed from `POST /fapi/v1/order` to `POST /fapi/v1/algoOrder`

---

### 2. `order_manager.py` - `modify_emergency_stop()` (Lines 843-961)

**Before:** Used `order.modify` WebSocket method to modify stop price in-place.

**After:** Implements **cancel-and-replace** strategy:

1. Cancel existing order via `algoOrder.cancel` (WS) or `DELETE /fapi/v1/algoOrder` (REST)
2. Place new order via `algoOrder.place` (WS) or `POST /fapi/v1/algoOrder` (REST)
3. Return new client order ID

**Important:** The function now returns `(new_cid, mode)` instead of `(resp, mode)` because the order ID changes after replacement.

---

### 3. `bot_22_A.py` - ESL CID Tracking Updates

Three call sites updated to capture and track the new CID after modification:

**Line ~1396:**
```python
new_cid, mode = await self.order_manager.modify_emergency_stop(...)
if new_cid and new_cid != self.emergency_cid:
    self.emergency_cid = new_cid
```

**Line ~1901:**
```python
new_cid, mode = await self.order_manager.modify_emergency_stop(...)
if new_cid and new_cid != self.emergency_cid:
    self.emergency_cid = new_cid
```

**Line ~3260:**
```python
new_cid, mode = await self.order_manager.modify_emergency_stop(...)
if new_cid and new_cid != kept_cid:
    self.emergency_cid = new_cid
    kept_cid = new_cid
```

---

## What's NOT Affected

The following order types still use the original endpoints:

| Order Type | Endpoint | Status |
|------------|----------|--------|
| `LIMIT` (entry orders) | `order.place` / `order.modify` | ✅ No change |
| `LIMIT` (exit orders) | `order.place` / `order.modify` | ✅ No change |
| `MARKET` | `order.place` | ✅ No change |

Edge-pegging and re-pegging of LIMIT orders continues to work as before.

---

## Log Message Changes

| Old Log | New Log |
|---------|---------|
| `EMERGENCY_STOP_PLACE_RESPONSE {mode=ws}` | `EMERGENCY_STOP_PLACE_RESPONSE {mode=ws.algo}` |
| `EMERGENCY_STOP_PLACE_RESPONSE {mode=rest}` | `EMERGENCY_STOP_PLACE_RESPONSE {mode=rest.algo}` |
| `EMERGENCY_STOP_MODIFY_RESPONSE {mode=ws}` | `EMERGENCY_STOP_MODIFY_RESPONSE {mode=cancel+replace, old_cid=..., new_cid=...}` |

---

## Future Considerations

### ALGO_UPDATE Event Handler

The new `ALGO_UPDATE` WebSocket event can be used to track conditional order status changes. Currently not implemented because:

1. ESL uses `closePosition=true` - when triggered, the position closes and `ACCOUNT_UPDATE` fires
2. The bot detects flat positions through position tracking, not order status
3. The current implementation works without explicit algo order status tracking

This can be added as a future enhancement for better order state tracking.

---

## Testing Checklist

- [ ] Bot starts without errors
- [ ] New ESL placement works (check for `EMERGENCY_STOP_PLACE_RESPONSE {mode=ws.algo}`)
- [ ] ESL modification works (check for `EMERGENCY_STOP_MODIFY_RESPONSE {mode=cancel+replace}`)
- [ ] No duplicate processes
- [ ] Entry/exit edge-pegging still works
- [ ] Trailing stop logic still works
- [ ] Position tracking still works

---

## Rollback Instructions

If issues occur, restore from backups:

```bash
cd /root/Bot_3_mono
cp order_manager.py.backup_before_algo_migration order_manager.py
# bot_22_A.py changes are minimal - can be reverted via git
git checkout bot_22_A.py
```

---

## References

- [Binance Derivatives Change Log](https://developers.binance.com/docs/derivatives/change-log)
- [Binance USDⓈ-M Futures Algo Order API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order)

---

*Document created: December 9, 2025*
*Author: AI Assistant*

